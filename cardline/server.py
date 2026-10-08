"""Servidor local: página da coleção/pipelines, upload de vídeos e execução das pipelines em fila.

Cada pipeline roda num processo próprio (`cardline run <id>`), um por vez; o progresso vai para o
banco e a página acompanha por polling. Se o servidor cair, os runs em andamento ficam
'interrupted' e podem ser retomados de onde pararam.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, instagram, narration, pipeline, rarity, social, youtube
from .catalog import is_booster_set, refresh_prices, reset_icon, save_manual_icon
from .collection import card_uid, load_scan, remove_card, remove_repeated, repeated, restore_card, set_card_foil
from .config import Settings
from .index import image_path, indexed_sets
from .money import CURRENCIES, usd_brl
from .video import probe

WEB = Path(__file__).parent / "web"


class Runner:
    """Executa os runs da fila, um por vez, cada um num subprocesso com log em <run>/pipeline.log."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cardline-runner", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        con = db.connect(self.settings.db_path)
        while True:
            row = con.execute("SELECT id, dir FROM runs WHERE status = 'queued' ORDER BY created_at, id LIMIT 1").fetchone()
            if row is None:
                self._wake.wait(5)
                self._wake.clear()
                continue
            log_path = self.settings.root / row["dir"] / "pipeline.log"
            with open(log_path, "ab") as log:
                log.write(f"\n=== {db.now()} ===\n".encode())
                log.flush()
                code = subprocess.call(
                    [sys.executable, "-m", "cardline", "run", str(row["id"])],
                    cwd=self.settings.root, stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
            status = con.execute("SELECT status FROM runs WHERE id = ?", (row["id"],)).fetchone()
            if status and status["status"] in ("queued", "running"):  # o processo morreu sem registrar o fim
                with con:
                    con.execute("UPDATE runs SET status = 'failed', pid = NULL, finished_at = ?, error = ? WHERE id = ?",
                                (db.now(), f"O processo da pipeline terminou com código {code}.", row["id"]))


class SyncJob:
    """`cardline sync` em segundo plano (sets novos, cartas, preços, imagens, índices e ícones), um por vez."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.log_path = settings.cache_dir / "sync.log"
        self.proc: subprocess.Popen | None = None
        self.started_at = self.finished_at = None
        self.code: int | None = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        if self.running:
            raise RuntimeError("A sincronização já está rodando.")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "cardline", "sync"], cwd=self.settings.root, stdout=log,
                stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        self.started_at, self.finished_at, self.code = db.now(), None, None

    def status(self) -> dict:
        if self.proc is not None and not self.running and self.finished_at is None:
            self.finished_at, self.code = db.now(), self.proc.returncode
        lines = self.log_path.read_text(errors="replace").splitlines()[-40:] if self.log_path.exists() else []
        return {
            "running": self.running, "started_at": self.started_at, "finished_at": self.finished_at,
            "ok": None if self.code is None else self.code == 0,
            "log": "\n".join(line.rsplit("\r", 1)[-1] for line in lines),
        }


class RerunBody(BaseModel):
    from_step: str | None = None


class CardPatch(BaseModel):
    foil: bool | None = None  # por enquanto só o acabamento; trocar a carta vem depois


class RunPatch(BaseModel):
    """Só os campos enviados mudam (mandar só a moeda do vídeo não apaga o valor pago)."""

    paid: float | None = None
    paid_currency: str | None = None
    currency: str | None = None  # moeda do vídeo com overlay
    narration: bool | None = None  # liga/desliga a narração do vídeo


class ScriptBody(BaseModel):
    lines: list[dict]  # [{"t": segundos, "texto": "..."}]
    voz: str | None = None  # voz do XTTS-v2 desta narração


class ClientBody(BaseModel):
    client_id: str
    client_secret: str


class PostBody(BaseModel):
    title: str = ""  # título (YouTube)
    caption: str = ""  # legenda (Instagram) ou descrição (YouTube)
    privacy: str = "public"  # só o YouTube tem visibilidade
    variant: str = "narrado"  # narrado | overlay
    tags: list[str] | None = None  # tags do YouTube (vazio = as sugeridas)
    publish_at: str | None = None  # programar: data e hora (ISO 8601, com fuso); vazio = agora


class LinkBody(BaseModel):
    url: str  # link do post (YouTube, Instagram ou TikTok)


class StatsBody(BaseModel):
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None


class TokenBody(BaseModel):
    token: str


class YouTubeLogin:
    """Conexão do canal do YouTube: o fluxo de dispositivo espera o usuário digitar o código em google.com/device."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.login: dict | None = None  # código mostrado na página e situação da espera

    def start(self) -> dict:
        info = youtube.start_device_login(self.settings)
        state = {"user_code": info["user_code"], "verification_url": info.get("verification_url") or info.get("verification_uri"),
                 "expires_at": time.time() + float(info.get("expires_in", 1800)), "status": "waiting", "error": None}
        self.login = state
        threading.Thread(target=self._poll, args=(info["device_code"], float(info.get("interval", 5)), state),
                         daemon=True).start()
        return state

    def _poll(self, device_code: str, interval: float, state: dict) -> None:
        while self.login is state and time.time() < state["expires_at"]:
            time.sleep(interval)
            try:
                result = youtube.poll_device_login(self.settings, device_code)
            except (youtube.YouTubeError, youtube.NotConnected, OSError) as e:
                state.update(status="error", error=str(e))
                return
            if result == "done":
                state["status"] = "done"
                return
            if result == "slow_down":
                interval += 5
        if state["status"] == "waiting":
            state.update(status="error", error=f"O código expirou sem a autorização do Google. {youtube.ACCESS_HINT}")


class PostJobs:
    """Publicações pela API em segundo plano (envio do YouTube, processamento do Instagram); uma por vez."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs: dict[tuple[int, str], dict] = {}  # (run_id, rede) → situação
        self.lock = threading.Lock()

    def of(self, run_id: int) -> dict[str, dict]:
        return {net: job for (rid, net), job in self.jobs.items() if rid == run_id}

    def start(self, run_id: int, network: str, work) -> None:
        with self.lock:
            if any(job["status"] == "sending" for job in self.jobs.values()):
                raise RuntimeError("Já há um vídeo sendo publicado; espere terminar.")
            job = {"status": "sending", "progress": 0.0, "message": "Começando", "error": None, "started_at": db.now()}
            self.jobs[(run_id, network)] = job
        threading.Thread(target=self._run, args=(work, job), daemon=True).start()

    @staticmethod
    def _run(work, job: dict) -> None:
        try:
            work(lambda fraction, message=None: job.update(progress=round(fraction, 3),
                                                           **({"message": message} if message else {})))
            job.update(status="done", progress=1.0)
        except Exception as e:  # noqa: BLE001 - qualquer falha vira mensagem na página
            job.update(status="failed", error=str(e))


# endereços que só valem em casa: o próprio PC, IPs da rede local e nomes sem domínio (o Instagram não chega neles)
LOCAL_HOST = re.compile(r"^(localhost|127\.|0\.0\.0\.0|\[|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.|[^.:]+(:\d+)?$)")


def public_base(settings: Settings, request: Request | None = None, *, port: int | None = None,
                fallback: str | None = None) -> str | None:
    """Endereço público do cardline (para o Instagram baixar o vídeo): o configurado, o do túnel pelo qual a
    página foi aberta, o que o ngrok informa na API local dele ou, por último, `fallback`."""
    if settings.public_url:
        return settings.public_url.rstrip("/")
    if request is not None:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
        if host and not LOCAL_HOST.match(host):
            return f"{request.headers.get('x-forwarded-proto', 'https')}://{host}"
        port = request.url.port or port
    try:
        tunnels = json.loads(urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3).read())["tunnels"]
    except (OSError, ValueError, KeyError):
        return fallback
    for t in tunnels:
        if t.get("public_url", "").startswith("https://") and t.get("config", {}).get("addr", "").endswith(f":{port or 8000}"):
            return t["public_url"]
    return fallback


def local_time(value: str | datetime) -> str:
    """Data e hora no fuso do servidor, para as mensagens ("08/10 às 18:00")."""
    when = datetime.fromisoformat(value) if isinstance(value, str) else value
    return when.astimezone().strftime("%d/%m às %H:%M")


def create_app(settings: Settings) -> FastAPI:
    runner = Runner(settings)
    sync_job = SyncJob(settings)
    yt = YouTubeLogin(settings)
    jobs = PostJobs(settings)
    edit_lock = threading.Lock()  # edições do scan.json não podem se intercalar
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    ollama = {"checked": 0.0, "available": False}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        pipeline.recover_interrupted(settings)
        social.interrupted(con())
        runner.start()
        stop = threading.Event()
        threading.Thread(target=agenda, args=(stop,), name="cardline-agenda", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="cardline", lifespan=lifespan)

    def con():
        return db.connect(settings.db_path)

    def icon_url(r) -> str | None:
        """A miniatura (leve) do ícone, ou o próprio ícone se ela ainda não existir."""
        path = settings.root / r["icon"] if r["icon"] else None
        if not path or not path.exists():
            return None
        thumb = path.with_suffix(".thumb.webp")
        shown = thumb if thumb.exists() else path
        return f"/set-icons/{shown.name}?v={int(path.stat().st_mtime)}"

    def card_json(r) -> dict:
        local = image_path(settings, r["set_code"], r["id"])
        return {
            "name": r["name"], "version": r["version"], "set": r["set_code"], "number": r["number"],
            "sort": r["sort_number"], "rarity": r["rarity"], "ink": r["ink"], "type": r["type"], "cost": r["cost"],
            "img": r["image_normal"], "img_large": r["image_large"],
            "local": f"/img/{r['set_code']}/{r['id']}.avif" if local.exists() else None,
            "tcg": r["tcgplayer_url"], "usd": r["usd"], "usd_foil": r["usd_foil"],
        }

    def ollama_available() -> bool:
        if time.monotonic() - ollama["checked"] > 30:
            try:
                urllib.request.urlopen(os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/api/tags", timeout=1).close()
                ollama["available"] = True
            except OSError:
                ollama["available"] = False
            ollama["checked"] = time.monotonic()
        return ollama["available"]

    def pack_values(cards: list[dict], rows: dict) -> list[dict]:
        """Valor de cada booster do vídeo pelas cartas (na abertura e hoje); a coleção é a da maioria das cartas."""
        packs: dict[int, list[dict]] = {}
        for x in cards:
            packs.setdefault(x.get("pack") or 1, []).append(x)
        return [{"pack": p, "set": Counter(x.get("set") or rows[x["card_id"]]["set_code"] for x in xs).most_common(1)[0][0],
                 "cards": len(xs), "value_open": sum(x.get("price_usd") or 0 for x in xs),
                 "value_now": sum(db.price_usd(rows[x["card_id"]], x["foil"]) or 0 for x in xs)}
                for p, xs in sorted(packs.items())]

    card_sets: dict[int, tuple[tuple, frozenset]] = {}  # pipeline → (versão do scan.json, cartas distintas)

    def distinct_cards(run) -> frozenset:
        """As cartas distintas identificadas na pipeline, já com as edições (lidas de novo quando o scan muda)."""
        path = settings.root / run["dir"] / "scan.json"
        try:
            st = path.stat()
        except OSError:
            return frozenset()
        version = (st.st_mtime_ns, st.st_size)  # o tamanho também: duas gravações podem cair no mesmo tique do relógio
        cached = card_sets.get(run["id"])
        if not cached or cached[0] != version:
            cached = card_sets[run["id"]] = (version, frozenset(x["card_id"] for x in load_scan(path.parent)["cards"]))
        return cached[1]

    def duplicates(c) -> dict[int, list[int]]:
        """Pipelines repetidas: as que têm as mesmas cartas (sem contar ordem, foil nem cartas repetidas dentro
        delas), cada uma → as outras do grupo."""
        groups: dict[frozenset, list[int]] = {}
        for r in c.execute("SELECT id, dir FROM runs ORDER BY id"):
            if ids := distinct_cards(r):
                groups.setdefault(ids, []).append(r["id"])
        return {rid: [o for o in g if o != rid] for g in groups.values() if len(g) > 1 for rid in g}

    def run_json(c, run, detail: bool = False, dups: dict[int, list[int]] | None = None) -> dict:
        folder = settings.root / run["dir"]
        url = f"/runs/{folder.name}"
        scan = load_scan(folder) if (folder / "scan.json").exists() else None
        cards = scan["cards"] if scan else []
        repeats = repeated(cards)
        removed = scan.get("removed", []) if scan else []
        ids = sorted({x["card_id"] for x in cards + removed})
        rows = {r["id"]: r for r in c.execute(
            f"SELECT * FROM cards WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        priced = scan is not None and any("price_usd" in x for x in cards)
        steps = {r["name"]: dict(r) for r in c.execute("SELECT * FROM run_steps WHERE run_id = ?", (run["id"],))}
        out = {
            "id": run["id"], "kind": run["kind"], "status": run["status"], "step": run["step"], "progress": run["progress"],
            "message": run["message"], "error": (run["error"] or "").strip().splitlines()[-1:] or None,
            "video_name": run["video_name"], "created_at": run["created_at"], "recorded_at": run["recorded_at"],
            "started_at": run["started_at"], "finished_at": run["finished_at"], "resume_from": run["resume_from"],
            "paid": run["paid"], "paid_currency": run["paid_currency"], "paid_usd": run["paid_usd"],
            "options": json.loads(run["options"]), "sets": scan["sets"] if scan else None,
            "n_cards": len(cards), "packs": max((x["pack"] or 0 for x in cards), default=0),  # cadastro: sem booster
            "value_open": sum(x.get("price_usd") or 0 for x in cards) if priced else None,
            "value_now": sum(db.price_usd(rows[x["card_id"]], x["foil"]) or 0 for x in cards) if cards else None,
            "pack_values": pack_values(cards, rows) if run["kind"] == "abertura" and priced else [],
            "thumbs": [f"{url}/{x['crop']}" for x in cards if x.get("crop")][:24],
            "overlay": f"{url}/overlay.mp4" if (folder / "overlay.mp4").exists() else None,
            "narrated": f"{url}/narrado.mp4" if json.loads(run["options"]).get("narration")
                        and (folder / "narrado.mp4").exists() else None,
            "poster": f"{url}/overlay.jpg" if (folder / "overlay.jpg").exists() else None,
            "posts": social.posts(c, run["id"]), "post_jobs": jobs.of(run["id"]), "scheduled": social.scheduled(c, run["id"]),
            "duplicates": (duplicates(c) if dups is None else dups).get(run["id"], []), "repeated": len(repeats),
            "steps": [{"name": n, "label": pipeline.LABELS[n], **{k: steps.get(n, {}).get(k) for k in
                       ("status", "message", "started_at", "finished_at")}} for n in pipeline.steps_for(run["kind"])],
        }
        if detail:
            out["cards"] = [
                {
                    "n": n, "uid": card_uid(x), "card": x["card_id"], "foil": x["foil"], "foil_reason": x.get("foil_reason"),
                    "pack": x["pack"], "slot": x["slot"], "t": x["t"], "price_open": x.get("price_usd"),
                    "price_now": db.price_usd(rows[x["card_id"]], x["foil"]),
                    "crop": f"{url}/{x['crop']}" if x.get("crop") else None, "inliers": x.get("inliers"),
                    "manual": x.get("manual", False), "check": x.get("check"),
                    "repeat_of": repeats[n - 1] + 1 if n - 1 in repeats else None,  # nº da 1ª aparição da carta
                }
                for n, x in enumerate(cards, 1)
            ]
            out["removed"] = [
                {"uid": card_uid(x), "card": x["card_id"], "foil": x["foil"], "t": x["t"],
                 "crop": f"{url}/{x['crop']}" if x.get("crop") else None, "removed_at": x.get("removed_at")}
                for x in removed
            ]
            out["card_info"] = {cid: card_json(r) for cid, r in rows.items()}
            script = narration.load_script(folder) or {}
            out["narration"] = {"enabled": bool(json.loads(run["options"]).get("narration")),
                                "lines": script.get("lines", []), "source": script.get("source"),
                                "writer": script.get("writer"), "voz": script.get("voz") or settings.narration_voice}
            if scan and run["kind"] == "abertura":
                names = {r["code"]: r["name"] for r in c.execute("SELECT code, name FROM sets")}
                out["post_suggestion"] = social.suggestion(scan, names, settings.youtube_tags)
            log = folder / "pipeline.log"
            lines = log.read_text(errors="replace").splitlines()[-120:] if log.exists() else []
            out["log"] = "\n".join(line.rsplit("\r", 1)[-1] for line in lines)
            out["error_full"] = run["error"]
        return out

    # --- API -----------------------------------------------------------------------------------

    @app.get("/api/meta")
    def meta():
        c = con()
        fx = usd_brl(settings)
        rates = {"USD": 1.0, **({"BRL": fx[0]} if fx else {})}
        return {
            "currency": settings.currency if settings.currency in rates else "USD",
            "rates": rates, "rate_day": fx[1] if fx else None, "pack_size": settings.pack_size,
            "steps": [{"name": n, "label": label} for n, label in pipeline.STEPS],
            "kinds": [{"name": k, "label": label, "steps": names} for k, (label, names) in pipeline.KINDS.items()],
            "sets": [{"code": r["code"], "name": r["name"], "booster": is_booster_set(r["code"]), "icon": icon_url(r)}
                     for r in c.execute("SELECT code, name, icon FROM sets ORDER BY released_at, code")],
            "rarities": [[k, label, color] for k, (label, color) in rarity.RARITIES.items()],
            "inks": [[k, label, color] for k, (label, color) in rarity.INKS.items()],
            "prices_updated_at": c.execute("SELECT MAX(prices_updated_at) FROM cards").fetchone()[0],
            "youtube": youtube.status(settings), "instagram": instagram.status(settings),
            "narration": {"unavailable": narration.unavailable(), "voice": settings.narration_voice,
                          "voices": [{**v, "sample": f"/vozes/{v['file']}" if v.get("file") else None}
                                     for v in narration.voices(settings)],
                          "writer": settings.narration_writer or None},
            "verify": {"default": bool(settings.verify_model),
                       "model": settings.verify_model or pipeline.DEFAULT_VERIFY_MODEL,
                       "available": ollama_available()},
        }

    @app.get("/api/collection")
    def collection():
        c = con()
        rows = c.execute("SELECT * FROM collection ORDER BY added_at, pack, slot").fetchall()
        card_rows = {r["id"]: r for r in c.execute(
            "SELECT * FROM cards WHERE id IN (SELECT card_id FROM collection)")}
        owned: dict = {}
        for r in rows:
            key = (r["card_id"], bool(r["foil"]))
            entry = owned.setdefault(key, {"card": r["card_id"], "foil": key[1], "qty": 0, "copies": [],
                                           "price": db.price_usd(card_rows[r["card_id"]], key[1])})
            entry["qty"] += 1
            entry["copies"].append({"run": r["run_id"], "pack": r["pack"], "slot": r["slot"], "t": r["video_time"],
                                    "paid": r["price_usd"], "added": r["added_at"]})
        return {"cards": {cid: card_json(r) for cid, r in card_rows.items()}, "owned": list(owned.values())}

    @app.get("/api/runs")
    def runs():
        c = con()
        dups = duplicates(c)
        return [run_json(c, r, dups=dups) for r in c.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC")]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int):
        c = con()
        run = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "Pipeline não encontrada.")
        return run_json(c, run, detail=True)

    @app.post("/api/runs", status_code=201)
    async def upload(
        request: Request, filename: str, paid: float | None = None, paid_currency: str = "BRL",
        set_hint: str | None = None, overlay: bool = True, verify: bool | None = None, currency: str | None = None,
        kind: str = "abertura", narration: bool = False,
    ):
        if kind not in pipeline.KINDS:
            raise HTTPException(400, "Tipo de pipeline deve ser abertura ou cadastro.")
        if paid_currency.upper() not in CURRENCIES or (currency and currency.upper() not in CURRENCIES):
            raise HTTPException(400, "Moeda deve ser USD ou BRL.")
        incoming = settings.runs_dir / "_incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        tmp = incoming / f"{uuid.uuid4().hex}.part"
        sha1, size = hashlib.sha1(), 0
        try:
            with open(tmp, "wb") as f:  # direto para o disco: vídeos 4K passam de 1 GB
                async for chunk in request.stream():
                    f.write(chunk)
                    sha1.update(chunk)
                    size += len(chunk)
            expected = request.headers.get("content-length")
            if size == 0 or (expected and int(expected) != size):
                raise HTTPException(400, "Upload vazio ou incompleto.")
            try:
                probe(tmp)
            except SystemExit as e:
                raise HTTPException(400, "O arquivo não parece ser um vídeo.") from e
            try:
                run_id = pipeline.create_run(
                    settings, tmp, video_name=Path(filename).name, sha1=sha1.hexdigest(), paid=paid,
                    paid_currency=paid_currency, set_hint=set_hint, overlay=overlay, verify=verify,
                    currency=currency, move=True, kind=kind, narration=narration,
                )
            except pipeline.DuplicateVideo as e:
                raise HTTPException(409, {"message": str(e), "run_id": e.run_id}) from e
        finally:
            tmp.unlink(missing_ok=True)
        runner.wake()
        return {"id": run_id}

    @app.post("/api/runs/{run_id}/rerun")
    def rerun(run_id: int, body: RerunBody):
        try:
            pipeline.enqueue(settings, run_id, body.from_step)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e)) from e
        runner.wake()
        return {"ok": True}

    def editable_run(run_id: int):
        run = con().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "Pipeline não encontrada.")
        if run["status"] in ("queued", "running"):
            raise HTTPException(409, "A pipeline está rodando; espere terminar para editar.")
        return settings.root / run["dir"]

    def narrated_run(run_id: int) -> Path:
        folder = editable_run(run_id)
        options = json.loads(con().execute("SELECT options FROM runs WHERE id = ?", (run_id,)).fetchone()[0])
        if not options.get("narration"):
            raise HTTPException(409, "A narração desta pipeline está desligada.")
        return folder

    @app.delete("/api/runs/{run_id}/cards/{uid}")
    def delete_card(run_id: int, uid: str):
        """Tira uma carta identificada errada ou duplicada; vale para a coleção e o vídeo ao reprocessar."""
        with edit_lock:
            try:
                remove_card(settings, editable_run(run_id), uid)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            pipeline.mark_stale(settings, run_id, "prices")
        return {"ok": True}

    @app.patch("/api/runs/{run_id}/cards/{uid}")
    def edit_card(run_id: int, uid: str, body: CardPatch):
        """Edita uma carta identificada (por enquanto, se é foil); vale ao reprocessar."""
        if body.foil is None:
            raise HTTPException(400, "Nada para editar.")
        with edit_lock:
            try:
                changed = set_card_foil(settings, editable_run(run_id), uid, body.foil)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            if changed:
                pipeline.mark_stale(settings, run_id, "prices")
        return {"ok": True, "changed": changed}

    @app.post("/api/runs/{run_id}/cards/{uid}/restore")
    def restore(run_id: int, uid: str):
        with edit_lock:
            try:
                restore_card(settings, editable_run(run_id), uid)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            pipeline.mark_stale(settings, run_id, "prices")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/sanitize")
    def sanitize(run_id: int):
        """Tira as cartas repetidas, deixando a primeira aparição de cada uma; vale ao reprocessar."""
        with edit_lock:
            gone = remove_repeated(settings, editable_run(run_id))
            if gone:
                pipeline.mark_stale(settings, run_id, "prices")
        return {"removed": len(gone)}

    @app.patch("/api/runs/{run_id}")
    def update_run(run_id: int, body: RunPatch):
        if any(c is not None and c.upper() not in CURRENCIES for c in (body.paid_currency, body.currency)):
            raise HTTPException(400, "Moeda deve ser USD ou BRL.")
        if con().execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone() is None:
            raise HTTPException(404, "Pipeline não encontrada.")
        sent = body.model_fields_set
        try:
            if "paid" in sent:
                pipeline.update_paid(settings, run_id, body.paid, body.paid_currency or "BRL")
            if "currency" in sent and body.currency:
                pipeline.update_overlay_currency(settings, run_id, body.currency.upper())
            if "narration" in sent and body.narration is not None:
                pipeline.update_narration(settings, run_id, body.narration)
        except ValueError as e:  # cadastro não tem valor pago nem vídeo
            raise HTTPException(400, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.put("/api/runs/{run_id}/narration")
    def save_script(run_id: int, body: ScriptBody):
        """Roteiro editado na página: vale na próxima narração (só as falas que mudaram são gravadas de novo)."""
        folder = narrated_run(run_id)
        try:
            narration.edit_script(folder, body.lines, body.voz)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        pipeline.mark_stale(settings, run_id, "narrate")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/narration/new")
    def new_script(run_id: int):
        """Descarta o roteiro: a próxima narração escreve outro."""
        narration.discard_script(narrated_run(run_id))
        pipeline.mark_stale(settings, run_id, "narrate")
        return {"ok": True}

    # --- redes: YouTube, Instagram, TikTok ------------------------------------------------------

    def net_call(fn, *args, **kwargs):
        """Chama a API de uma rede traduzindo os erros para a página."""
        try:
            return fn(*args, **kwargs)
        except (youtube.NotConnected, instagram.NotConnected) as e:
            raise HTTPException(409, str(e)) from e
        except (youtube.YouTubeError, instagram.InstagramError) as e:
            raise HTTPException(502, str(e)) from e
        except OSError as e:
            raise HTTPException(502, f"Sem conexão com a rede ({e}).") from e

    @app.get("/api/youtube")
    def youtube_status():
        login = yt.login
        return {**youtube.status(settings),
                "login": {k: login[k] for k in ("user_code", "verification_url", "expires_at", "status", "error")}
                if login else None}

    @app.post("/api/youtube/client")
    def youtube_client(body: ClientBody):
        try:
            youtube.save_client(settings, body.client_id, body.client_secret)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return youtube.status(settings)

    @app.post("/api/youtube/connect")
    def youtube_connect():
        """Pede um código ao Google; a página mostra o código e espera o usuário autorizar."""
        state = net_call(yt.start)
        return {k: state[k] for k in ("user_code", "verification_url", "expires_at", "status")}

    @app.post("/api/youtube/disconnect")
    def youtube_disconnect():
        yt.login = None
        youtube.disconnect(settings)
        return youtube.status(settings)

    @app.get("/api/youtube/recent")
    def youtube_recent():
        """Os últimos vídeos do canal, para vincular o que foi postado pelo app."""
        return net_call(youtube.recent_uploads, settings)

    @app.get("/api/instagram")
    def instagram_status():
        return instagram.status(settings)

    @app.post("/api/instagram/token")
    def instagram_token(body: TokenBody):
        """Token gerado no painel da Meta (conta profissional testadora do app)."""
        try:
            return net_call(instagram.save_token, settings, body.token)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/instagram/disconnect")
    def instagram_disconnect():
        instagram.disconnect(settings)
        return instagram.status(settings)

    def refresh_numbers(c, only_run: int | None = None) -> int:
        """Lê os números de cada post pela API da rede (YouTube e Instagram conectados); devolve quantos leu."""
        n = 0
        if youtube.status(settings)["connected"]:
            posts = [(r, v) for r, v in social.linked(c, "youtube") if only_run in (None, r)]
            if posts:
                found = net_call(youtube.stats, settings, [v for _, v in posts])
                for run_id, vid in posts:
                    if info := found.get(vid):
                        social.save_stats(c, run_id, "youtube", info)
                        social.update_post(c, run_id, "youtube", title=info["title"], privacy=info["privacy"],
                                           published_at=info["published_at"], scheduled_at=info["scheduled_at"])
                        n += 1
        if instagram.status(settings)["connected"]:
            posts = [(r, m) for r, m in social.linked(c, "instagram") if only_run in (None, r)]
            if posts:
                try:
                    instagram.refresh(settings)  # renova o token de 60 dias com folga
                except (instagram.InstagramError, OSError):
                    pass
                found = net_call(instagram.stats, settings, [m for _, m in posts])
                for run_id, media in posts:
                    if info := found.get(media):
                        social.save_stats(c, run_id, "instagram", info)
                        social.update_post(c, run_id, "instagram", published_at=info["published_at"])
                        n += 1
        return n

    def last_reads(c, only_run: int | None = None) -> list[str | None]:
        """A última leitura pela API de cada post que dá para ler (rede conectada e ID na rede); None = nunca lido."""
        nets = [n for n, mod in (("youtube", youtube), ("instagram", instagram)) if mod.status(settings)["connected"]]
        if not nets:
            return []
        return [r[0] for r in c.execute(
            "SELECT (SELECT MAX(fetched_at) FROM post_stats s WHERE s.run_id = p.run_id AND s.network = p.network"
            f" AND s.manual = 0) FROM posts p WHERE p.post_id IS NOT NULL AND p.network IN ({','.join('?' * len(nets))})"
            " AND (? IS NULL OR p.run_id = ?)", (*nets, only_run, only_run))]

    @app.post("/api/social/stats")
    def social_stats(max_age: float = 0, run: int | None = None):
        """Atualiza os números dos posts pela API. Com `max_age`, só se algum post estiver com a leitura mais velha
        que isso (ou sem leitura): ler um post não deixa os outros parecendo atualizados."""
        c = con()
        reads = last_reads(c, run)
        if max_age and reads and all(r and time.time() - datetime.fromisoformat(r).timestamp() < max_age for r in reads):
            return {"updated": 0, "fresh": True}
        return {"updated": refresh_numbers(c, run)}

    def opening(run_id: int):
        run = con().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            raise HTTPException(404, "Pipeline não encontrada.")
        if run["kind"] != "abertura":
            raise HTTPException(400, "Só aberturas de booster têm vídeo para postar.")
        return run

    def video_of(run, variant: str) -> tuple[Path, str]:
        folder = settings.root / run["dir"]
        narrated = folder / "narrado.mp4"
        if variant == "narrado" and narrated.exists() and json.loads(run["options"]).get("narration"):
            return narrated, "narrado"
        if not (folder / "overlay.mp4").exists():
            raise HTTPException(409, "Gere o vídeo com overlay antes de postar.")
        return folder / "overlay.mp4", "overlay"

    def publish_time(value: str | None) -> datetime | None:
        """A data de programar a publicação (ISO 8601); sem fuso, vale o do servidor."""
        if not value:
            return None
        try:
            when = datetime.fromisoformat(value).astimezone()
        except ValueError as e:
            raise HTTPException(400, "Data de publicação inválida.") from e
        if when < datetime.now().astimezone() + timedelta(minutes=5):
            raise HTTPException(400, "Escolha uma data pelo menos 5 minutos no futuro.")
        return when

    def start_instagram(run, variant: str, caption: str, base: str, scheduled: bool = False) -> None:
        """Publica o Reel em segundo plano; o Instagram baixa o vídeo pelo endereço público `base`."""
        run_id = run["id"]
        video, variant = video_of(run, variant)
        video_url = f"{base}/runs/{video.parent.name}/{video.name}?v={int(video.stat().st_mtime)}"
        caption = f"{caption}\n\n{social.HASHTAGS['instagram']}".strip()

        def work(progress):
            c = db.connect(settings.db_path)
            try:
                media = instagram.publish_reel(settings, video_url, caption, progress)
            except Exception as e:
                if scheduled:
                    social.set_schedule(c, run_id, "instagram", "failed", str(e))
                raise
            social.save_post(c, run_id, "instagram", media["id"], media.get("permalink") or "https://www.instagram.com/",
                             via="api", variant=variant, title=(media.get("caption") or "")[:120] or None,
                             privacy="public", published_at=media.get("timestamp"))
            social.unschedule(c, run_id, "instagram")
            try:
                refresh_numbers(c, run_id)
            except HTTPException:
                pass

        jobs.start(run_id, "instagram", work)

    @app.post("/api/runs/{run_id}/posts/{network}")
    def post_video(run_id: int, network: str, body: PostBody, request: Request):
        """Publica pela API da rede, em segundo plano (YouTube: envio do arquivo; Instagram: o Reel pelo túnel).

        Com `publish_at`, programa: o YouTube recebe o vídeo agora e publica sozinho na data; o Instagram não
        programa pela API, então o cardline guarda o pedido e publica na hora (agenda)."""
        run = opening(run_id)
        if network not in ("youtube", "instagram"):
            raise HTTPException(400, "Pela API, só YouTube e Instagram; o TikTok é pelo app (e depois vincule o link).")
        c = con()
        if network in social.posts(c, run_id):
            raise HTTPException(409, f"Esta pipeline já tem um post no {social.NETWORKS[network]}; desvincule para postar de novo.")
        pending = social.scheduled(c, run_id).get(network)
        if pending and pending["status"] != "failed":
            raise HTTPException(409, f"Esta pipeline já tem publicação programada no {social.NETWORKS[network]} "
                                     f"({local_time(pending['publish_at'])}); cancele para mudar.")
        when = publish_time(body.publish_at)
        video, variant = video_of(run, body.variant)
        if network == "youtube":
            if body.privacy not in youtube.PRIVACY:
                raise HTTPException(400, "Visibilidade deve ser public, unlisted ou private.")
            if not body.title.strip():
                raise HTTPException(400, "O vídeo precisa de um título.")
            if not youtube.status(settings)["connected"]:
                raise HTTPException(409, "Conecte o canal do YouTube.")
            caption = f"{body.caption}\n\n{social.HASHTAGS['youtube']}".strip()
            tags = body.tags
            if not tags:
                folder = settings.root / run["dir"]
                scan = load_scan(folder) if (folder / "scan.json").exists() else {"cards": []}
                names = {r["code"]: r["name"] for r in c.execute("SELECT code, name FROM sets")}
                tags = social.suggestion(scan, names, settings.youtube_tags)["tags"]

            def work(progress):
                created = youtube.upload(settings, video, body.title, caption, tags, body.privacy,
                                         progress=lambda f: progress(f, "Enviando o vídeo"), publish_at=when)
                c = db.connect(settings.db_path)
                status = created.get("status", {})
                social.save_post(c, run_id, "youtube", created["id"], youtube.url(created["id"]), via="api", variant=variant,
                                 title=created.get("snippet", {}).get("title"), privacy=status.get("privacyStatus"),
                                 published_at=created.get("snippet", {}).get("publishedAt"),
                                 scheduled_at=status.get("publishAt"))
                try:  # a visibilidade de verdade (projeto sem auditoria: travado como privado)
                    refresh_numbers(c, run_id)
                except HTTPException:
                    pass

            try:
                jobs.start(run_id, network, work)
            except RuntimeError as e:
                raise HTTPException(409, str(e)) from e
            return {"ok": True}
        if not instagram.status(settings)["connected"]:
            raise HTTPException(409, "Conecte o Instagram (cole o token gerado no painel da Meta).")
        base = public_base(settings, request)
        if when:  # na hora, o endereço vem do public_url ou do ngrok; o de agora fica de reserva
            at = when.isoformat(timespec="seconds")
            social.schedule(c, run_id, network, at, {"caption": body.caption, "variant": variant, "base": base,
                                                     "port": request.url.port})
            return {"ok": True, "scheduled": at}
        if not base:
            raise HTTPException(409, "O Instagram baixa o vídeo de um endereço público: abra a página pelo túnel "
                                     "(ngrok) ou configure public_url no cardline.toml.")
        try:
            start_instagram(run, variant, body.caption, base)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        social.unschedule(c, run_id, network)  # a programação que tinha falhado (se houver) dá lugar a este post
        return {"ok": True}

    def publish_due(now: datetime | None = None) -> None:
        """Publica o que foi programado para agora (Instagram). O que não der para publicar ainda (túnel fechado,
        Instagram desconectado) espera com o motivo e tenta de novo a cada ciclo; 1 h depois da hora, desiste."""
        now = now or datetime.now().astimezone()
        c = con()
        for s in social.due(c, now):
            run_id, network = s["run_id"], s["network"]
            if network in social.posts(c, run_id):  # postou pelo app e vinculou: não publica de novo
                social.unschedule(c, run_id, network)
                continue
            at = datetime.fromisoformat(s["publish_at"])
            if (now - at).total_seconds() > social.LATE_LIMIT:
                social.set_schedule(c, run_id, network, "failed",
                                    f"Não publicou até 1 h depois da hora marcada: {s['error']}" if s["error"] else
                                    f"O cardline estava desligado na hora marcada ({local_time(at)}).")
                continue
            req = json.loads(s["request"])
            base = public_base(settings, port=req.get("port"), fallback=req.get("base"))
            problem = ("O Instagram está desconectado: conecte de novo para publicar."
                       if not instagram.status(settings)["connected"] else
                       None if base else "Sem endereço público: o túnel (ngrok) está fechado.")
            if problem:
                social.set_schedule(c, run_id, network, "waiting", problem)
                continue
            social.set_schedule(c, run_id, network, "sending")
            try:
                start_instagram(opening(run_id), req["variant"], req["caption"], base, scheduled=True)
            except RuntimeError:  # outro vídeo sendo publicado: tenta no próximo ciclo
                social.set_schedule(c, run_id, network, "waiting", s["error"])
                return
            except HTTPException as e:
                social.set_schedule(c, run_id, network, "failed", e.detail)

    def agenda(stop: threading.Event) -> None:
        """A cada 30 s, publica as programações que chegaram na hora."""
        while True:
            try:
                publish_due()
            except Exception as e:  # noqa: BLE001 - o laço não pode parar
                print(f"agenda: {e}", file=sys.stderr, flush=True)
            if stop.wait(30):
                return

    app.state.publish_due = publish_due

    @app.delete("/api/runs/{run_id}/scheduled/{network}")
    def cancel_scheduled(run_id: int, network: str):
        """Cancela a publicação que o cardline faria na hora marcada (ou descarta a que falhou)."""
        c = con()
        if (social.scheduled(c, run_id).get(network) or {}).get("status") == "sending":
            raise HTTPException(409, "A publicação já começou; espere terminar.")
        social.unschedule(c, run_id, network)
        return {"ok": True}

    @app.put("/api/runs/{run_id}/posts")
    def link_post(run_id: int, body: LinkBody):
        """Vincula um post já feito (YouTube, Instagram ou TikTok) a esta abertura, pelo link."""
        opening(run_id)
        found = social.detect(body.url)
        if not found:
            raise HTTPException(400, "Não reconheci o link: use o link do vídeo no YouTube, do Reel ou do TikTok.")
        network, post_id, url = found
        c = con()
        if network == "youtube" and youtube.status(settings)["connected"]:
            info = net_call(youtube.stats, settings, [post_id]).get(post_id)
            if info is None:
                raise HTTPException(404, "O YouTube não encontrou esse vídeo (ou ele é privado de outra conta).")
            social.save_post(c, run_id, network, post_id, url, via="link", title=info["title"], privacy=info["privacy"],
                             published_at=info["published_at"], scheduled_at=info["scheduled_at"])
            social.save_stats(c, run_id, network, info)
        elif network == "instagram" and instagram.status(settings)["connected"]:
            media = net_call(instagram.find_media, settings, post_id)  # o link traz o código; a API usa o ID da mídia
            social.save_post(c, run_id, network, media["id"] if media else None, url, via="link",
                             title=((media or {}).get("caption") or "")[:120] or None,
                             published_at=(media or {}).get("timestamp"))
            if media:
                refresh_numbers(c, run_id)
        else:
            social.save_post(c, run_id, network, None if network == "instagram" else post_id, url, via="link")
        if (social.scheduled(c, run_id).get(network) or {}).get("status") != "sending":
            social.unschedule(c, run_id, network)  # já postou: a publicação programada não acontece mais
        return {"ok": True, "network": network}

    @app.patch("/api/runs/{run_id}/posts/{network}")
    def post_numbers(run_id: int, network: str, body: StatsBody):
        """Números informados à mão (o TikTok não dá os números sem a aprovação do app)."""
        c = con()
        if network not in social.posts(c, run_id):
            raise HTTPException(404, "Vincule o post antes de informar os números.")
        numbers = body.model_dump()
        if all(v is None for v in numbers.values()) or any(v is not None and v < 0 for v in numbers.values()):
            raise HTTPException(400, "Informe os números (não negativos).")
        social.save_stats(c, run_id, network, numbers, manual=True)
        return {"ok": True}

    @app.delete("/api/runs/{run_id}/posts/{network}")
    def unlink_post(run_id: int, network: str):
        """Desvincula o post desta abertura (ele continua na rede)."""
        c = con()
        with c:
            c.execute("DELETE FROM posts WHERE run_id = ? AND network = ?", (run_id, network))
        jobs.jobs.pop((run_id, network), None)
        return {"ok": True}

    @app.post("/api/prices/refresh")
    def refresh_current_prices():
        """Busca os preços de hoje dos sets da coleção; o preço na abertura de cada carta não muda."""
        sets = [r[0] for r in con().execute(
            "SELECT DISTINCT cards.set_code FROM collection JOIN cards ON cards.id = collection.card_id")]
        try:
            return refresh_prices(settings, sets)
        except OSError as e:
            raise HTTPException(502, f"Não consegui buscar os preços no Lorcast ({e}).") from e

    @app.delete("/api/runs/{run_id}")
    def delete(run_id: int):
        try:
            pipeline.delete_run(settings, run_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    @app.get("/api/sets")
    def sets_list():
        """Sets do catálogo com ícone, cartas no catálogo, na coleção e se já são reconhecidos em vídeo."""
        c = con()
        catalog = {r["set_code"]: r["n"] for r in c.execute("SELECT set_code, COUNT(*) AS n FROM cards GROUP BY set_code")}
        owned = {r["set_code"]: (r["copies"], r["unique_cards"]) for r in c.execute(
            "SELECT cards.set_code, COUNT(*) AS copies, COUNT(DISTINCT collection.card_id) AS unique_cards"
            " FROM collection JOIN cards ON cards.id = collection.card_id GROUP BY cards.set_code")}
        indexed = set(indexed_sets(settings))
        out = []
        for r in c.execute("SELECT * FROM sets ORDER BY released_at DESC, code"):
            icon = icon_url(r)
            copies, unique = owned.get(r["code"], (0, 0))
            out.append({"code": r["code"], "name": r["name"], "released_at": r["released_at"],
                        "booster": is_booster_set(r["code"]), "icon": icon, "icon_source": r["icon_source"] if icon else None,
                        "cards": catalog.get(r["code"], 0), "owned": copies, "owned_unique": unique,
                        "recognized": r["code"] in indexed})
        return out

    @app.get("/api/sets/sync")
    def sync_status():
        return sync_job.status()

    @app.post("/api/sets/sync")
    def sync_start():
        try:
            sync_job.start()
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return sync_job.status()

    @app.post("/api/sets/{code}/icon")
    async def set_icon(code: str, request: Request):
        data = await request.body()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, "Imagem grande demais (máximo 8 MB).")
        try:
            save_manual_icon(settings, code, data)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True}

    @app.delete("/api/sets/{code}/icon")
    def unset_icon(code: str):
        try:
            reset_icon(settings, code)
        except (OSError, ValueError, KeyError) as e:
            raise HTTPException(502, f"Ícone removido, mas não consegui buscar o automático ({e}).") from e
        return {"ok": True}

    # --- arquivos ------------------------------------------------------------------------------

    app.mount("/runs", StaticFiles(directory=settings.runs_dir), name="runs")
    app.mount("/img", StaticFiles(directory=settings.images_dir, check_dir=False), name="img")
    (settings.cache_dir / "sets").mkdir(parents=True, exist_ok=True)
    narration.samples_dir(settings).mkdir(parents=True, exist_ok=True)
    app.mount("/vozes", StaticFiles(directory=narration.samples_dir(settings)), name="vozes")
    app.mount("/set-icons", StaticFiles(directory=settings.cache_dir / "sets"), name="set-icons")
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


def serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    print(f"cardline em http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
