"""Servidor local: página da coleção/pipelines, upload de vídeos e execução das pipelines em fila.

Cada pipeline roda num processo próprio (`cardline run <id>`), um por vez; o progresso vai para o
banco e a página acompanha por polling. Se o servidor cair, os runs em andamento ficam
'interrupted' e podem ser retomados de onde pararam.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from http.cookies import SimpleCookie

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, db, instagram, jobs as job_log, logo, narration, pipeline, rarity, sealed, social, youtube
from . import packs as packs_mod
from .catalog import is_booster_set, refresh_prices, reset_icon, save_manual_icon
from .collection import card_uid, load_scan, remove_card, remove_repeated, repeated, restore_card, set_card_foil
from .config import Settings
from .index import image_path, indexed_sets
from .money import CURRENCIES, to_usd, usd_brl
from .video import probe

WEB = Path(__file__).parent / "web"
# cada parte do envio de um vídeo: o Cloudflare (plano grátis) recusa requisições com mais de 100 MB
UPLOAD_CHUNK = 32 * 1024 * 1024


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
        self.job: int | None = None  # a linha desta sincronização na aba Pipelines

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, user_id: int | None = None) -> None:
        if self.running:
            raise RuntimeError("A sincronização já está rodando.")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "wb") as log:
            self.proc = subprocess.Popen(
                [sys.executable, "-m", "cardline", "sync"], cwd=self.settings.root, stdout=log,
                stderr=subprocess.STDOUT, env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        self.started_at, self.finished_at, self.code = db.now(), None, None
        self.job = job_log.create(db.connect(self.settings.db_path), "sync", user_id)
        threading.Thread(target=self._watch, args=(self.proc, self.job), daemon=True).start()

    def _watch(self, proc: subprocess.Popen, job: int) -> None:
        """Quando o `cardline sync` termina, a linha dele na aba Pipelines ganha o resultado e o log."""
        code = proc.wait()
        log = self.log_path.read_text(errors="replace") if self.log_path.exists() else ""
        lines = [line.rsplit("\r", 1)[-1] for line in log.splitlines() if line.strip()]
        new_sets = [line.strip() for line in lines if line.startswith("  ") and "cartas" in line]
        job_log.finish(db.connect(self.settings.db_path), job, "done" if code == 0 else "failed",
                       "Sincronizado." if code == 0 else (lines[-1] if lines else f"O sync terminou com código {code}."),
                       {"sets": len(new_sets)}, "\n".join(lines[-400:]))

    def status(self) -> dict:
        if self.proc is not None and not self.running and self.finished_at is None:
            self.finished_at, self.code = db.now(), self.proc.returncode
        lines = self.log_path.read_text(errors="replace").splitlines()[-40:] if self.log_path.exists() else []
        return {
            "running": self.running, "started_at": self.started_at, "finished_at": self.finished_at,
            "ok": None if self.code is None else self.code == 0,
            "log": "\n".join(line.rsplit("\r", 1)[-1] for line in lines),
        }


class Task:
    """Tarefa de fundo (preços, números das redes): roda numa thread, uma de cada vez por tipo. O Resumo acompanha
    pelo /api/tasks, e a execução disparada pelo botão vira uma linha na aba Pipelines (tabela jobs)."""

    def __init__(self, settings: Settings, kind: str, user_id: int | None = None):
        self.settings, self.kind, self.user_id = settings, kind, user_id
        self.lock = threading.Lock()
        self.state = {"running": False, "started_at": None, "finished_at": None, "ok": None, "message": None,
                      "progress": None, "job": None}

    def start(self, work, record: bool = True) -> dict:
        """`work(progress)` devolve a mensagem do fim, ou (mensagem, resultado) para guardar na linha."""
        with self.lock:
            if self.state["running"]:
                raise RuntimeError("Já está rodando; espere terminar.")
            job = job_log.create(db.connect(self.settings.db_path), self.kind, self.user_id) if record else None
            self.state = {"running": True, "started_at": db.now(), "finished_at": None, "ok": None,
                          "message": "Começando", "progress": 0.0, "job": job}
        threading.Thread(target=self._run, args=(work, job), daemon=True).start()
        return dict(self.state)

    def _run(self, work, job: int | None) -> None:
        def progress(fraction: float, message: str | None = None) -> None:
            self.state.update(progress=round(fraction, 3), **({"message": message} if message else {}))

        try:
            out = work(progress)
            message, result = out if isinstance(out, tuple) else (out, None)
            self.state.update(running=False, ok=True, progress=1.0, message=message, finished_at=db.now())
            status = "done"
        except Exception as e:  # noqa: BLE001 - qualquer falha vira mensagem na página
            message, result = e.detail if isinstance(e, HTTPException) else str(e), None
            self.state.update(running=False, ok=False, message=message, finished_at=db.now())
            status = "failed"
        if job:
            job_log.finish(db.connect(self.settings.db_path), job, status, message, result)


class SealedBody(BaseModel):
    set_code: str
    product_id: int  # produto no TCGplayer (da lista /api/sealed/products)
    qty: int = 1
    paid: float | None = None  # por unidade
    paid_currency: str = "BRL"


class SealedPatch(BaseModel):
    qty: int | None = None
    paid: float | None = None  # por unidade; null apaga
    paid_currency: str | None = None


class LoginBody(BaseModel):
    id: str  # o pedido de entrada (o código mostrado na página)


class ShareBody(BaseModel):
    email: str  # quem pode ver


class UploadBody(BaseModel):
    filename: str
    size: int  # bytes do vídeo inteiro


class PackBody(BaseModel):
    set: str | None = None  # o set do booster (registro de lacrados)
    t: float | None = None  # só para incluir um booster que faltou: o instante no vídeo
    value: float | None = None  # valor editado do booster; null volta ao preço de mercado
    currency: str = "BRL"  # moeda de `value`


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
    logo: str | None = None  # logo do vídeo (nome em data/logos); null tira


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
    """Conexão do canal do YouTube de cada usuário: o fluxo de dispositivo espera ele digitar o código em
    google.com/device."""

    def __init__(self):
        self.logins: dict[int, dict] = {}  # usuário → código mostrado na página e situação da espera

    def of(self, user_id: int) -> dict | None:
        return self.logins.get(user_id)

    def forget(self, user_id: int) -> None:
        self.logins.pop(user_id, None)

    def start(self, account: Settings) -> dict:
        info = youtube.start_device_login(account)
        state = {"user_code": info["user_code"], "verification_url": info.get("verification_url") or info.get("verification_uri"),
                 "expires_at": time.time() + float(info.get("expires_in", 1800)), "status": "waiting", "error": None}
        self.logins[account.account] = state
        threading.Thread(target=self._poll, args=(account, info["device_code"], float(info.get("interval", 5)), state),
                         daemon=True).start()
        return state

    def _poll(self, account: Settings, device_code: str, interval: float, state: dict) -> None:
        while self.logins.get(account.account) is state and time.time() < state["expires_at"]:
            time.sleep(interval)
            try:
                result = youtube.poll_device_login(account, device_code)
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


def session_cookie(header: str | None) -> str | None:
    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except Exception:  # noqa: BLE001 - cookie malformado de outro site: sem sessão
        return None
    return jar[auth.COOKIE].value if auth.COOKIE in jar else None


class AuthGate:
    """Toda a API, menos /api/auth, exige uma sessão. O cabeçalho X-Cardline-Owner mostra os dados de quem
    compartilhou com o usuário, e aí só leitura: qualquer ação é recusada."""

    def __init__(self, app, settings: Settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/") or path.startswith("/api/auth/"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        con = db.connect(self.settings.db_path)
        user = auth.request_user(con, self.settings, session_cookie(headers.get("cookie")))
        if user is None:
            return await JSONResponse({"detail": "Entre com a sua conta do Google."}, 401)(scope, receive, send)
        owner, view = user.id, headers.get("x-cardline-owner", "")
        if view.isdigit() and int(view) != user.id:
            if scope["method"] not in ("GET", "HEAD"):
                return await JSONResponse({"detail": "Você está vendo a coleção de outra pessoa: só visualização."},
                                          403)(scope, receive, send)
            if not auth.can_view(con, user, int(view)):
                return await JSONResponse({"detail": "Essa coleção não foi compartilhada com você."}, 403)(scope, receive, send)
            owner = int(view)
        token = auth.CURRENT.set(auth.Viewer(user, owner))
        try:
            await self.app(scope, receive, send)
        finally:
            auth.CURRENT.reset(token)


class CacheRules:
    """Cache-Control de cada resposta, para o navegador e para quem está na frente (o Cloudflare guarda vídeos e
    imagens por padrão): os arquivos com dono nunca ficam num cache compartilhado, a API não fica em cache nenhum e
    a página revalida a cada abertura, então uma atualização aparece na hora."""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def rule(path: str) -> str | None:
        if path.startswith("/api/"):
            return "no-store"
        if path.startswith(("/runs/", "/logos/")):
            return "private, no-cache"
        if path.startswith(("/img/", "/set-icons/", "/vozes/")):
            return None  # catálogo público: o cache comum serve
        return "no-cache"  # a página: index.html, app.js, app.css

    async def __call__(self, scope, receive, send):
        rule = self.rule(scope.get("path", "")) if scope["type"] == "http" else None
        if rule is None:
            return await self.app(scope, receive, send)

        async def send_with_rule(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"cache-control"]
                message = {**message, "headers": [*headers, (b"cache-control", rule.encode())]}
            await send(message)

        await self.app(scope, receive, send_with_rule)


class CanonicalHost:
    """Quem chega pelo Cloudflare vai sempre para https e para o domínio sem "www." (a sessão fica num endereço
    só). O acesso direto, pela rede de casa, não muda."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        visitor, host = headers.get("cf-visitor", ""), headers.get("host", "")
        insecure = '"http"' in visitor.replace(" ", "")
        if visitor and (insecure or host.startswith("www.")):
            query = scope.get("query_string", b"").decode("latin-1")
            target = f"https://{host.removeprefix('www.')}{scope.get('path', '/')}{'?' + query if query else ''}"
            return await RedirectResponse(target, 308)(scope, receive, send)
        await self.app(scope, receive, send)


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
    yt = YouTubeLogin()
    jobs = PostJobs(settings)
    task_list: dict[tuple[str, int], Task] = {}  # (preços | redes, usuário): o Resumo de cada um mostra quando rodam

    def task(name: str, user_id: int) -> Task:
        if (name, user_id) not in task_list:
            task_list[(name, user_id)] = Task(settings, {"prices": "precos", "social": "redes"}[name], user_id)
        return task_list[(name, user_id)]

    edit_lock = threading.Lock()  # edições do scan.json não podem se intercalar
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    ollama = {"checked": 0.0, "available": False}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        auth.adopt(con(), settings)  # o que não tem dono (de antes das contas) fica com o administrador
        for part in (settings.runs_dir / "_incoming").glob("*.part"):  # envios de antes de reiniciar: não continuam
            part.unlink(missing_ok=True)
        pipeline.recover_interrupted(settings)
        social.interrupted(con())
        job_log.interrupted(con())
        runner.start()
        stop = threading.Event()
        threading.Thread(target=agenda, args=(stop,), name="cardline-agenda", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="cardline", lifespan=lifespan)
    app.add_middleware(AuthGate, settings=settings)
    app.add_middleware(CacheRules)  # por fora: vale também para as recusas do porteiro
    app.add_middleware(CanonicalHost)

    def con():
        return db.connect(settings.db_path)

    # --- quem está usando: o porteiro (AuthGate) diz quem é e de quem são os dados mostrados ---

    def viewer() -> auth.Viewer:
        v = auth.CURRENT.get()
        if v is None:
            raise HTTPException(401, "Entre com a sua conta do Google.")
        return v

    def me() -> int:
        """Quem usa a página: é em nome dele que tudo é criado e alterado."""
        return viewer().user.id

    def owner() -> int:
        """De quem são os dados mostrados: os dele, ou os de quem compartilhou (só leitura)."""
        return viewer().owner

    def acct(user_id: int | None = None) -> Settings:
        """As configurações com as credenciais (YouTube, Instagram, logos) de um usuário (por padrão, de quem usa)."""
        return dataclasses.replace(settings, account=me() if user_id is None else user_id)

    def run_row(run_id: int, write: bool = False):
        """A pipeline, se ela é do usuário (ou, só para ver, de quem compartilhou com ele)."""
        run = con().execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if run is None or run["user_id"] != (me() if write else owner()):
            raise HTTPException(404, "Pipeline não encontrada.")
        return run

    def require_admin() -> None:
        if not auth.is_admin(con(), settings, viewer().user):
            raise HTTPException(403, "Só o administrador mexe no catálogo de sets.")

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
        for r in c.execute("SELECT id, dir FROM runs WHERE user_id = ? ORDER BY id", (owner(),)):  # só as do mesmo dono
            if ids := distinct_cards(r):
                groups.setdefault(ids, []).append(r["id"])
        return {rid: [o for o in g if o != rid] for g in groups.values() if len(g) > 1 for rid in g}

    def run_json(c, run, detail: bool = False, dups: dict[int, list[int]] | None = None) -> dict:
        folder = settings.root / run["dir"]
        url = f"/runs/{folder.name}"
        scan = load_scan(folder) if (folder / "scan.json").exists() else None
        cards = scan["cards"] if scan else []
        boosters = scan.get("packs", []) if scan else []  # registro de lacrados: os boosters da pilha
        repeats = repeated(cards)
        removed = scan.get("removed", []) if scan and run["kind"] != "lacrados" else []  # lacrados: boosters removidos
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
            "steps": [{"name": n, "label": pipeline.label(run["kind"], n), **{k: steps.get(n, {}).get(k) for k in
                       ("status", "message", "started_at", "finished_at")}} for n in pipeline.steps_for(run["kind"])],
        }
        if run["kind"] == "lacrados":
            market = {r["set_code"]: r["usd"] for r in c.execute(
                "SELECT set_code, usd FROM sealed WHERE run_id = ? AND product_id IS NOT NULL", (run["id"],))}
            now_of = lambda p: p["manual_usd"] if p.get("manual_usd") is not None else market.get(p["set"]) or p.get("price_usd")  # noqa: E731
            out.update(packs=len(boosters), thumbs=[f"{url}/{p['crop']}" for p in boosters if p.get("crop")][:24],
                       value_open=sum(p.get("price_usd") or 0 for p in boosters) if any("price_usd" in p for p in boosters) else None,
                       value_now=sum(now_of(p) or 0 for p in boosters) if boosters else None)
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
            if run["kind"] == "lacrados":
                pack_json = lambda p: {"uid": p["uid"], "set": p["set"], "set_name": p.get("set_name"), "t": p["t"],  # noqa: E731
                                       "price_open": p.get("price_usd"), "price_now": now_of(p), "value_usd": p.get("manual_usd"),
                                       "crop": f"{url}/{p['crop']}" if p.get("crop") else None, "inliers": p.get("inliers"),
                                       "manual": p.get("manual", False)}
                out["pack_items"] = [{"n": n, **pack_json(p)} for n, p in enumerate(boosters, 1)]
                out["removed"] = [pack_json(p) for p in (scan or {}).get("removed", [])]
            script = narration.load_script(folder) or {}
            out["narration"] = {"enabled": bool(json.loads(run["options"]).get("narration")),
                                "lines": script.get("lines", []), "source": script.get("source"),
                                "writer": script.get("writer"), "voz": script.get("voz") or settings.narration_voice}
            if scan and run["kind"] in pipeline.VIDEO_KINDS:
                names = {r["code"]: r["name"] for r in c.execute("SELECT code, name FROM sets")}
                out["post_suggestion"] = (social.sealed_suggestion(scan, names, settings.youtube_tags) if run["kind"] == "lacrados"
                                          else social.suggestion(scan, names, settings.youtube_tags))
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
            "youtube": youtube.status(acct()), "instagram": instagram.status(acct()),
            "logo": {"default": logo.default(acct()), "corner": settings.logo_corner},
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
        rows = c.execute("SELECT * FROM collection WHERE user_id = ? ORDER BY added_at, pack, slot", (owner(),)).fetchall()
        card_rows = {r["id"]: r for r in c.execute(
            "SELECT * FROM cards WHERE id IN (SELECT card_id FROM collection WHERE user_id = ?)", (owner(),))}
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
        return [run_json(c, r, dups=dups) for r in c.execute(
            "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC, id DESC", (owner(),))]

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: int):
        return run_json(con(), run_row(run_id), detail=True)

    uploads: dict[str, dict] = {}  # vídeos chegando em partes: id → dono, arquivo, bytes recebidos, sha1

    def my_upload(upload_id: str) -> dict:
        up = uploads.get(upload_id)
        if up is None or up["user"] != me():
            raise HTTPException(404, "Esse envio não existe mais (o servidor reiniciou?). Envie o vídeo de novo.")
        return up

    @app.post("/api/uploads", status_code=201)
    def upload_start(body: UploadBody):
        """Começa o envio de um vídeo em partes (PUT /api/uploads/{id}?offset=…); a pipeline nasce com
        `upload={id}`. Pelo túnel do Cloudflare, cada requisição pode ter no máximo 100 MB."""
        if body.size <= 0:
            raise HTTPException(400, "O vídeo está vazio.")
        now = time.time()
        for key in [k for k, v in uploads.items() if now - v["touched"] > 6 * 3600]:  # abandonados
            uploads.pop(key)["path"].unlink(missing_ok=True)
        incoming = settings.runs_dir / "_incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        upload_id = uuid.uuid4().hex
        path = incoming / f"{upload_id}.part"
        path.touch()
        uploads[upload_id] = {"user": me(), "path": path, "name": Path(body.filename).name, "size": 0,
                              "total": body.size, "sha1": hashlib.sha1(), "touched": now}
        return {"id": upload_id, "chunk": UPLOAD_CHUNK}

    @app.put("/api/uploads/{upload_id}")
    async def upload_part(upload_id: str, offset: int, request: Request):
        """Uma parte, no ponto em que o envio está. Uma parte repetida (a resposta se perdeu) ou fora de ordem
        recebe 409 com o tamanho que já chegou, e o envio continua dali."""
        up = my_upload(upload_id)
        if offset != up["size"]:
            raise HTTPException(409, {"message": "Parte fora de ordem.", "size": up["size"]})
        data = await request.body()
        if offset != up["size"]:  # outra parte entrou enquanto esta chegava
            raise HTTPException(409, {"message": "Parte fora de ordem.", "size": up["size"]})
        if not data or len(data) > UPLOAD_CHUNK or up["size"] + len(data) > up["total"]:
            raise HTTPException(400, "Parte vazia ou maior que o esperado.")
        with open(up["path"], "ab") as f:
            f.write(data)
        up["sha1"].update(data)
        up["size"] += len(data)
        up["touched"] = time.time()
        return {"size": up["size"]}

    @app.delete("/api/uploads/{upload_id}")
    def upload_cancel(upload_id: str):
        my_upload(upload_id)
        uploads.pop(upload_id)["path"].unlink(missing_ok=True)
        return {"ok": True}

    @app.post("/api/runs", status_code=201)
    async def upload(
        request: Request, filename: str, paid: float | None = None, paid_currency: str = "BRL",
        set_hint: str | None = None, overlay: bool = True, verify: bool | None = None, currency: str | None = None,
        kind: str = "abertura", narration: bool = False, logo_name: str | None = Query(None, alias="logo"),
        sealed_id: int | None = None, sealed_qty: int = 1, upload_id: str | None = Query(None, alias="upload"),
    ):
        if kind not in pipeline.KINDS:
            raise HTTPException(400, "Tipo de pipeline deve ser abertura ou cadastro.")
        if logo_name and not logo.path(acct(), logo_name):
            raise HTTPException(400, "Logo não encontrado; envie a imagem de novo.")
        if sealed_id and sealed.owner(con(), sealed_id) != me():
            raise HTTPException(404, "Esse booster não está nos seus lacrados.")
        if paid_currency.upper() not in CURRENCIES or (currency and currency.upper() not in CURRENCIES):
            raise HTTPException(400, "Moeda deve ser USD ou BRL.")
        if upload_id:  # o vídeo já chegou em partes (/api/uploads)
            up = my_upload(upload_id)
            if up["size"] != up["total"]:
                raise HTTPException(400, "O envio do vídeo ainda não terminou.")
            uploads.pop(upload_id)
            tmp, sha1 = up["path"], up["sha1"]
        else:  # o vídeo é o corpo desta requisição
            incoming = settings.runs_dir / "_incoming"
            incoming.mkdir(parents=True, exist_ok=True)
            tmp, sha1 = incoming / f"{uuid.uuid4().hex}.part", hashlib.sha1()
        try:
            if not upload_id:
                size = 0
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
                    currency=currency, move=True, kind=kind, narration=narration, logo=logo_name or None,
                    sealed=(sealed_id, max(1, sealed_qty)) if sealed_id else None, user_id=me(),
                )
            except pipeline.DuplicateVideo as e:
                raise HTTPException(409, {"message": str(e), "run_id": e.run_id}) from e
            except LookupError as e:  # o booster escolhido não está mais nos lacrados
                raise HTTPException(409, str(e)) from e
        finally:
            tmp.unlink(missing_ok=True)
        runner.wake()
        return {"id": run_id}

    @app.post("/api/logos")
    async def upload_logo(request: Request):
        """Logo para o vídeo de uma pipeline (PNG com transparência fica melhor); devolve o nome para a criação."""
        data = await request.body()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(413, "Imagem grande demais (máximo 8 MB).")
        try:
            name = logo.save(acct(), data)  # na pasta de logos de quem enviou
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"logo": name, "url": f"/logos/{name}"}

    @app.post("/api/runs/{run_id}/rerun")
    def rerun(run_id: int, body: RerunBody):
        run_row(run_id, write=True)
        try:
            pipeline.enqueue(settings, run_id, body.from_step)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except (RuntimeError, ValueError) as e:
            raise HTTPException(409, str(e)) from e
        runner.wake()
        return {"ok": True}

    def editable_run(run_id: int):
        run = run_row(run_id, write=True)
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

    def set_name(code: str) -> str:
        row = con().execute("SELECT name FROM sets WHERE code = ?", (code,)).fetchone()
        if row is None:
            raise HTTPException(400, "Set desconhecido.")
        return row["name"]

    def pack_run(run_id: int) -> Path:
        folder = editable_run(run_id)
        if con().execute("SELECT kind FROM runs WHERE id = ?", (run_id,)).fetchone()["kind"] != "lacrados":
            raise HTTPException(400, "Só o registro de lacrados tem boosters para editar.")
        return folder

    @app.delete("/api/runs/{run_id}/packs/{uid}")
    def delete_pack(run_id: int, uid: str):
        """Tira um booster identificado errado; vale para os lacrados e o vídeo ao reprocessar."""
        with edit_lock:
            try:
                packs_mod.remove_pack(pack_run(run_id), uid)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            pipeline.mark_stale(settings, run_id, "prices")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/packs/{uid}/restore")
    def restore_pack(run_id: int, uid: str):
        with edit_lock:
            try:
                packs_mod.restore_pack(pack_run(run_id), uid)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            pipeline.mark_stale(settings, run_id, "prices")
        return {"ok": True}

    @app.patch("/api/runs/{run_id}/packs/{uid}")
    def edit_pack(run_id: int, uid: str, body: PackBody):
        """Corrige um booster: o set e/ou o valor (fixo para ele; null volta ao preço de mercado). Vale ao reprocessar."""
        if body.value is not None and (body.value < 0 or body.currency.upper() not in CURRENCIES):
            raise HTTPException(400, "Valor (não negativo) em USD ou BRL.")
        value = {"value_usd": to_usd(settings, body.value, body.currency) if body.value is not None else None} \
            if "value" in body.model_fields_set else {}
        with edit_lock:
            try:
                changed = packs_mod.edit_pack(pack_run(run_id), uid, body.set, set_name(body.set) if body.set else None, **value)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            if changed:
                pipeline.mark_stale(settings, run_id, "prices")
        return {"ok": True, "changed": changed}

    @app.post("/api/runs/{run_id}/packs")
    def add_pack(run_id: int, body: PackBody):
        """Inclui um booster que a identificação não pegou, no instante t do vídeo."""
        if body.t is None or body.t < 0 or not body.set:
            raise HTTPException(400, "Informe o set e o instante do vídeo em que o booster aparece.")
        with edit_lock:
            uid = packs_mod.add_pack(pack_run(run_id), body.set, set_name(body.set), body.t)
            pipeline.mark_stale(settings, run_id, "prices")
        return {"ok": True, "uid": uid}

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
        run_row(run_id, write=True)
        sent = body.model_fields_set
        try:
            if "paid" in sent:
                pipeline.update_paid(settings, run_id, body.paid, body.paid_currency or "BRL")
            if "currency" in sent and body.currency:
                pipeline.update_overlay_currency(settings, run_id, body.currency.upper())
            if "narration" in sent and body.narration is not None:
                pipeline.update_narration(settings, run_id, body.narration)
            if "logo" in sent:
                if body.logo and not logo.path(acct(), body.logo):
                    raise HTTPException(400, "Logo não encontrado; envie a imagem de novo.")
                pipeline.update_logo(settings, run_id, body.logo or None)
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
        login = yt.of(me())
        return {**youtube.status(acct()),
                "login": {k: login[k] for k in ("user_code", "verification_url", "expires_at", "status", "error")}
                if login else None}

    @app.post("/api/youtube/client")
    def youtube_client(body: ClientBody):
        """O cliente OAuth é do app (serve para entrar e para o YouTube de todos): só o administrador troca."""
        require_admin()
        try:
            youtube.save_client(settings, body.client_id, body.client_secret)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return youtube.status(acct())

    @app.post("/api/youtube/connect")
    def youtube_connect():
        """Pede um código ao Google; a página mostra o código e espera o usuário autorizar."""
        state = net_call(yt.start, acct())
        return {k: state[k] for k in ("user_code", "verification_url", "expires_at", "status")}

    @app.post("/api/youtube/disconnect")
    def youtube_disconnect():
        yt.forget(me())
        youtube.disconnect(acct())
        return youtube.status(acct())

    @app.get("/api/youtube/recent")
    def youtube_recent():
        """Os últimos vídeos do canal, para vincular o que foi postado pelo app."""
        return net_call(youtube.recent_uploads, acct())

    @app.get("/api/instagram")
    def instagram_status():
        return instagram.status(acct())

    @app.post("/api/instagram/token")
    def instagram_token(body: TokenBody):
        """Token gerado no painel da Meta (conta profissional testadora do app)."""
        try:
            return net_call(instagram.save_token, acct(), body.token)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/instagram/disconnect")
    def instagram_disconnect():
        instagram.disconnect(acct())
        return instagram.status(acct())

    def refresh_numbers(c, user_id: int, only_run: int | None = None, progress=lambda fraction, message=None: None) -> int:
        """Lê os números dos posts do usuário pela API da rede (o YouTube e o Instagram que ele conectou); devolve
        quantos leu. Recebe o usuário porque também roda em segundo plano (depois de postar, na agenda)."""
        n, us = 0, acct(user_id)
        if youtube.status(us)["connected"]:
            progress(0.1, "Lendo o YouTube")
            posts = [(r, v) for r, v in social.linked(c, "youtube", user_id) if only_run in (None, r)]
            if posts:
                found = net_call(youtube.stats, us, [v for _, v in posts])
                for run_id, vid in posts:
                    if info := found.get(vid):
                        social.save_stats(c, run_id, "youtube", info)
                        social.update_post(c, run_id, "youtube", title=info["title"], privacy=info["privacy"],
                                           published_at=info["published_at"], scheduled_at=info["scheduled_at"])
                        n += 1
        if instagram.status(us)["connected"]:
            posts = [(r, m) for r, m in social.linked(c, "instagram", user_id) if only_run in (None, r)]
            if posts:
                progress(0.5, "Lendo o Instagram")
                try:
                    instagram.refresh(us)  # renova o token de 60 dias com folga
                except (instagram.InstagramError, OSError):
                    pass
                found = net_call(instagram.stats, us, [m for _, m in posts])
                for run_id, media in posts:
                    if info := found.get(media):
                        social.save_stats(c, run_id, "instagram", info)
                        social.update_post(c, run_id, "instagram", published_at=info["published_at"])
                        n += 1
        return n

    def last_reads(c, user_id: int, only_run: int | None = None) -> list[str | None]:
        """A última leitura pela API de cada post do usuário que dá para ler (rede conectada e ID na rede);
        None = nunca lido."""
        us = acct(user_id)
        nets = [n for n, mod in (("youtube", youtube), ("instagram", instagram)) if mod.status(us)["connected"]]
        if not nets:
            return []
        return [r[0] for r in c.execute(
            "SELECT (SELECT MAX(fetched_at) FROM post_stats s WHERE s.run_id = p.run_id AND s.network = p.network"
            " AND s.manual = 0) FROM posts p JOIN runs r ON r.id = p.run_id WHERE p.post_id IS NOT NULL"
            f" AND p.network IN ({','.join('?' * len(nets))}) AND r.user_id = ? AND (? IS NULL OR p.run_id = ?)",
            (*nets, user_id, only_run, only_run))]

    @app.post("/api/social/stats")
    def social_stats(max_age: float = 0, run: int | None = None):
        """Atualiza os números dos posts pela API. Com `max_age`, só se algum post estiver com a leitura mais velha
        que isso (ou sem leitura): ler um post não deixa os outros parecendo atualizados."""
        c = con()
        if run is not None:
            run_row(run, write=True)
        reads = last_reads(c, me(), run)
        if max_age and reads and all(r and time.time() - datetime.fromisoformat(r).timestamp() < max_age for r in reads):
            return {"updated": 0, "fresh": True}
        return {"updated": refresh_numbers(c, me(), run)}

    def opening(run_id: int):
        run = run_row(run_id, write=True)
        if run["kind"] not in pipeline.VIDEO_KINDS:
            raise HTTPException(400, "Só aberturas de booster e registros de lacrados têm vídeo para postar.")
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
        run_id, us = run["id"], acct(run["user_id"])  # a conta do Instagram do dono da pipeline
        video, variant = video_of(run, variant)
        rel = f"{video.parent.name}/{video.name}"  # o Instagram baixa sem sessão: o link vai assinado
        video_url = f"{base}/runs/{rel}?v={int(video.stat().st_mtime)}&k={auth.sign(settings, rel)}"
        caption = f"{caption}\n\n{social.HASHTAGS['instagram']}".strip()

        def work(progress):
            c = db.connect(settings.db_path)
            try:
                media = instagram.publish_reel(us, video_url, caption, progress)
            except Exception as e:
                if scheduled:
                    social.set_schedule(c, run_id, "instagram", "failed", str(e))
                raise
            social.save_post(c, run_id, "instagram", media["id"], media.get("permalink") or "https://www.instagram.com/",
                             via="api", variant=variant, title=(media.get("caption") or "")[:120] or None,
                             privacy="public", published_at=media.get("timestamp"))
            social.unschedule(c, run_id, "instagram")
            try:
                refresh_numbers(c, run["user_id"], run_id)
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
            us = acct()
            if not youtube.status(us)["connected"]:
                raise HTTPException(409, "Conecte o canal do YouTube.")
            caption = f"{body.caption}\n\n{social.HASHTAGS['youtube']}".strip()
            tags = body.tags
            if not tags:
                folder = settings.root / run["dir"]
                scan = load_scan(folder) if (folder / "scan.json").exists() else {"cards": []}
                names = {r["code"]: r["name"] for r in c.execute("SELECT code, name FROM sets")}
                tags = social.suggestion(scan, names, settings.youtube_tags)["tags"]

            def work(progress):
                created = youtube.upload(us, video, body.title, caption, tags, body.privacy,
                                         progress=lambda f: progress(f, "Enviando o vídeo"), publish_at=when)
                c = db.connect(settings.db_path)
                status = created.get("status", {})
                social.save_post(c, run_id, "youtube", created["id"], youtube.url(created["id"]), via="api", variant=variant,
                                 title=created.get("snippet", {}).get("title"), privacy=status.get("privacyStatus"),
                                 published_at=created.get("snippet", {}).get("publishedAt"),
                                 scheduled_at=status.get("publishAt"))
                try:  # a visibilidade de verdade (projeto sem auditoria: travado como privado)
                    refresh_numbers(c, run["user_id"], run_id)
                except HTTPException:
                    pass

            try:
                jobs.start(run_id, network, work)
            except RuntimeError as e:
                raise HTTPException(409, str(e)) from e
            return {"ok": True}
        if not instagram.status(acct())["connected"]:
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
            run = c.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()  # a agenda roda sem usuário logado
            base = public_base(settings, port=req.get("port"), fallback=req.get("base"))
            problem = ("O Instagram está desconectado: conecte de novo para publicar."
                       if not instagram.status(acct(run["user_id"]))["connected"] else
                       None if base else "Sem endereço público: o túnel (ngrok) está fechado.")
            if problem:
                social.set_schedule(c, run_id, network, "waiting", problem)
                continue
            social.set_schedule(c, run_id, network, "sending")
            try:
                start_instagram(run, req["variant"], req["caption"], base, scheduled=True)
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
        run_row(run_id, write=True)
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
        c, us = con(), acct()
        if network == "youtube" and youtube.status(us)["connected"]:
            info = net_call(youtube.stats, us, [post_id]).get(post_id)
            if info is None:
                raise HTTPException(404, "O YouTube não encontrou esse vídeo (ou ele é privado de outra conta).")
            social.save_post(c, run_id, network, post_id, url, via="link", title=info["title"], privacy=info["privacy"],
                             published_at=info["published_at"], scheduled_at=info["scheduled_at"])
            social.save_stats(c, run_id, network, info)
        elif network == "instagram" and instagram.status(us)["connected"]:
            media = net_call(instagram.find_media, us, post_id)  # o link traz o código; a API usa o ID da mídia
            social.save_post(c, run_id, network, media["id"] if media else None, url, via="link",
                             title=((media or {}).get("caption") or "")[:120] or None,
                             published_at=(media or {}).get("timestamp"))
            if media:
                refresh_numbers(c, me(), run_id)
        else:
            social.save_post(c, run_id, network, None if network == "instagram" else post_id, url, via="link")
        if (social.scheduled(c, run_id).get(network) or {}).get("status") != "sending":
            social.unschedule(c, run_id, network)  # já postou: a publicação programada não acontece mais
        return {"ok": True, "network": network}

    @app.patch("/api/runs/{run_id}/posts/{network}")
    def post_numbers(run_id: int, network: str, body: StatsBody):
        """Números informados à mão (o TikTok não dá os números sem a aprovação do app)."""
        run_row(run_id, write=True)
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
        run_row(run_id, write=True)
        c = con()
        with c:
            c.execute("DELETE FROM posts WHERE run_id = ? AND network = ?", (run_id, network))
        jobs.jobs.pop((run_id, network), None)
        return {"ok": True}

    def collection_sets(user_id: int) -> list[str]:
        return [r[0] for r in con().execute(
            "SELECT DISTINCT cards.set_code FROM collection JOIN cards ON cards.id = collection.card_id"
            " WHERE collection.user_id = ? ORDER BY 1", (user_id,))]

    @app.post("/api/prices/refresh")
    def refresh_current_prices():
        """Busca os preços de hoje dos sets da coleção; o preço na abertura de cada carta não muda."""
        try:
            return refresh_prices(settings, collection_sets(me()))
        except OSError as e:
            raise HTTPException(502, f"Não consegui buscar os preços no Lorcast ({e}).") from e

    def sealed_json(r: dict) -> dict:
        return {**r, "value_usd": r["usd"] * r["qty"] if r["usd"] is not None else None,
                "paid_total_usd": r["paid_usd"] * r["qty"] if r["paid_usd"] is not None else None}

    @app.get("/api/sealed")
    def sealed_list():
        """Os lacrados da coleção, com o valor de hoje (preço de mercado × quantidade) e o pago."""
        out = [sealed_json(r) for r in sealed.items(con(), owner())]
        paid = [x for x in out if x["paid_total_usd"] is not None]
        return {"items": out, "qty": sum(x["qty"] for x in out),
                "value_usd": sum(x["value_usd"] or 0 for x in out),
                "paid_usd": sum(x["paid_total_usd"] for x in paid) if paid else None,
                "value_of_paid_usd": sum(x["value_usd"] or 0 for x in paid) if paid else None}

    @app.get("/api/sealed/boosters")
    def sealed_boosters():
        """Os boosters fechados da coleção, para escolher numa abertura."""
        return [sealed_json(r) for r in sealed.boosters(con(), me())]

    @app.get("/api/sealed/products")
    def sealed_products(set_code: str = Query(..., alias="set")):
        """Os lacrados do set à venda no TCGplayer, com o preço de hoje."""
        try:
            return sealed.products(settings, con(), set_code)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except (OSError, ValueError, KeyError) as e:
            raise HTTPException(502, f"Não consegui buscar os produtos no TCGplayer ({e}).") from e

    @app.post("/api/sealed", status_code=201)
    def sealed_add(body: SealedBody):
        if body.qty < 1 or (body.paid is not None and body.paid < 0):
            raise HTTPException(400, "Quantidade (1 ou mais) e valor pago (não negativo) inválidos.")
        if body.paid_currency.upper() not in CURRENCIES:
            raise HTTPException(400, "Moeda deve ser USD ou BRL.")
        try:
            return {"id": sealed.add(settings, con(), body.set_code, body.product_id, body.qty, body.paid, body.paid_currency,
                                     me())}
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except (OSError, ValueError, KeyError) as e:
            raise HTTPException(502, f"Não consegui buscar o produto no TCGplayer ({e}).") from e

    @app.patch("/api/sealed/{item_id}")
    def sealed_edit(item_id: int, body: SealedPatch):
        if (body.qty is not None and body.qty < 1) or (body.paid is not None and body.paid < 0):
            raise HTTPException(400, "Quantidade (1 ou mais) e valor pago (não negativo) inválidos.")
        if body.paid_currency and body.paid_currency.upper() not in CURRENCIES:
            raise HTTPException(400, "Moeda deve ser USD ou BRL.")
        if sealed.owner(con(), item_id) != me():
            raise HTTPException(404, "Lacrado não encontrado.")
        try:
            sealed.update(settings, con(), item_id, body.qty, body.paid, body.paid_currency, "paid" in body.model_fields_set)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    @app.delete("/api/sealed/{item_id}")
    def sealed_remove(item_id: int):
        if sealed.owner(con(), item_id) != me():
            raise HTTPException(404, "Lacrado não encontrado.")
        try:
            sealed.remove(con(), item_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        return {"ok": True}

    @app.get("/api/history")
    def history():
        """As séries por dia do Resumo: o valor da coleção e as visualizações nas redes (por dia de leitura)."""
        c = con()
        return {"value": [dict(r) for r in c.execute("SELECT day, cards_usd, sealed_usd, cards FROM user_values"
                                                     " WHERE user_id = ? ORDER BY day", (owner(),))],
                "views": social.views_by_day(c, owner())}

    def live_job(j: dict) -> dict:
        """A linha da atualização com o andamento de agora, se ela ainda estiver rodando."""
        if j["status"] != "running":
            return j
        for t in task_list.values():
            if t.state.get("job") == j["id"] and t.state["running"]:
                return {**j, "progress": t.state["progress"], "message": t.state["message"]}
        if j["kind"] == "sync" and sync_job.job == j["id"]:
            st = sync_job.status()
            last = [line for line in st["log"].splitlines() if line.strip()][-1:] or ["Começando"]
            return {**j, "message": last[0].strip(), "log": st["log"]}
        return j

    @app.get("/api/jobs")
    def jobs_list():
        """As atualizações disparadas na página (preços, números das redes, sincronização), da mais nova."""
        return [live_job(j) for j in job_log.recent(con(), owner())]

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: int):
        j = job_log.get(con(), job_id, owner())
        if j is None:
            raise HTTPException(404, "Atualização não encontrada.")
        return live_job(j)

    @app.get("/api/tasks")
    def task_status():
        """As tarefas de fundo do Resumo: preços e números das redes."""
        return {name: dict(task(name, me()).state) for name in ("prices", "social")}

    @app.post("/api/tasks/prices")
    def start_prices():
        """Atualiza os preços de hoje em segundo plano (uma consulta ao Lorcast por set da coleção)."""
        uid = me()  # a tarefa roda numa thread, sem o usuário da requisição

        def work(progress):
            c = db.connect(settings.db_path)
            before = db.collection_value(c, uid)
            names = {r["code"]: r["name"] for r in c.execute("SELECT code, name FROM sets")}
            try:
                done = refresh_prices(settings, collection_sets(uid), lambda f, m=None: progress(0.8 * f, m))["sets"]
            except OSError as e:
                raise RuntimeError(f"Não consegui buscar os preços no Lorcast ({e}).") from e
            note, n = "", 0
            try:  # os lacrados: preço do TCGplayer via tcgcsv
                n = sealed.refresh_prices(settings, c, lambda f, m=None: progress(0.8 + 0.2 * f, m), uid)
            except (OSError, ValueError, KeyError, LookupError) as e:
                note = f"; os dos lacrados não vieram ({e})"
            after = db.collection_value(c, uid)
            result = {"sets": [names.get(x, x) for x in done], "sealed": n, "cards_before": before[0],
                      "cards_after": after[0], "sealed_before": before[1], "sealed_after": after[1]}
            return (f"Preços de hoje atualizados ({len(done)} {'set' if len(done) == 1 else 'sets'}"
                    + (f" e {n} {'lacrado' if n == 1 else 'lacrados'}" if n else "") + f"){note}.", result)

        try:
            return task("prices", uid).start(work)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/api/tasks/social")
    def start_social(max_age: float = 0):
        """Lê os números de todos os vídeos vinculados em segundo plano. Com `max_age`, só se algum vídeo estiver
        com a leitura mais velha que isso (ou sem leitura)."""
        uid = me()
        reads = last_reads(con(), uid)
        if not reads:
            return {**task("social", uid).state, "skipped": True}
        if max_age and all(r and time.time() - datetime.fromisoformat(r).timestamp() < max_age for r in reads):
            return {**task("social", uid).state, "fresh": True}

        def work(progress):
            c = con()
            before = social.total_views(c, uid)
            n = refresh_numbers(c, uid, None, progress)
            return (f"{n} {'vídeo atualizado' if n == 1 else 'vídeos atualizados'}.",
                    {"videos": n, "views_before": before, "views_after": social.total_views(c, uid)})

        try:  # a leitura automática (ao abrir o Resumo) não vira linha na aba Pipelines; a do botão vira
            return task("social", uid).start(work, record=not max_age)
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e

    @app.delete("/api/runs/{run_id}")
    def delete(run_id: int):
        run_row(run_id, write=True)
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
            " FROM collection JOIN cards ON cards.id = collection.card_id WHERE collection.user_id = ?"
            " GROUP BY cards.set_code", (owner(),))}
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
        require_admin()  # o catálogo (sets, cartas, preços) é de todos
        try:
            sync_job.start(me())
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return sync_job.status()

    @app.post("/api/sets/{code}/icon")
    async def set_icon(code: str, request: Request):
        require_admin()
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
        require_admin()
        try:
            reset_icon(settings, code)
        except (OSError, ValueError, KeyError) as e:
            raise HTTPException(502, f"Ícone removido, mas não consegui buscar o automático ({e}).") from e
        return {"ok": True}

    # --- contas: entrar com o Google, sair, compartilhar -----------------------------------------

    logins: dict[str, dict] = {}  # entradas esperando o código ser digitado em google.com/device
    web_logins: dict[str, dict] = {}  # entradas pelo botão, esperando a volta do Google (state → verificador PKCE)
    STATE_COOKIE = "cardline_entrada"

    def user_json(u: auth.User) -> dict:
        return {"id": u.id, "email": u.email, "name": u.name, "picture": u.picture}

    def https(request: Request) -> bool:
        return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"

    def web_ready(request: Request) -> bool:
        """O botão comum do Google: precisa do cliente web e da página aberta no endereço público, por https (o
        Google só volta para endereços cadastrados, e o IP da rede de casa não pode ser um deles)."""
        host = (request.headers.get("host") or "").split(":")[0]
        public = urllib.parse.urlparse(settings.public_url).hostname if settings.public_url else None
        return auth.web_client(settings) is not None and host == public and https(request)

    def welcome(info: dict) -> tuple[auth.User, str]:
        """Confere se a conta pode entrar e abre a sessão: o usuário e o token do cookie."""
        c = con()
        if not auth.allowed(c, settings, info["email"]):
            raise PermissionError(f"A conta {info['email']} não tem acesso a este cardline. Peça para quem cuida dele "
                                  "compartilhar a coleção com você.")
        user = auth.upsert_user(c, info["email"], info["name"], info["picture"])
        auth.adopt(c, settings)  # sem administrador definido, o primeiro a entrar fica com os dados de antes
        return user, auth.new_session(c, user.id)

    def with_session(resp, token: str, request: Request):
        resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, path="/", httponly=True,
                        samesite="lax", secure=https(request))
        return resp

    @app.get("/api/auth/me")
    def auth_me(request: Request):
        """Quem está logado (ou ninguém: a página mostra a entrada), com quem ele compartilhou e quem compartilhou
        com ele."""
        c = con()
        user = auth.request_user(c, settings, session_cookie(request.headers.get("cookie")))
        if user is None:
            public = settings.public_url if settings.public_url and auth.web_client(settings) else None
            return {"user": None, "google": youtube.client(settings) is not None, "web": web_ready(request),
                    **({"public": public} if public and not web_ready(request) else {})}
        return {"user": user_json(user), "admin": auth.is_admin(c, settings, user), "my_shares": auth.shares(c, user.id),
                "shared_with_me": [user_json(u) for u in auth.shared_with(c, user)]}

    @app.post("/api/auth/start")
    def auth_start():
        """Pede o código ao Google: a página mostra o código para digitar em google.com/device."""
        try:
            info = auth.start_login(settings)
        except LookupError as e:
            raise HTTPException(409, str(e)) from e
        except (RuntimeError, OSError) as e:
            raise HTTPException(502, str(e)) from e
        now = time.time()
        for key in [k for k, v in logins.items() if v["expires_at"] < now]:
            logins.pop(key)
        if len(logins) >= 50:  # a entrada é aberta: um teto para ninguém encher a memória (e o Google) de pedidos
            raise HTTPException(429, "Muitas entradas esperando o código. Tente de novo em alguns minutos.")
        login_id = uuid.uuid4().hex
        logins[login_id] = {"device_code": info["device_code"], "interval": float(info.get("interval", 5)), "next": 0.0,
                            "expires_at": now + float(info.get("expires_in", 1800))}
        return {"id": login_id, "user_code": info["user_code"],
                "verification_url": info.get("verification_url") or info.get("verification_uri"),
                "expires_at": logins[login_id]["expires_at"], "interval": logins[login_id]["interval"]}

    @app.post("/api/auth/poll")
    def auth_poll(body: LoginBody, request: Request):
        """A página pergunta se o código já foi digitado; quando foi, abre a sessão (cookie de 30 dias)."""
        pending = logins.get(body.id)
        if pending is None or pending["expires_at"] < time.time():
            logins.pop(body.id, None)
            raise HTTPException(410, "O código expirou. Peça outro.")
        if time.time() < pending["next"]:  # o Google pede um intervalo entre as consultas
            return {"status": "pending"}
        pending["next"] = time.time() + pending["interval"]
        try:
            info = auth.poll_login(settings, pending["device_code"])
        except PermissionError as e:
            logins.pop(body.id, None)
            raise HTTPException(403, str(e)) from e
        except TimeoutError as e:
            logins.pop(body.id, None)
            raise HTTPException(410, str(e)) from e
        except (RuntimeError, OSError) as e:
            raise HTTPException(502, str(e)) from e
        if info is None:
            return {"status": "pending"}
        logins.pop(body.id, None)
        try:
            user, token = welcome(info)
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e
        return with_session(JSONResponse({"status": "done", "user": user_json(user)}), token, request)

    @app.get("/api/auth/google")
    def auth_google(request: Request):
        """O botão "Fazer login com o Google": vai para a escolha da conta no Google, que volta para o callback."""
        if not web_ready(request):
            raise HTTPException(404, "O botão do Google só funciona no endereço público (https); aqui, entre pelo código.")
        now = time.time()
        for key in [k for k, v in web_logins.items() if v["at"] < now - 600]:
            web_logins.pop(key)
        if len(web_logins) >= 200:
            raise HTTPException(429, "Muitas entradas em andamento. Tente de novo em alguns minutos.")
        state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        web_logins[state] = {"verifier": verifier, "at": now}
        resp = RedirectResponse(auth.web_login_url(settings, state, verifier), 303)
        # o mesmo navegador que saiu tem de voltar: o state também fica num cookie (contra entrada forjada)
        resp.set_cookie(STATE_COOKIE, state, max_age=600, path="/api/auth/google", httponly=True, samesite="lax",
                        secure=True)
        return resp

    @app.get("/api/auth/google/callback")
    def auth_google_back(request: Request, state: str = "", code: str = "", error: str = ""):
        """A volta do Google: troca o código pela conta, abre a sessão e leva para a página."""
        def back(problem: str | None = None, done: bool = True):
            resp = RedirectResponse("/" + ("?" + urllib.parse.urlencode({"login_erro": problem}) if problem else ""), 303)
            if done:  # um retorno que não é deste navegador não atrapalha a entrada que está em andamento nele
                resp.delete_cookie(STATE_COOKIE, path="/api/auth/google")
            return resp

        if not state or request.cookies.get(STATE_COOKIE) != state:
            return back("A entrada começou em outra aba ou outro navegador. Tente de novo.", done=False)
        pending = web_logins.pop(state, None)
        if pending is None or pending["at"] < time.time() - 600:
            return back("A entrada expirou. Tente de novo.")
        if error:
            return back("O Google não autorizou a entrada." + (
                " Se o app do Google Cloud estiver em teste, a sua conta precisa estar na lista de usuários de teste."
                if error == "access_denied" else f" ({error})"))
        try:
            info = auth.finish_web_login(settings, code, pending["verifier"])
        except (RuntimeError, OSError) as e:
            return back(str(e))
        try:
            _, token = welcome(info)
        except PermissionError as e:
            return back(str(e))
        return with_session(back(), token, request)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request):
        auth.end_session(con(), session_cookie(request.headers.get("cookie")))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    @app.get("/api/shares")
    def shares_list():
        c, user = con(), viewer().user
        return {"mine": auth.shares(c, user.id), "with_me": [user_json(u) for u in auth.shared_with(c, user)]}

    @app.post("/api/shares")
    def share_add(body: ShareBody):
        """Deixa outro e-mail ver os seus dados (só ver: pipelines, coleção, lacrados e Resumo)."""
        email = body.email.strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(400, "Informe um e-mail válido.")
        if email == viewer().user.email:
            raise HTTPException(400, "Esse é o seu próprio e-mail.")
        auth.share(con(), me(), email)
        return {"mine": auth.shares(con(), me())}

    @app.delete("/api/shares/{email}")
    def share_remove(email: str):
        auth.unshare(con(), me(), email)
        return {"mine": auth.shares(con(), me())}

    # --- arquivos ------------------------------------------------------------------------------

    def file_user(request: Request) -> auth.User | None:
        """Arquivos (vídeos, recortes, logos) ficam fora da API: a sessão é conferida aqui."""
        return auth.request_user(con(), settings, session_cookie(request.headers.get("cookie")))

    @app.get("/runs/{run_id}/{path:path}")
    def run_file(run_id: int, path: str, request: Request, k: str | None = None):
        """Arquivo de uma pipeline: para o dono e para quem ele compartilhou; o Instagram usa o link assinado."""
        row = con().execute("SELECT dir, user_id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Arquivo não encontrado.")
        if not auth.signed(settings, f"{run_id}/{path}", k):
            user = file_user(request)
            if user is None or not auth.can_view(con(), user, row["user_id"]):
                raise HTTPException(404, "Arquivo não encontrado.")
        folder = (settings.root / row["dir"]).resolve()
        target = (folder / path).resolve()
        if not target.is_relative_to(folder) or not target.is_file():
            raise HTTPException(404, "Arquivo não encontrado.")
        return FileResponse(target)

    @app.get("/logos/{name}")
    def logo_file(name: str, request: Request):
        user = file_user(request)
        path = logo.path(dataclasses.replace(settings, account=user.id), name) if user else None
        if path is None:
            raise HTTPException(404, "Logo não encontrado.")
        return FileResponse(path)
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
