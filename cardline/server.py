"""Servidor local: página da coleção/pipelines, upload de vídeos e execução das pipelines em fila.

Cada pipeline roda num processo próprio (`cardline run <id>`), um por vez; o progresso vai para o
banco e a página acompanha por polling. Se o servidor cair, os runs em andamento ficam
'interrupted' e podem ser retomados de onde pararam.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, pipeline, rarity
from .catalog import is_booster_set
from .collection import load_scan
from .config import Settings
from .index import image_path
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


class RerunBody(BaseModel):
    from_step: str | None = None


class PaidBody(BaseModel):
    paid: float | None = None
    paid_currency: str = "BRL"


def create_app(settings: Settings) -> FastAPI:
    runner = Runner(settings)
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

    def run_json(c, run, detail: bool = False) -> dict:
        folder = settings.root / run["dir"]
        url = f"/runs/{folder.name}"
        scan = load_scan(folder) if (folder / "scan.json").exists() else None
        cards = scan["cards"] if scan else []
        ids = sorted({x["card_id"] for x in cards})
        rows = {r["id"]: r for r in c.execute(
            f"SELECT * FROM cards WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
        priced = scan is not None and any("price_usd" in x for x in cards)
        steps = {r["name"]: dict(r) for r in c.execute("SELECT * FROM run_steps WHERE run_id = ?", (run["id"],))}
        out = {
            "id": run["id"], "status": run["status"], "step": run["step"], "progress": run["progress"],
            "message": run["message"], "error": (run["error"] or "").strip().splitlines()[-1:] or None,
            "video_name": run["video_name"], "created_at": run["created_at"], "recorded_at": run["recorded_at"],
            "started_at": run["started_at"], "finished_at": run["finished_at"], "resume_from": run["resume_from"],
            "paid": run["paid"], "paid_currency": run["paid_currency"], "paid_usd": run["paid_usd"],
            "options": json.loads(run["options"]), "sets": scan["sets"] if scan else None,
            "n_cards": len(cards), "packs": max((x["pack"] for x in cards), default=0),
            "value_open": sum(x.get("price_usd") or 0 for x in cards) if priced else None,
            "value_now": sum(db.price_usd(rows[x["card_id"]], x["foil"]) or 0 for x in cards) if cards else None,
            "thumbs": [f"{url}/{x['crop']}" for x in cards if x.get("crop")][:24],
            "overlay": f"{url}/overlay.mp4" if (folder / "overlay.mp4").exists() else None,
            "poster": f"{url}/overlay.jpg" if (folder / "overlay.jpg").exists() else None,
            "steps": [{"name": n, "label": pipeline.LABELS[n], **{k: steps.get(n, {}).get(k) for k in
                       ("status", "message", "started_at", "finished_at")}} for n in pipeline.STEP_NAMES],
        }
        if detail:
            out["cards"] = [
                {
                    "n": n, "card": x["card_id"], "foil": x["foil"], "foil_reason": x.get("foil_reason"),
                    "pack": x["pack"], "slot": x["slot"], "t": x["t"], "price_open": x.get("price_usd"),
                    "price_now": db.price_usd(rows[x["card_id"]], x["foil"]),
                    "crop": f"{url}/{x['crop']}" if x.get("crop") else None, "inliers": x.get("inliers"),
                    "manual": x.get("manual", False), "check": x.get("check"),
                }
                for n, x in enumerate(cards, 1)
            ]
            out["card_info"] = {cid: card_json(r) for cid, r in rows.items()}
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
            "sets": [{"code": r["code"], "name": r["name"], "booster": is_booster_set(r["code"])}
                     for r in c.execute("SELECT code, name FROM sets ORDER BY released_at, code")],
            "rarities": [[k, label, color] for k, (label, color) in rarity.RARITIES.items()],
            "inks": [[k, label, color] for k, (label, color) in rarity.INKS.items()],
            "prices_updated_at": c.execute("SELECT MAX(prices_updated_at) FROM cards").fetchone()[0],
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
    ):
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
                    currency=currency, move=True,
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

    @app.patch("/api/runs/{run_id}")
    def set_paid(run_id: int, body: PaidBody):
        if body.paid_currency.upper() not in CURRENCIES:
            raise HTTPException(400, "Moeda deve ser USD ou BRL.")
        pipeline.update_paid(settings, run_id, body.paid, body.paid_currency)
        return {"ok": True}

    @app.delete("/api/runs/{run_id}")
    def delete(run_id: int):
        try:
            pipeline.delete_run(settings, run_id)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except RuntimeError as e:
            raise HTTPException(409, str(e)) from e
        return {"ok": True}

    # --- arquivos ------------------------------------------------------------------------------

    app.mount("/runs", StaticFiles(directory=settings.runs_dir), name="runs")
    app.mount("/img", StaticFiles(directory=settings.images_dir, check_dir=False), name="img")
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


def serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    print(f"cardline em http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")
