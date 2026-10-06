"""Sincroniza catálogo e preços do Lorcast, baixa as imagens oficiais e monta os índices."""

from __future__ import annotations

import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from . import db, lorcast
from .config import Settings
from .index import build_set_index, image_path, index_is_fresh

# Ícones dos sets: Lorcana não tem símbolo de expansão nas cartas (o símbolo do rodapé é a raridade),
# então o ícone de cada set é a foto oficial do booster no TCGplayer, via tcgcsv.com (espelho público).
TCGCSV = "https://tcgcsv.com/tcgplayer/71"  # 71 = Disney Lorcana
ICON_MAX = 600


def is_booster_set(code: str) -> bool:
    """Sets principais (códigos numéricos) são os que saem em booster."""
    return code.isdigit()


def sync(settings: Settings, set_codes: list[str] | None = None, images: bool = True) -> None:
    con = db.connect(settings.db_path)
    fetched_at = db.now()
    sets = lorcast.fetch_sets()
    print(f"Catálogo: {len(sets)} sets no Lorcast")
    for s in sets:
        cards = lorcast.fetch_set_cards(s["id"])
        db.upsert_set(con, s)
        db.upsert_cards(con, cards, fetched_at)
        con.commit()
        print(f"  {s['code']:>8}  {s['name']:<42} {len(cards):>4} cartas")

    if not images:
        return
    known = {s["code"] for s in sets}
    targets = set_codes or [s["code"] for s in sets if is_booster_set(s["code"])]
    for code in targets:
        if code not in known:
            raise SystemExit(f"Set {code!r} não existe no Lorcast.")
        rows = db.cards_in_set(con, code)
        missing = [r for r in rows if r["image_normal"] and not image_path(settings, code, r["id"]).exists()]
        if missing:
            print(f"Set {code}: baixando {len(missing)} imagens...", flush=True)
            with ThreadPoolExecutor(max_workers=6) as ex:
                list(ex.map(lambda r, code=code: lorcast.download(r["image_normal"], image_path(settings, code, r["id"])),
                            missing))
        ids = [r["id"] for r in rows]
        if not index_is_fresh(settings, code, ids):
            print(f"Set {code}: indexando features...", flush=True)
            build_set_index(settings, code, ids)
    try:
        synced = sync_set_icons(settings, con)
        print(f"Ícones de set: {synced} novos" if synced else "Ícones de set: em dia")
    except (OSError, ValueError, KeyError) as e:  # tcgcsv fora do ar não invalida o catálogo já sincronizado
        print(f"Ícones de set: não consegui buscar agora ({e}); ficam os que já existem")
    print("Sync concluído.")


def icon_path(settings: Settings, code: str) -> Path:
    return settings.cache_dir / "sets" / f"{re.sub(r'[^A-Za-z0-9_-]', '_', code)}.png"


def booster_pack(products: list[dict]) -> dict | None:
    """O booster avulso do set: não o "sleeved", a caixa, o display ou os kits."""
    for p in products:
        name = p.get("name", "")
        if name.endswith("Booster Pack") and not re.search(r"Sleeved|Bundle|Box|Case|Display", name):
            return p
    return None


def cutout(data: bytes) -> Image.Image:
    """Foto de produto em fundo branco → PNG com fundo transparente, recortado no produto.

    Só sai o branco ligado à borda da foto: partes brancas dentro da arte do booster ficam.
    """
    rgb = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
    white = (rgb.min(axis=2) > 232).astype(np.uint8)
    _, labels = cv2.connectedComponents(white, connectivity=4)
    edge = np.concatenate([labels[0], labels[-1], labels[:, 0], labels[:, -1]])
    background = np.isin(labels, [label for label in np.unique(edge) if label != 0])
    alpha = cv2.GaussianBlur(np.where(background, 0, 255).astype(np.uint8), (3, 3), 0)  # borda suave
    img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    img = img.crop(img.getbbox())
    img.thumbnail((ICON_MAX, ICON_MAX), Image.LANCZOS)
    return img


def _store_icon(settings: Settings, con, code: str, img: Image.Image, source: str) -> None:
    dest = icon_path(settings, code)
    dest.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest)
    with con:
        con.execute("UPDATE sets SET icon = ?, icon_source = ? WHERE code = ?",
                    (os.path.relpath(dest, settings.root), source, code))


def sync_set_icons(settings: Settings, con, only: list[str] | None = None, force: bool = False) -> int:
    """Baixa a foto do booster de cada set que ainda não tem ícone; ícone enviado à mão não é trocado."""
    groups = json.loads(lorcast.fetch(f"{TCGCSV}/groups"))["results"]
    by_code = {g["abbreviation"].lower(): g for g in groups if g.get("abbreviation")}
    done = 0
    for s in con.execute("SELECT * FROM sets").fetchall():
        group = by_code.get(s["code"].lower())
        if (only and s["code"] not in only) or group is None:
            continue
        with con:
            con.execute("UPDATE sets SET tcg_group_id = ? WHERE code = ?", (group["groupId"], s["code"]))
        has_icon = s["icon"] and (settings.root / s["icon"]).exists()
        if has_icon and (s["icon_source"] == "manual" or not force):
            continue  # ícone enviado à mão só sai pelo "usar a foto do booster" (ou se o arquivo sumir)
        try:
            pack = booster_pack(json.loads(lorcast.fetch(f"{TCGCSV}/{group['groupId']}/products"))["results"])
            if not pack or not pack.get("imageUrl"):
                continue  # set sem booster avulso (promos, quests): a página usa o selo com o número
            image = lorcast.fetch(pack["imageUrl"].replace("_200w.", "_in_1000x1000."))
            _store_icon(settings, con, s["code"], cutout(image), "tcgplayer")
        except (OSError, ValueError, KeyError) as e:
            if only:
                raise
            print(f"  ícone do set {s['code']}: falhou ({e})", flush=True)  # os outros sets seguem
            continue
        print(f"  ícone do set {s['code']}: {pack['name']}", flush=True)
        done += 1
    return done


def save_manual_icon(settings: Settings, code: str, data: bytes) -> None:
    """Ícone escolhido pelo usuário (ex.: o logo do set); a sincronização não o substitui."""
    con = db.connect(settings.db_path)
    if con.execute("SELECT 1 FROM sets WHERE code = ?", (code,)).fetchone() is None:
        raise LookupError(f"Set {code} não existe.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:
        raise ValueError("O arquivo não é uma imagem válida.") from e
    img = img.convert("RGBA")
    img.thumbnail((ICON_MAX, ICON_MAX), Image.LANCZOS)
    _store_icon(settings, con, code, img, "manual")


def reset_icon(settings: Settings, code: str) -> None:
    """Volta ao ícone automático (a foto do booster), se o set tiver um."""
    con = db.connect(settings.db_path)
    icon_path(settings, code).unlink(missing_ok=True)
    with con:
        con.execute("UPDATE sets SET icon = NULL, icon_source = NULL WHERE code = ?", (code,))
    sync_set_icons(settings, con, only=[code], force=True)


def refresh_prices(settings: Settings, set_codes: list[str]) -> dict:
    """Atualiza os preços de hoje dos sets; o preço de cada carta na abertura fica como estava."""
    con = db.connect(settings.db_path)
    fetched_at = db.now()
    done = []
    for code in set_codes:
        row = con.execute("SELECT id FROM sets WHERE code = ?", (code,)).fetchone()
        if row is None:
            continue
        cards = lorcast.fetch_set_cards(row["id"])
        with con:
            db.upsert_cards(con, cards, fetched_at)
        done.append(code)
    return {"updated_at": fetched_at, "sets": done}
