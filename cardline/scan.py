"""Scan de um vídeo de abertura: identifica cada carta revelada e o instante em que ela aparece.

Fluxo: frames a `analysis_fps` → matching em paralelo → "carta no topo" por frame →
segmentação temporal (cada nova carta no topo da pilha = uma carta revelada) →
recorte retificado e foil (pela estrutura do booster) de cada carta → `<run>/scan.json`.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from collections import deque
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np

from . import db
from .config import Settings, load_settings
from .foil import assign_foils
from .index import indexed_sets, load_index
from .matcher import Matcher
from .video import VideoInfo, probe, read_frames

Progress = Callable[[float | None, str], None]  # (fração 0..1 ou None, mensagem)

ANALYSIS_SHORT_SIDE = 1080
MIN_INLIERS = 12  # confiança mínima para um frame contar como "carta X no topo"
MIN_RUN = 3  # frames (na taxa de análise) para uma carta valer como revelada
BACKFILL_S = 2.0  # quanto o início de uma carta pode recuar até os primeiros matches fracos dela
SCAN_VERSION = 1


# --- processo de trabalho -------------------------------------------------------------------

_worker: dict = {}


def _init_worker(root: str, set_codes: list[str]) -> None:
    cv2.setNumThreads(1)
    settings = load_settings(Path(root))
    ref = load_index(settings, set_codes)
    _worker.update(ref=ref, matcher=Matcher(ref))


def _analyze(job: tuple[int, np.ndarray]) -> dict:
    i, frame = job
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    matches = _worker["matcher"].match(gray)
    h, w = gray.shape
    rec: dict = {
        "i": i,
        "matches": [
            {"card": m.card, "inliers": m.inliers, "votes": m.votes, "quad": (m.quad / [w, h]).round(4).tolist()}
            for m in matches[:3]
        ],
    }
    if matches and matches[0].inliers >= MIN_INLIERS:
        top = matches[0]
        size = tuple(int(v) for v in _worker["ref"].sizes[top.card])
        crop = cv2.warpPerspective(frame, top.H, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        rec["crop"] = cv2.imencode(".jpg", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 90])[1]
        rec["sharpness"] = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY), cv2.CV_32F).var())
    return rec


# --- orquestração ---------------------------------------------------------------------------


def detect_sets(settings: Settings, info: VideoInfo, progress: Progress) -> list[str]:
    """Descobre de que set(s) são as cartas usando um índice leve com todos os sets e ~40 frames."""
    available = indexed_sets(settings)
    if not available:
        raise RuntimeError("Nenhum set indexado; rode `cardline sync` antes.")
    if len(available) == 1:
        return available
    ref = load_index(settings, available, per_card=250)  # só keypoints da arte
    matcher = Matcher(ref)
    sets_of_group: dict[int, set[str]] = {}
    for group, code in zip(ref.groups.tolist(), ref.card_sets):
        sets_of_group.setdefault(group, set()).add(code)
    fps = min(2.0, 40 / max(info.duration, 1))
    expected = int(info.duration * fps) + 1
    tally: dict[str, int] = {}
    for n, frame in enumerate(read_frames(info, ANALYSIS_SHORT_SIDE, fps=fps), 1):
        progress(0.1 * n / expected, f"Detectando o set do booster ({n}/{expected})")
        found = matcher.match(cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY))
        # reimpressões com a mesma arte em outro set não dizem nada sobre o set do booster
        if found and found[0].inliers >= MIN_INLIERS and len(sets_of_group[ref.groups[found[0].card]]) == 1:
            code = ref.card_sets[found[0].card]
            tally[code] = tally.get(code, 0) + 1
    if not tally:
        raise RuntimeError("Nenhuma carta reconhecida no vídeo.")
    total = sum(tally.values())
    return sorted(c for c, n in tally.items() if n >= max(2, 0.15 * total)) or [max(tally, key=tally.get)]


def _segment(records: list[dict], fps: float) -> list[dict]:
    """Agrupa os frames em cartas reveladas: cada sequência estável de uma nova carta no topo."""
    runs: list[dict] = []
    for rec in records:
        top = rec["matches"][0] if rec["matches"] else None
        if not top or top["inliers"] < MIN_INLIERS:
            continue  # frame sem carta confiável (mão na frente, borrão): não quebra a sequência
        if runs and runs[-1]["card"] == top["card"]:
            runs[-1]["frames"].append(rec)
        else:
            runs.append({"card": top["card"], "frames": [rec]})
    events: list[dict] = []
    for run in runs:
        if len(run["frames"]) < MIN_RUN:
            continue  # carta de passagem / ruído
        if events and events[-1]["card"] == run["card"]:
            events[-1]["frames"] += run["frames"]
        else:
            events.append(run)
    # a carta costuma entrar borrada/em movimento: recua o início até os primeiros matches (fracos) dela
    prev_end = -1
    for ev in events:
        first = ev["frames"][0]["i"]
        ev["start"] = first
        for j in range(first - 1, max(prev_end, first - int(BACKFILL_S * fps) - 1), -1):
            top = records[j]["matches"][0] if records[j]["matches"] else None
            if top and top["card"] == ev["card"]:
                ev["start"] = j
            elif top and top["inliers"] >= MIN_INLIERS:
                break  # outra carta firme no topo
        prev_end = ev["frames"][-1]["i"]
    return events


def scan_video(
    settings: Settings, video: Path, run_dir: Path, set_codes: list[str] | None, progress: Progress
) -> dict:
    """Identifica as cartas do vídeo; grava `scan.json`, `track.json` e `crops/` em `run_dir`."""
    info = probe(video)
    (run_dir / "crops").mkdir(parents=True, exist_ok=True)
    for old in (run_dir / "crops").glob("*.jpg"):  # de uma execução anterior
        old.unlink()
    set_codes = set_codes or detect_sets(settings, info, progress)
    ref = load_index(settings, set_codes)

    fps = settings.analysis_fps
    expected = int(info.duration * fps) + 1
    records: list[dict] = []
    pending: deque = deque()
    workers = settings.workers
    # spawn, não fork: o processo principal já usou o pool de threads do OpenCV (detecção de set),
    # e um fork nesse estado deixa os workers travados num mutex herdado
    spawn = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(workers, spawn, _init_worker, (str(settings.root), set_codes)) as ex:
        for job in enumerate(read_frames(info, ANALYSIS_SHORT_SIDE, fps=fps)):
            pending.append(ex.submit(_analyze, job))
            while len(pending) >= workers * 3 or (pending and pending[0].done()):  # limita frames em memória
                records.append(pending.popleft().result())
                progress(0.1 + 0.9 * len(records) / expected, f"Analisando frames ({len(records)}/{expected})")
        while pending:
            records.append(pending.popleft().result())
            progress(0.1 + 0.9 * len(records) / expected, f"Analisando frames ({len(records)}/{expected})")

    con = db.connect(settings.db_path)
    events = _segment(records, fps)
    cards = []
    for n, ev in enumerate(events):
        frames = ev["frames"]
        best = max(frames, key=lambda r: r["matches"][0]["inliers"] * np.sqrt(r["sharpness"]))
        crop_rel = f"crops/{n + 1:02d}.jpg"
        (run_dir / crop_rel).write_bytes(best["crop"].tobytes())
        row = db.card(con, ref.card_ids[ev["card"]])
        alternatives = {}
        for rec in frames:
            for m in rec["matches"][1:]:
                alternatives[m["card"]] = max(alternatives.get(m["card"], 0), m["inliers"])
        cards.append({
            "slot": n % settings.pack_size + 1,
            "pack": n // settings.pack_size + 1,
            "card_id": row["id"],
            "set": row["set_code"],
            "number": row["number"],
            "name": row["name"],
            "version": row["version"],
            "rarity": row["rarity"],
            "ink": row["ink"],
            "foil": False,
            "foil_reason": None,
            "t": round(ev["start"] / fps, 2),
            "inliers": max(r["matches"][0]["inliers"] for r in frames),
            "frames": len(frames),
            "quad": np.median([r["matches"][0]["quad"] for r in frames], axis=0).round(4).tolist(),
            "crop": crop_rel,
            "alternatives": [
                {"card_id": ref.card_ids[c], "inliers": n}
                for c, n in sorted(alternatives.items(), key=lambda kv: -kv[1])[:3]
            ],
        })
    assign_foils(cards, settings.pack_size)
    for i, c in enumerate(cards):
        c["t_end"] = cards[i + 1]["t"] if i + 1 < len(cards) else round(info.duration, 2)

    result = {
        "version": SCAN_VERSION,
        "video": os.path.relpath(video.resolve(), settings.root),
        "recorded_at": info.creation_time,
        "duration": round(info.duration, 2),
        "size": [info.width, info.height],
        "analysis_fps": fps,
        "sets": set_codes,
        "scanned_at": db.now(),
        "cards": cards,
    }
    (run_dir / "scan.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    track = [
        {"t": round(r["i"] / fps, 2), "matches": [{**m, "card": ref.card_ids[m["card"]]} for m in r["matches"]]}
        for r in records
    ]
    (run_dir / "track.json").write_text(json.dumps(track))
    return result
