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
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, instagram, narration, pipeline, rarity, social, youtube
from .catalog import is_booster_set, refresh_prices, reset_icon, save_manual_icon
from .collection import card_uid, load_scan, remove_card, restore_card, set_card_foil
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


def public_base(settings: Settings, request: Request) -> str | None:
    """Endereço público do cardline (para o Instagram baixar o vídeo): o configurado, o do túnel pelo qual a
    página foi aberta ou o que o ngrok informa na API local dele."""
    if settings.public_url:
        return settings.public_url.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if host and not re.match(r"^(localhost|127\.|0\.0\.0\.0|\[::1\])", host):
        return f"{request.headers.get('x-forwarded-proto', 'https')}://{host}"
    try:
        tunnels = json.loads(urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=3).read())["tunnels"]
    except (OSError, ValueError, KeyError):
        return None
    port = str(request.url.port or 8000)
    for t in tunnels:
        if t.get("public_url", "").startswith("https://") and t.get("config", {}).get("addr", "").endswith(f":{port}"):
            return t["public_url"]
    return None


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
        runner.start()
        yield

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

    def run_json(c, run, detail: bool = False) -> dict:
        folder = settings.root / run["dir"]
        url = f"/runs/{folder.name}"
        scan = load_scan(folder) if (folder / "scan.json").exists() else None
        cards = scan["cards"] if scan else []
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
            "posts": social.posts(c, run["id"]), "post_jobs": jobs.of(run["id"]),
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
        return [run_json(c, r) for r in c.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC")]

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
                                           published_at=info["published_at"])
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

    @app.post("/api/social/stats")
    def social_stats(max_age: float = 0, run: int | None = None):
        """Atualiza os números dos posts (se a última leitura automática tiver mais de `max_age` s)."""
        c = con()
        last = c.execute("SELECT MAX(fetched_at) FROM post_stats WHERE manual = 0").fetchone()[0]
        if max_age and last and time.time() - datetime.fromisoformat(last).timestamp() < max_age:
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

    @app.post("/api/runs/{run_id}/posts/{network}")
    def post_video(run_id: int, network: str, body: PostBody, request: Request):
        """Publica pela API da rede, em segundo plano (YouTube: envio do arquivo; Instagram: o Reel pelo túnel)."""
        run = opening(run_id)
        if network not in ("youtube", "instagram"):
            raise HTTPException(400, "Pela API, só YouTube e Instagram; o TikTok é pelo app (e depois vincule o link).")
        if network in social.posts(con(), run_id):
            raise HTTPException(409, f"Esta pipeline já tem um post no {social.NETWORKS[network]}; desvincule para postar de novo.")
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
                names = {r["code"]: r["name"] for r in con().execute("SELECT code, name FROM sets")}
                tags = social.suggestion(scan, names, settings.youtube_tags)["tags"]

            def work(progress):
                created = youtube.upload(settings, video, body.title, caption, tags, body.privacy,
                                         progress=lambda f: progress(f, "Enviando o vídeo"))
                c = db.connect(settings.db_path)
                social.save_post(c, run_id, "youtube", created["id"], youtube.url(created["id"]), via="api", variant=variant,
                                 title=created.get("snippet", {}).get("title"),
                                 privacy=created.get("status", {}).get("privacyStatus"),
                                 published_at=created.get("snippet", {}).get("publishedAt"))
                try:  # a visibilidade de verdade (projeto sem auditoria: travado como privado)
                    refresh_numbers(c, run_id)
                except HTTPException:
                    pass
        else:
            if not instagram.status(settings)["connected"]:
                raise HTTPException(409, "Conecte o Instagram (cole o token gerado no painel da Meta).")
            base = public_base(settings, request)
            if not base:
                raise HTTPException(409, "O Instagram baixa o vídeo de um endereço público: abra a página pelo túnel "
                                         "(ngrok) ou configure public_url no cardline.toml.")
            video_url = f"{base}/runs/{video.parent.name}/{video.name}?v={int(video.stat().st_mtime)}"
            caption = f"{body.caption}\n\n{social.HASHTAGS['instagram']}".strip()

            def work(progress):
                media = instagram.publish_reel(settings, video_url, caption, progress)
                c = db.connect(settings.db_path)
                social.save_post(c, run_id, "instagram", media["id"], media.get("permalink") or "https://www.instagram.com/",
                                 via="api", variant=variant, title=(media.get("caption") or "")[:120] or None,
                                 privacy="public", published_at=media.get("timestamp"))
                try:
                    refresh_numbers(c, run_id)
                except HTTPException:
                    pass
        try:
            jobs.start(run_id, network, work)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
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
                             published_at=info["published_at"])
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
