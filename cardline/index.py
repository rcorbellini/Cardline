"""Índice de features locais (SIFT) das imagens oficiais das cartas, um arquivo por set.

O matching de um frame contra esse índice identifica a carta *e* devolve a homografia
imagem-oficial → frame, que localiza a carta no vídeo (usada para recorte e overlay).
"""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .config import Settings

INDEX_VERSION = 4
# Os keypoints mais fortes de uma carta ficam no texto (mesma fonte em todas → ambíguos), então a
# arte ganha um orçamento próprio e vem primeiro: o índice "leve" (per_card) fica só com a arte.
ART_FEATURES = 450
REST_FEATURES = 450
ART_BOTTOM = 0.55  # fração da altura da carta ocupada pela arte
SAME_ART_BITS = 24  # distância de hamming (de 256) para considerar duas artes iguais


def image_path(settings: Settings, set_code: str, card_id: str) -> Path:
    return settings.images_dir / set_code / f"{card_id}.avif"


def index_path(settings: Settings, set_code: str) -> Path:
    return settings.index_dir / f"set-{set_code}.npz"


def load_rgb(path: Path) -> np.ndarray:
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def art_hash(gray: np.ndarray) -> np.ndarray:
    """dHash de 256 bits da região da arte, para achar reimpressões com a mesma ilustração."""
    h, w = gray.shape
    art = gray[int(0.07 * h) : int(0.52 * h), int(0.08 * w) : int(0.92 * w)]
    small = cv2.resize(art, (17, 16), interpolation=cv2.INTER_AREA).astype(np.int16)
    return np.packbits(small[:, 1:] > small[:, :-1])


def _features(path: Path) -> tuple[np.ndarray, np.ndarray, tuple[int, int], np.ndarray]:
    cv2.setNumThreads(1)
    gray = cv2.cvtColor(load_rgb(path), cv2.COLOR_RGB2GRAY)
    # sem nfeatures/máscara: o SIFT do OpenCV corta os N mais fortes *antes* de aplicar a máscara
    kps, desc = cv2.SIFT_create().detectAndCompute(gray, None)
    if desc is None:
        return np.zeros((0, 2), np.float32), np.zeros((0, 128), np.uint8), gray.shape[::-1], art_hash(gray)
    pts = np.float32([k.pt for k in kps])
    response = np.array([k.response for k in kps])
    in_art = pts[:, 1] < ART_BOTTOM * gray.shape[0]
    order = []
    for region, n in ((in_art, ART_FEATURES), (~in_art, REST_FEATURES)):
        idx = np.nonzero(region)[0]
        order.extend(idx[np.argsort(-response[idx])][:n])
    desc = np.clip(np.rint(desc[order]), 0, 255).astype(np.uint8)
    return pts[order], desc, gray.shape[::-1], art_hash(gray)


def build_set_index(settings: Settings, set_code: str, card_ids: list[str]) -> Path:
    paths = [image_path(settings, set_code, cid) for cid in card_ids]
    keep = [i for i, p in enumerate(paths) if p.exists()]
    with ProcessPoolExecutor(os.cpu_count(), multiprocessing.get_context("spawn")) as ex:
        feats = list(ex.map(_features, [paths[i] for i in keep], chunksize=8))
    out = index_path(settings, set_code)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")  # a sincronização pela página pode rodar junto com um scan
    with open(tmp, "wb") as fh:
        np.savez(
            fh,
            version=INDEX_VERSION,
            card_ids=np.array([card_ids[i] for i in keep]),
            counts=np.array([len(f[0]) for f in feats], np.int32),
            kp=np.concatenate([f[0] for f in feats]),
            desc=np.concatenate([f[1] for f in feats]),
            sizes=np.array([f[2] for f in feats], np.int32),
            hashes=np.stack([f[3] for f in feats]),
        )
    tmp.replace(out)
    return out


def index_is_fresh(settings: Settings, set_code: str, card_ids: list[str]) -> bool:
    path = index_path(settings, set_code)
    if not path.exists():
        return False
    with np.load(path) as z:
        if int(z["version"]) != INDEX_VERSION:
            return False
        indexed = set(z["card_ids"].tolist())
    available = {cid for cid in card_ids if image_path(settings, set_code, cid).exists()}
    return available <= indexed


def indexed_sets(settings: Settings) -> list[str]:
    return sorted(p.stem.removeprefix("set-") for p in settings.index_dir.glob("set-*.npz"))


@dataclass
class RefIndex:
    card_ids: list[str]
    card_sets: list[str]
    kp: np.ndarray  # (N, 2) posição do keypoint na imagem oficial
    desc: np.ndarray  # (N, 128) RootSIFT float32
    owner: np.ndarray  # (N,) índice da carta dona do keypoint
    offsets: np.ndarray  # (cartas + 1,) início dos keypoints de cada carta
    sizes: np.ndarray  # (cartas, 2) largura/altura da imagem oficial
    groups: np.ndarray  # (cartas,) cartas com a mesma arte compartilham o grupo


def root_sift(desc: np.ndarray) -> np.ndarray:
    d = desc.astype(np.float32)
    d /= d.sum(axis=1, keepdims=True) + 1e-7
    return np.sqrt(d)


def load_index(settings: Settings, set_codes: list[str], per_card: int | None = None) -> RefIndex:
    """Carrega e concatena os índices dos sets; `per_card` limita os keypoints (índice "leve")."""
    ids, sets, kps, descs, sizes, hashes, counts = [], [], [], [], [], [], []
    for code in set_codes:
        path = index_path(settings, code)
        if not path.exists():
            raise SystemExit(f"Índice do set {code} não existe; rode `cardline sync --sets {code}`.")
        with np.load(path) as z:  # cada z[...] relê o array do disco: ler uma vez só
            all_kp, all_desc = z["kp"], z["desc"]
            start = 0
            for cid, n, size, h in zip(z["card_ids"], z["counts"], z["sizes"], z["hashes"]):
                take = n if per_card is None else min(n, per_card)
                ids.append(str(cid))
                sets.append(code)
                kps.append(all_kp[start : start + take])
                descs.append(all_desc[start : start + take])
                sizes.append(size)
                hashes.append(h)
                counts.append(take)
                start += n
    counts_arr = np.array(counts, np.int64)
    return RefIndex(
        card_ids=ids,
        card_sets=sets,
        kp=np.concatenate(kps).astype(np.float32),
        desc=root_sift(np.concatenate(descs)),
        owner=np.repeat(np.arange(len(ids), dtype=np.int32), counts_arr),
        offsets=np.concatenate([[0], np.cumsum(counts_arr)]),
        sizes=np.array(sizes, np.int32),
        groups=_art_groups(np.stack(hashes)),
    )


def _art_groups(hashes: np.ndarray) -> np.ndarray:
    """Union-find das cartas cuja arte é praticamente idêntica (reimpressões entre sets)."""
    n = len(hashes)
    parent = np.arange(n)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n - 1):
        dist = np.bitwise_count(hashes[i + 1 :] ^ hashes[i]).sum(axis=1)
        for j in np.nonzero(dist <= SAME_ART_BITS)[0] + i + 1:
            parent[find(int(j))] = find(i)
    return np.array([find(i) for i in range(n)], np.int32)
