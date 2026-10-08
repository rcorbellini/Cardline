"""Catálogo de Magic: The Gathering pelo Scryfall (https://scryfall.com/docs/api).

Sets, cartas, imagens e preços de mercado do TCGplayer (`prices.usd` / `usd_foil`). A carta do catálogo é a
impressão em inglês; a impressão em português do mesmo set (o mesmo número de colecionador) entra como o nome e a
imagem em português da mesma carta, para o reconhecimento comparar com as duas. A Wizards imprimiu Magic em
português até Modern Horizons 3 (2024); sets depois disso só têm a versão em inglês.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse

from . import lorcast

API = "https://api.scryfall.com"
HEADERS = {"Accept": "application/json"}  # o Scryfall pede User-Agent e Accept em toda chamada
_INTERVAL = 0.1  # a documentação pede 50–100 ms entre chamadas
_last = 0.0
PREFIX = "mtg-"
# tipos de set que saem em booster (os outros: commander, promos, tokens, digitais...)
BOOSTER_TYPES = {"core", "expansion", "masters", "draft_innovation", "funny"}


def _api(url: str) -> dict:
    global _last
    wait = _INTERVAL - (time.monotonic() - _last)
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    return json.loads(lorcast.fetch(url if url.startswith("http") else API + url, timeout=60, headers=HEADERS))


def fetch_sets() -> list[dict]:
    """Os sets de papel (sem os só digitais), no formato da tabela `sets`."""
    out = []
    for s in _api("/sets")["data"]:
        if s.get("digital"):
            continue
        out.append({
            "code": PREFIX + s["code"], "id": s["id"], "name": s["name"], "released_at": s.get("released_at"),
            "game": "magic", "booster": s.get("set_type") in BOOSTER_TYPES and not s.get("parent_set_code"),
            "abbr": s["code"].upper(), "symbol": s.get("icon_svg_uri"), "card_count": s.get("card_count"),
        })
    return out


def _search(query: str) -> list[dict]:
    """Todas as páginas de uma busca (175 cartas por página); nenhuma carta = lista vazia."""
    url = f"/cards/search?{urllib.parse.urlencode({'q': query, 'unique': 'prints', 'order': 'set'})}"
    cards: list[dict] = []
    while url:
        try:
            page = _api(url)
        except urllib.error.HTTPError as e:
            if e.code == 404:  # o Scryfall responde 404 quando a busca não acha nada
                return cards
            raise
        cards += page.get("data", [])
        url = page.get("next_page") if page.get("has_more") else None
    return cards


def _num(value) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _images(c: dict) -> dict:
    """As imagens da carta; nas de duas faces, as da frente (é a face que aparece ao abrir o booster)."""
    return c.get("image_uris") or ((c.get("card_faces") or [{}])[0].get("image_uris")) or {}


def to_row(c: dict) -> dict:
    """Uma carta do Scryfall no formato da tabela `cards`."""
    face = (c.get("card_faces") or [{}])[0]
    colors = c.get("colors") if c.get("colors") is not None else face.get("colors") or []
    prices = c.get("prices") or {}
    images = _images(c)
    number = str(c["collector_number"])
    m = re.match(r"\d+", number)
    return {
        "id": PREFIX + c["id"], "game": "magic", "set_code": PREFIX + c["set"], "number": number,
        "sort_number": int(m.group()) if m else None, "name": c["name"], "name_pt": None, "version": None,
        "rarity": c.get("rarity"),
        "ink": "/".join(colors) or "C", "type": c.get("type_line") or face.get("type_line"),
        "cost": int(c["cmc"]) if c.get("cmc") is not None else None,
        "image_small": images.get("small"), "image_normal": images.get("normal"), "image_large": images.get("large"),
        "image_pt": None, "tcgplayer_url": (c.get("purchase_uris") or {}).get("tcgplayer"),
        "usd": _num(prices.get("usd")), "usd_foil": _num(prices.get("usd_foil")) or _num(prices.get("usd_etched")),
        "raw": json.dumps({k: c.get(k) for k in ("oracle_id", "lang", "frame", "border_color", "finishes",
                                                 "promo_types", "tcgplayer_id")}),
    }


def fetch_set_cards(code: str, langs: tuple[str, ...] = ("en", "pt")) -> list[dict]:
    """As cartas do set (código do Scryfall, sem o prefixo), com nome e imagem em português quando houver."""
    rows = [to_row(c) for c in _search(f"e:{code} lang:en")]
    if "pt" in langs and rows:
        by_number = {r["number"]: r for r in rows}
        for c in _search(f"e:{code} lang:pt"):
            row = by_number.get(str(c["collector_number"]))
            if row is None:
                continue
            row["name_pt"] = c.get("printed_name") or ((c.get("card_faces") or [{}])[0].get("printed_name"))
            if c.get("image_status") not in (None, "missing", "placeholder"):
                row["image_pt"] = _images(c).get("normal")
    return rows
