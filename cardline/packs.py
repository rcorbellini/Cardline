"""Pipeline de lacrados: identifica cada booster colocado na pilha do vídeo, como o scan faz com as cartas.

Referência: a foto oficial do booster de cada set (o ícone do set, que vem do TCGplayer). Em cada frame, o
booster mais visível (SIFT + homografia, o mesmo Matcher das cartas); no tempo, cada sequência de um set no
topo da pilha é um booster colocado. Dois boosters seguidos do mesmo set se separam quando a arte muda, ou
quando a pilha para num lugar, o booster some num movimento (cai o número de pontos casados) e para noutro.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from collections import deque
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import db
from .config import Settings, load_settings
from .index import RefIndex, root_sift
from .matcher import Matcher
from .scan import Progress
from .video import probe, read_frames

REF_SIDE = 700  # lado maior da foto de referência
REF_FEATURES = 1500
ANALYSIS_FPS = 4.0
ANALYSIS_SHORT_SIDE = 720
MIN_INLIERS = 18  # confiança mínima para um frame contar como "booster X no topo"
MIN_RUN = 2  # frames seguidos para valer como booster (menos que isso é passagem)
SAME_ART = 0.5  # correlação entre as artes (o meio do booster) abaixo disso: outra arte, outro booster
STILL = 0.035  # o booster parado: centro dentro disso (fração do quadro) por PLATEAU frames
PLATEAU = 3
SHIFT = 0.08  # outro booster do mesmo set: parado noutro lugar...
DIP = 0.7  # ... depois de um movimento em que os pontos casados caem abaixo dessa fração
SIG = (40, 72)  # recorte pequeno (largura, altura) para comparar a arte
SCAN_VERSION = 1


def reference_sets(con) -> list[tuple[str, str]]:
    """(código, caminho do ícone) dos sets com a foto do booster do TCGplayer."""
    return [(r["code"], r["icon"]) for r in con.execute(
        "SELECT code, icon FROM sets WHERE icon_source = 'tcgplayer' AND icon IS NOT NULL ORDER BY released_at, code")]


def _ref_features(path: Path) -> tuple[np.ndarray, np.ndarray, tuple[int, int]]:
    img = Image.open(path).convert("RGBA")
    scale = REF_SIDE / max(img.size)
    img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    rgb = np.asarray(Image.alpha_composite(Image.new("RGBA", img.size, (128, 128, 128, 255)), img).convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = (np.asarray(img)[..., 3] > 128).astype(np.uint8) * 255  # só o booster, sem o fundo
    kps, desc = cv2.SIFT_create(nfeatures=REF_FEATURES).detectAndCompute(gray, mask)
    if desc is None:
        return np.zeros((0, 2), np.float32), np.zeros((0, 128), np.float32), gray.shape[::-1]
    return np.float32([k.pt for k in kps]), desc, gray.shape[::-1]


def load_index(settings: Settings, refs: list[tuple[str, str]]) -> RefIndex:
    """Índice das fotos dos boosters (em cache; refeito quando um ícone muda)."""
    path = settings.index_dir / "packs.npz"
    key = json.dumps([(code, icon, (settings.root / icon).stat().st_mtime_ns) for code, icon in refs])
    if path.exists():
        with np.load(path) as z:
            if str(z["key"]) == key:
                counts = z["counts"]
                return _ref_index(z["codes"].tolist(), z["kp"], z["desc"], counts, z["sizes"])
    feats = [_ref_features(settings.root / icon) for _, icon in refs]
    codes = [code for code, _ in refs]
    counts = np.array([len(f[0]) for f in feats], np.int64)
    kp, desc = np.concatenate([f[0] for f in feats]), np.concatenate([f[1] for f in feats])
    sizes = np.array([f[2] for f in feats], np.int32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "wb") as fh:
        np.savez(fh, key=key, codes=np.array(codes), kp=kp, desc=desc, counts=counts, sizes=sizes)
    tmp.replace(path)
    return _ref_index(codes, kp, desc, counts, sizes)


def _ref_index(codes: list[str], kp, desc, counts, sizes) -> RefIndex:
    counts = np.asarray(counts, np.int64)
    return RefIndex(card_ids=list(codes), card_sets=list(codes), kp=np.asarray(kp, np.float32), desc=root_sift(desc),
                    owner=np.repeat(np.arange(len(codes), dtype=np.int32), counts),
                    offsets=np.concatenate([[0], np.cumsum(counts)]), sizes=np.asarray(sizes, np.int32),
                    groups=np.arange(len(codes), dtype=np.int32))  # cada set é um grupo: o topo comum a todos não decide


# --- processo de trabalho -------------------------------------------------------------------

_worker: dict = {}


def _init_worker(root: str, refs: list[tuple[str, str]]) -> None:
    cv2.setNumThreads(1)
    ref = load_index(load_settings(Path(root)), refs)
    _worker.update(ref=ref, matcher=Matcher(ref))


def _analyze(job: tuple[int, np.ndarray]) -> dict:
    i, frame = job
    gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
    found = _worker["matcher"].match(gray)
    h, w = gray.shape
    if not found or found[0].inliers < MIN_INLIERS:
        return {"i": i}
    top = found[0]
    size = tuple(int(v) for v in _worker["ref"].sizes[top.card])
    crop = cv2.warpPerspective(frame, top.H, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
    small = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY), SIG, interpolation=cv2.INTER_AREA).astype(np.float32)
    crop = cv2.resize(crop, (round(crop.shape[1] * 360 / crop.shape[0]), 360), interpolation=cv2.INTER_AREA)
    return {"i": i, "set": _worker["ref"].card_ids[top.card], "inliers": top.inliers,
            "quad": (top.quad / [w, h]).round(4).tolist(), "sig": small,
            "crop": cv2.imencode(".jpg", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 88])[1],
            "sharpness": float(cv2.Laplacian(small, cv2.CV_32F).var())}


# --- segmentação ----------------------------------------------------------------------------


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    a = (a - a.mean()) / (a.std() + 1e-6)
    b = (b - b.mean()) / (b.std() + 1e-6)
    return float((a * b).mean())


def _art(sig: np.ndarray) -> np.ndarray:
    """O meio do booster: o topo (logo) e o nome do set são iguais em todas as artes do set."""
    h, w = sig.shape
    return sig[h // 4: 3 * h // 4, w // 10: w - w // 10]


def _center(rec: dict) -> np.ndarray:
    return np.asarray(rec["quad"]).mean(axis=0)


def _plateaus(frames: list[dict]) -> list[tuple[int, int]]:
    """Trechos (início, fim) em que o booster ficou parado: PLATEAU+ frames com o centro dentro de STILL e a mesma
    arte (outro booster pode cair exatamente no mesmo lugar)."""
    out, start = [], 0
    for k in range(1, len(frames) + 1):
        if k == len(frames) or np.linalg.norm(_center(frames[k]) - np.median([_center(f) for f in frames[start:k]], axis=0)) > STILL \
                or _ncc(_art(frames[k]["sig"]), _art(frames[start]["sig"])) < SAME_ART:
            if k - start >= PLATEAU:
                out.append((start, k - 1))
            start = k
    return out


def _split_same_set(frames: list[dict]) -> list[list[dict]]:
    """Dentro de uma sequência do mesmo set, onde começa outro booster: a arte mudou, ou a pilha parou num
    lugar, houve um movimento (os pontos casados caem) e parou noutro lugar."""
    cuts = []
    plateaus = _plateaus(frames)
    for (a0, a1), (b0, b1) in zip(plateaus, plateaus[1:]):
        a, b = frames[a0:a1 + 1], frames[b0:b1 + 1]
        best_a, best_b = max(a, key=lambda f: f["inliers"]), max(b, key=lambda f: f["inliers"])
        moved = np.linalg.norm(np.median([_center(f) for f in a], 0) - np.median([_center(f) for f in b], 0)) >= SHIFT
        between = frames[a1 + 1:b0]
        dipped = bool(between) and min(f["inliers"] for f in between) < DIP * min(best_a["inliers"], best_b["inliers"])
        if _ncc(_art(best_a["sig"]), _art(best_b["sig"])) < SAME_ART or (moved and dipped):
            cuts.append(a1 + 1 + int(np.argmin([f["inliers"] for f in between])) if between else b0)
    pieces, last = [], 0
    for cut in cuts:
        pieces.append(frames[last:cut])
        last = cut
    pieces.append(frames[last:])
    return [p for p in pieces if p]


def segment(records: list[dict]) -> list[list[dict]]:
    """Os boosters colocados: sequências do mesmo set no topo (frames sem booster não quebram), sem as
    passagens curtas, separando boosters seguidos do mesmo set."""
    runs: list[list[dict]] = []
    for rec in records:
        if "set" not in rec:
            continue  # mão na frente, borrão: não quebra a sequência
        if runs and runs[-1][0]["set"] == rec["set"]:
            runs[-1].append(rec)
        else:
            runs.append([rec])
    merged: list[list[dict]] = []
    for run in runs:
        if len(run) < MIN_RUN:
            continue
        if merged and merged[-1][0]["set"] == run[0]["set"]:
            merged[-1] += run
        else:
            merged.append(run)
    return [piece for run in merged for piece in _split_same_set(run)]


# --- orquestração ---------------------------------------------------------------------------


def scan_video(settings: Settings, video: Path, run_dir: Path, progress: Progress) -> dict:
    """Identifica os boosters do vídeo; grava `scan.json` (packs) e `crops/` em `run_dir`."""
    info = probe(video)
    con = db.connect(settings.db_path)
    refs = reference_sets(con)
    if not refs:
        raise RuntimeError("Nenhum set tem a foto do booster ainda; sincronize os sets (aba Sets) antes.")
    load_index(settings, refs)  # monta o cache antes dos processos de trabalho
    (run_dir / "crops").mkdir(parents=True, exist_ok=True)
    for old in (run_dir / "crops").glob("*.jpg"):
        old.unlink()
    expected = int(info.duration * ANALYSIS_FPS) + 1
    records: list[dict] = []
    pending: deque = deque()
    workers = settings.workers
    spawn = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(workers, spawn, _init_worker, (str(settings.root), refs)) as ex:
        for job in enumerate(read_frames(info, ANALYSIS_SHORT_SIDE, fps=ANALYSIS_FPS)):
            pending.append(ex.submit(_analyze, job))
            while len(pending) >= workers * 3 or (pending and pending[0].done()):
                records.append(pending.popleft().result())
                progress(len(records) / expected, f"Procurando boosters ({len(records)}/{expected} frames)")
        while pending:
            records.append(pending.popleft().result())
            progress(len(records) / expected, f"Procurando boosters ({len(records)}/{expected} frames)")
    names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM sets")}
    packs = []
    for n, frames in enumerate(segment(records), 1):
        best = max(frames, key=lambda f: f["inliers"] * np.sqrt(f["sharpness"] + 1))
        crop = f"crops/{n:02d}.jpg"
        (run_dir / crop).write_bytes(best["crop"].tobytes())
        code = frames[0]["set"]
        packs.append({"uid": f"{n:02d}", "set": code, "set_name": names.get(code, code), "name": "Booster Pack",
                      "t": round(frames[0]["i"] / ANALYSIS_FPS, 2), "inliers": max(f["inliers"] for f in frames),
                      "frames": len(frames), "quad": np.median([f["quad"] for f in frames], axis=0).round(4).tolist(),
                      "crop": crop})
    for i, p in enumerate(packs):
        p["t_end"] = packs[i + 1]["t"] if i + 1 < len(packs) else round(info.duration, 2)
    result = {
        "version": SCAN_VERSION, "kind": "lacrados",
        "video": os.path.relpath(video.resolve(), settings.root), "recorded_at": info.creation_time,
        "duration": round(info.duration, 2), "size": [info.width, info.height], "analysis_fps": ANALYSIS_FPS,
        "sets": sorted({p["set"] for p in packs}), "scanned_at": db.now(), "packs": packs, "cards": [],
    }
    (run_dir / "scan.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
    track = [{"t": round(r["i"] / ANALYSIS_FPS, 2), **({"set": r["set"], "inliers": r["inliers"]} if "set" in r else {})}
             for r in records]
    (run_dir / "track.json").write_text(json.dumps(track))
    return result


# --- correções na página --------------------------------------------------------------------


def _load(run_dir: Path) -> dict:
    return json.loads((run_dir / "scan.json").read_text())


def _save(run_dir: Path, scan: dict) -> None:
    packs = sorted(scan["packs"], key=lambda p: p["t"])
    for i, p in enumerate(packs):
        p["t_end"] = packs[i + 1]["t"] if i + 1 < len(packs) else scan.get("duration", p["t"] + 2)
    scan["packs"] = packs
    scan["sets"] = sorted({p["set"] for p in packs})
    (run_dir / "scan.json").write_text(json.dumps(scan, indent=2, ensure_ascii=False))


def remove_pack(run_dir: Path, uid: str) -> None:
    """Tira um booster identificado errado; ele fica em `removed` para poder voltar."""
    scan = _load(run_dir)
    pack = next((p for p in scan["packs"] if p["uid"] == uid), None)
    if pack is None:
        raise LookupError("Booster não encontrado nesta pipeline.")
    scan["packs"].remove(pack)
    scan.setdefault("removed", []).append({**pack, "removed_at": db.now()})
    _save(run_dir, scan)


def restore_pack(run_dir: Path, uid: str) -> None:
    scan = _load(run_dir)
    pack = next((p for p in scan.get("removed", []) if p["uid"] == uid), None)
    if pack is None:
        raise LookupError("Booster removido não encontrado nesta pipeline.")
    scan["removed"].remove(pack)
    pack.pop("removed_at", None)
    scan["packs"].append(pack)
    _save(run_dir, scan)


_UNSET = object()


def edit_pack(run_dir: Path, uid: str, code: str | None = None, name: str | None = None, value_usd=_UNSET) -> bool:
    """Corrige um booster: o set (o preço é refeito ao reprocessar) e/ou o valor. O valor editado fica fixo para
    esse booster (no vídeo, nos lacrados e no valor de hoje); `value_usd=None` volta ao preço de mercado.
    Devolve se algo mudou."""
    scan = _load(run_dir)
    pack = next((p for p in scan["packs"] if p["uid"] == uid), None)
    if pack is None:
        raise LookupError("Booster não encontrado nesta pipeline.")
    changed = False
    if code and pack["set"] != code:
        pack.update(set=code, set_name=name, manual=True)
        for key in ("price_usd", "price_key", "product_id", "image"):
            pack.pop(key, None)
        changed = True
    if value_usd is not _UNSET and pack.get("manual_usd") != value_usd:
        if value_usd is None:
            pack.pop("manual_usd", None)
        else:
            pack["manual_usd"] = round(float(value_usd), 4)
        changed = True
    if changed:
        _save(run_dir, scan)
    return changed


def add_pack(run_dir: Path, code: str, name: str, t: float) -> str:
    """Um booster que a identificação não pegou, no instante `t` do vídeo (a etiqueta vai no rodapé)."""
    scan = _load(run_dir)
    uid = f"m{int(round(t * 100))}"
    while any(p["uid"] == uid for p in scan["packs"] + scan.get("removed", [])):
        uid += "b"
    scan["packs"].append({"uid": uid, "set": code, "set_name": name, "name": "Booster Pack", "t": round(max(0.0, t), 2),
                          "inliers": None, "frames": 0, "quad": None, "crop": None, "manual": True})
    _save(run_dir, scan)
    return uid
