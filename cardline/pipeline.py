"""Pipeline de uma abertura: passos persistidos, com progresso, re-executáveis a partir de qualquer ponto.

Cada run tem uma pasta (`runs/<id>/`) com os artefatos dos passos:

    scan     scan.json, track.json, crops/   identifica as cartas e quando cada uma aparece
    verify   scan.json → check               (opcional) confere cada carta com um modelo de visão local
    prices   scan.json → price_usd           atualiza os preços do set e precifica cada carta
    commit   tabela collection (run_id)      registra as cartas na coleção, substituindo as anteriores do run
    overlay  overlay.mp4                     vídeo com os preços sobrepostos
    narrate  narrado.mp4, narracao/          (opcional) roteiro sem spoiler lido por uma voz em português

Há dois tipos: "abertura" (booster: todos os passos, com o vídeo) e "cadastro" (cartas que você já tem:
termina ao registrar na coleção, sem valor pago nem vídeo, e sem deduzir a foil pela estrutura do booster).

Corrigir o resultado de um passo (ex.: `cardline edit` no scan) marca os seguintes como
desatualizados; executar o run de novo a partir dali refaz só o resto, e as cartas registradas
na coleção mudam junto.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db, lorcast
from .collection import load_scan, register, save_scan
from .config import Settings
from .money import money_for, to_usd

STEPS = [
    ("scan", "Identificar cartas"),
    ("verify", "Conferir com IA local"),
    ("prices", "Atualizar preços"),
    ("commit", "Registrar na coleção"),
    ("overlay", "Gerar vídeo com overlay"),
    ("narrate", "Narrar o vídeo"),
]
STEP_NAMES = [name for name, _ in STEPS]
LABELS = dict(STEPS)
KINDS = {
    "abertura": ("Abertura de booster", STEP_NAMES),
    "cadastro": ("Cadastro de coleção", [n for n in STEP_NAMES if n not in ("overlay", "narrate")]),
}


def steps_for(kind: str) -> list[str]:
    return KINDS[kind][1]


DEFAULT_VERIFY_MODEL = "qwen3.5:4b"


class Skip(Exception):
    """O passo não se aplica a este run (ex.: overlay desligado)."""


class DuplicateVideo(Exception):
    def __init__(self, run_id: int):
        super().__init__(f"Este vídeo já foi processado na pipeline #{run_id}.")
        self.run_id = run_id


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _stored_path(settings: Settings, path: Path) -> str:
    """Caminho relativo à raiz do projeto quando possível (o banco continua válido se a pasta mudar)."""
    path = path.resolve()
    try:
        return path.relative_to(settings.root).as_posix()
    except ValueError:
        return str(path)


def safe_filename(name: str) -> str:
    stem, ext = os.path.splitext(Path(name).name)
    return (re.sub(r"[^\w.-]+", "_", stem).strip("._") or "video") + (ext.lower() or ".mp4")


def create_run(
    settings: Settings,
    video: Path,
    *,
    video_name: str | None = None,
    sha1: str | None = None,
    paid: float | None = None,
    paid_currency: str = "BRL",
    set_hint: str | None = None,
    overlay: bool = True,
    verify: bool | None = None,
    currency: str | None = None,
    move: bool = False,
    kind: str = "abertura",
    narration: bool = False,
) -> int:
    """Cadastra um run na fila. Com `move`, o vídeo (um upload) passa a morar na pasta do run."""
    if kind not in KINDS:
        raise ValueError(f"Tipo de pipeline desconhecido: {kind}")
    if kind == "cadastro":  # sem compra e sem vídeo de saída
        paid, overlay = None, False
    narration = narration and overlay  # narra o vídeo com overlay
    con = db.connect(settings.db_path)
    sha1 = sha1 or sha1_file(video)
    dup = con.execute("SELECT id FROM runs WHERE video_sha1 = ?", (sha1,)).fetchone()
    if dup:
        raise DuplicateVideo(dup["id"])
    options = {
        "overlay": overlay,
        "verify": bool(settings.verify_model) if verify is None else verify,
        "currency": (currency or settings.currency).upper(),
        "narration": narration,
    }
    paid_currency = paid_currency.upper()
    with con:
        run_id = con.execute(
            "INSERT INTO runs(kind, video, video_name, video_sha1, dir, status, paid, paid_currency, paid_usd, set_hint,"
            " options, created_at) VALUES (?, ?, ?, ?, '', 'queued', ?, ?, ?, ?, ?, ?)",
            (kind, _stored_path(settings, video), video_name or video.name, sha1, paid, paid_currency if paid else None,
             to_usd(settings, paid, paid_currency), set_hint or None, json.dumps(options), db.now()),
        ).lastrowid
        folder = settings.runs_dir / str(run_id)
        folder.mkdir(parents=True, exist_ok=True)
        if move:
            dest = folder / safe_filename(video_name or video.name)
            shutil.move(video, dest)
            video = dest
        con.execute(
            "UPDATE runs SET video = ?, dir = ? WHERE id = ?",
            (_stored_path(settings, video), _stored_path(settings, folder), run_id),
        )
        con.executemany(
            "INSERT INTO run_steps(run_id, name, status) VALUES (?, ?, 'pending')", [(run_id, n) for n in steps_for(kind)]
        )
    return run_id


# --- execução ---------------------------------------------------------------------------------


@dataclass
class RunContext:
    settings: Settings
    con: sqlite3.Connection
    run_id: int
    step: str = ""
    _last: float = 0.0

    @property
    def run(self) -> sqlite3.Row:
        return self.con.execute("SELECT * FROM runs WHERE id = ?", (self.run_id,)).fetchone()

    @property
    def dir(self) -> Path:
        return self.settings.root / self.run["dir"]

    @property
    def options(self) -> dict:
        return json.loads(self.run["options"])

    def progress(self, fraction: float | None, message: str) -> None:
        """Atualiza o progresso do passo no banco (a página acompanha por ele), no máximo ~3x/s."""
        now = time.monotonic()
        if now - self._last < 0.33 and (fraction is None or fraction < 1):
            return
        self._last = now
        with self.con:
            self.con.execute("UPDATE runs SET progress = ?, message = ? WHERE id = ?", (fraction, message, self.run_id))
        if sys.stdout.isatty():
            print(f"\r  {message}".ljust(72), end="", flush=True)


def _scan(ctx: RunContext) -> str:
    from .scan import scan_video

    run = ctx.run
    sets = [s.strip() for s in run["set_hint"].split(",")] if run["set_hint"] else None
    result = scan_video(ctx.settings, ctx.settings.root / run["video"], ctx.dir, sets, ctx.progress, run["kind"])
    with ctx.con:
        ctx.con.execute("UPDATE runs SET recorded_at = ? WHERE id = ?", (result["recorded_at"], ctx.run_id))
    n, size = len(result["cards"]), ctx.settings.pack_size
    msg = f"{n} cartas · set {', '.join(result['sets'])}"
    if run["kind"] == "abertura" and n % size:
        msg += f" · atenção: {n} cartas não fecham boosters de {size}"
    return msg


def _verify(ctx: RunContext) -> str:
    from .verify import Unavailable, verify

    if not ctx.options.get("verify"):
        raise Skip("desligado")
    model = ctx.settings.verify_model or DEFAULT_VERIFY_MODEL
    scan = load_scan(ctx.dir)
    try:
        warnings = verify(ctx.settings, ctx.dir, scan, model, ctx.progress)
    except Unavailable as e:
        raise Skip(str(e)) from e
    save_scan(ctx.dir, scan)
    summary = f"{model}: {len(scan['cards']) - len(warnings)} ok, {len(warnings)} para conferir"
    return "\n".join([summary, *warnings])


def opening_day(run: sqlite3.Row) -> str:
    """Dia da abertura (data local da gravação do vídeo; senão, de quando a pipeline foi criada)."""
    stamp = run["recorded_at"] or run["created_at"]
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return stamp[:10]


def _prices(ctx: RunContext) -> str:
    """Atualiza os preços de hoje do set e precifica as cartas que ainda não têm preço da abertura.

    O preço da abertura é fixado na primeira vez e não muda ao reprocessar. Uma carta que entra
    depois (trocada, inserida, foil corrigida) recebe o preço do dia da abertura, se houver histórico.
    """
    scan = load_scan(ctx.dir)
    fetched_at, offline = db.now(), []
    for code in sorted({c["set"] for c in scan["cards"]}):
        ctx.progress(None, f"Buscando preços atuais do set {code}")
        row = ctx.con.execute("SELECT id FROM sets WHERE code = ?", (code,)).fetchone()
        try:
            cards = lorcast.fetch_set_cards(row["id"])
        except OSError:
            offline.append(code)
            continue
        with ctx.con:
            db.upsert_cards(ctx.con, cards, fetched_at)
    day, kept = opening_day(ctx.run), 0
    for c in scan["cards"]:
        key = f"{c['card_id']}:{int(c['foil'])}"
        if c.get("price_usd") is not None and c.get("price_key", key) == key:  # sem price_key: scan antigo
            c["price_key"] = key
            kept += 1
            continue
        c["price_usd"], c["price_key"] = db.price_on(ctx.con, c["card_id"], c["foil"], day), key
    save_scan(ctx.dir, scan)
    total = sum(c["price_usd"] or 0 for c in scan["cards"])
    moment = "no cadastro" if ctx.run["kind"] == "cadastro" else "na abertura"
    msg = f"cartas valiam {money_for(ctx.settings, 'USD').fmt(total)} {moment}"
    if kept:
        msg += f" ({kept} com o preço {'do cadastro' if ctx.run['kind'] == 'cadastro' else 'da abertura'} mantido)"
    if offline:
        msg += f" · sem conexão: preços em cache para o set {', '.join(offline)}"
    return msg


def _commit(ctx: RunContext) -> str:
    scan = load_scan(ctx.dir)
    register(ctx.con, ctx.run_id, scan)
    db.record_value(ctx.con)
    return f"{len(scan['cards'])} cartas registradas na coleção"


def _overlay(ctx: RunContext) -> str:
    from .overlay import render

    if not ctx.options.get("overlay", True):
        raise Skip("desligado")
    money = money_for(ctx.settings, ctx.options.get("currency"))
    run = ctx.run
    render(ctx.settings, load_scan(ctx.dir), ctx.dir / "overlay.mp4", money, ctx.progress, run["paid_usd"],
           run["paid"], run["paid_currency"])
    return "overlay.mp4 pronto" + (f" · capa de {ctx.settings.intro_seconds:g}s" if ctx.settings.intro_seconds > 0 else "")


def _narrate(ctx: RunContext) -> str:
    if not ctx.options.get("narration"):
        raise Skip("desligada")
    if not (ctx.dir / "overlay.mp4").exists():
        raise Skip("sem vídeo com overlay")
    from .narration import narrate

    return narrate(ctx.settings, ctx.run, ctx.dir, ctx.progress)


STEP_FUNCS = {"scan": _scan, "verify": _verify, "prices": _prices, "commit": _commit, "overlay": _overlay,
              "narrate": _narrate}


def _ensure_steps(con: sqlite3.Connection, run_id: int, kind: str) -> None:
    """Runs criados antes de um passo existir ganham a linha dele (ex.: narrar, nas aberturas antigas)."""
    with con:
        con.executemany("INSERT OR IGNORE INTO run_steps(run_id, name, status) VALUES (?, ?, 'pending')",
                        [(run_id, n) for n in steps_for(kind)])


def first_step_to_run(con: sqlite3.Connection, run: sqlite3.Row) -> str:
    if run["resume_from"]:
        return run["resume_from"]
    done = {r["name"] for r in con.execute(
        "SELECT name FROM run_steps WHERE run_id = ? AND status IN ('done', 'skipped')", (run["id"],))}
    names = steps_for(run["kind"])
    return next((n for n in names if n not in done), names[0])


def execute(settings: Settings, run_id: int, from_step: str | None = None) -> bool:
    """Executa o run do passo `from_step` (ou de onde parou) até o fim. Devolve se terminou bem."""
    con = db.connect(settings.db_path)
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise SystemExit(f"Pipeline #{run_id} não existe.")
    _ensure_steps(con, run_id, run["kind"])
    start = from_step or first_step_to_run(con, run)
    names = steps_for(run["kind"])
    todo = names[names.index(start):]
    with con:
        con.execute(
            "UPDATE runs SET status = 'running', pid = ?, started_at = ?, finished_at = NULL, error = NULL,"
            " resume_from = NULL WHERE id = ?", (os.getpid(), db.now(), run_id))
        con.executemany(
            "UPDATE run_steps SET status = 'pending', message = NULL, started_at = NULL, finished_at = NULL"
            " WHERE run_id = ? AND name = ?", [(run_id, n) for n in todo])
    ctx = RunContext(settings, con, run_id)
    for name in todo:
        ctx.step, ctx._last = name, 0.0
        with con:
            con.execute("UPDATE runs SET step = ?, progress = 0, message = ? WHERE id = ?", (name, LABELS[name], run_id))
            con.execute("UPDATE run_steps SET status = 'running', started_at = ? WHERE run_id = ? AND name = ?",
                        (db.now(), run_id, name))
        print(f"→ {LABELS[name]}", flush=True)
        try:
            message, status = STEP_FUNCS[name](ctx), "done"
        except Skip as e:
            message, status = str(e), "skipped"
        except Exception as e:  # qualquer falha vira estado do run, para a página mostrar e permitir retomar
            with con:
                con.execute("UPDATE run_steps SET status = 'failed', message = ?, finished_at = ?"
                            " WHERE run_id = ? AND name = ?", (str(e), db.now(), run_id, name))
                con.execute("UPDATE runs SET status = 'failed', error = ?, message = ?, resume_from = ?,"
                            " finished_at = ?, pid = NULL WHERE id = ?",
                            (traceback.format_exc(), str(e), name, db.now(), run_id))
            print(f"\n✗ {LABELS[name]}: {e}", flush=True)
            traceback.print_exc()
            return False
        with con:
            con.execute("UPDATE run_steps SET status = ?, message = ?, finished_at = ? WHERE run_id = ? AND name = ?",
                        (status, message, db.now(), run_id, name))
        print(f"\r  {'ok' if status == 'done' else 'pulado'}: {message}".ljust(72), flush=True)
    with con:
        con.execute("UPDATE runs SET status = 'done', step = NULL, progress = NULL, message = NULL, finished_at = ?,"
                    " pid = NULL WHERE id = ?", (db.now(), run_id))
    return True


# --- operações sobre runs ---------------------------------------------------------------------


def enqueue(settings: Settings, run_id: int, from_step: str | None = None) -> None:
    """Põe o run na fila do servidor para rodar a partir de `from_step` (ou de onde parou)."""
    con = db.connect(settings.db_path)
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"Pipeline #{run_id} não existe.")
    if from_step is not None and from_step not in steps_for(run["kind"]):
        raise ValueError(f"Passo desconhecido para esta pipeline: {from_step}")
    if run["status"] in ("queued", "running"):
        raise RuntimeError(f"A pipeline #{run_id} já está na fila ou rodando.")
    with con:
        con.execute("UPDATE runs SET status = 'queued', resume_from = ?, error = NULL WHERE id = ?",
                    (from_step or first_step_to_run(con, run), run_id))


def mark_stale(settings: Settings, run_id: int, from_step: str) -> None:
    """Depois de uma correção, os passos a partir de `from_step` precisam rodar de novo."""
    con = db.connect(settings.db_path)
    run = con.execute("SELECT kind, resume_from FROM runs WHERE id = ?", (run_id,)).fetchone()
    names = steps_for(run["kind"])
    if from_step not in names:
        return
    _ensure_steps(con, run_id, run["kind"])
    if run["resume_from"] in names and names.index(run["resume_from"]) < names.index(from_step):
        from_step = run["resume_from"]  # já havia uma correção antes: continua de lá, senão ela se perde
    with con:
        con.executemany(
            "UPDATE run_steps SET status = 'stale' WHERE run_id = ? AND name = ? AND status != 'pending'",
            [(run_id, n) for n in names[names.index(from_step):]],
        )
        con.execute("UPDATE runs SET status = 'stale', resume_from = ? WHERE id = ? AND status NOT IN ('queued', 'running')",
                    (from_step, run_id))


def update_paid(settings: Settings, run_id: int, paid: float | None, currency: str) -> None:
    con = db.connect(settings.db_path)
    if con.execute("SELECT kind FROM runs WHERE id = ?", (run_id,)).fetchone()["kind"] != "abertura":
        raise ValueError("Cadastro de coleção não tem valor pago.")
    with con:
        con.execute("UPDATE runs SET paid = ?, paid_currency = ?, paid_usd = ? WHERE id = ?",
                    (paid, currency.upper() if paid else None, to_usd(settings, paid, currency), run_id))
    # o resumo do vídeo mostra o valor pago
    if con.execute("SELECT status FROM run_steps WHERE run_id = ? AND name = 'overlay'", (run_id,)).fetchone()[0] == "done":
        mark_stale(settings, run_id, "overlay")


def update_overlay_currency(settings: Settings, run_id: int, currency: str) -> None:
    """Muda a moeda do vídeo com overlay; o vídeo fica desatualizado até reprocessar."""
    con = db.connect(settings.db_path)
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"Pipeline #{run_id} não existe.")
    if run["status"] in ("queued", "running"):
        raise RuntimeError("A pipeline está rodando; espere terminar para mudar a moeda do vídeo.")
    if run["kind"] != "abertura":
        raise ValueError("Cadastro de coleção não gera vídeo.")
    options = json.loads(run["options"])
    if options.get("currency") == currency:
        return
    options["currency"] = currency
    with con:
        con.execute("UPDATE runs SET options = ? WHERE id = ?", (json.dumps(options), run_id))
    step = con.execute("SELECT status FROM run_steps WHERE run_id = ? AND name = 'overlay'", (run_id,)).fetchone()
    if step and step["status"] in ("done", "stale"):
        mark_stale(settings, run_id, "overlay")


def update_narration(settings: Settings, run_id: int, enabled: bool) -> None:
    """Liga ou desliga a narração. Ligar deixa o passo pendente; desligar apaga o vídeo narrado."""
    con = db.connect(settings.db_path)
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"Pipeline #{run_id} não existe.")
    if run["status"] in ("queued", "running"):
        raise RuntimeError("A pipeline está rodando; espere terminar para mudar a narração.")
    if run["kind"] != "abertura":
        raise ValueError("Cadastro de coleção não gera vídeo.")
    options = json.loads(run["options"])
    if bool(options.get("narration")) == enabled:
        return
    if enabled and not options.get("overlay", True):
        raise ValueError("Sem o vídeo com overlay não há o que narrar.")
    options["narration"] = enabled
    with con:
        con.execute("UPDATE runs SET options = ? WHERE id = ?", (json.dumps(options), run_id))
    _ensure_steps(con, run_id, run["kind"])
    if enabled:
        mark_stale(settings, run_id, "narrate")
        return
    (settings.root / run["dir"] / "narrado.mp4").unlink(missing_ok=True)
    with con:
        con.execute("UPDATE run_steps SET status = 'skipped', message = 'desligada', started_at = NULL, finished_at = NULL"
                    " WHERE run_id = ? AND name = 'narrate'", (run_id,))
        if run["status"] == "stale" and run["resume_from"] == "narrate":  # só faltava narrar
            con.execute("UPDATE runs SET status = 'done', resume_from = NULL WHERE id = ?", (run_id,))


def delete_run(settings: Settings, run_id: int) -> None:
    """Apaga o run, as cartas que ele registrou e a pasta dele (o vídeo original fora de runs/ fica)."""
    con = db.connect(settings.db_path)
    run = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise LookupError(f"Pipeline #{run_id} não existe.")
    if run["status"] == "running":
        raise RuntimeError("A pipeline está rodando; espere terminar para apagar.")
    with con:
        con.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    db.record_value(con)  # as cartas dela saíram da coleção
    folder = (settings.root / run["dir"]).resolve()
    if run["dir"] and folder.is_relative_to(settings.runs_dir.resolve()) and folder != settings.runs_dir.resolve():
        shutil.rmtree(folder, ignore_errors=True)


def recover_interrupted(settings: Settings) -> None:
    """Runs marcados como rodando cujo processo não existe mais viram 'interrupted' (retomáveis)."""
    con = db.connect(settings.db_path)
    for run in con.execute("SELECT id, pid, step FROM runs WHERE status = 'running'").fetchall():
        if run["pid"] and _alive(run["pid"]):
            continue
        with con:
            con.execute("UPDATE runs SET status = 'interrupted', resume_from = ?, pid = NULL WHERE id = ?",
                        (run["step"], run["id"]))
            con.execute("UPDATE run_steps SET status = 'failed', message = 'interrompido'"
                        " WHERE run_id = ? AND status = 'running'", (run["id"],))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
