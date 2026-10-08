"""Catálogo do Pokémon TCG pelo TCGdex (https://tcgdex.dev), em inglês e português.

O TCGdex usa os mesmos ids de set e de carta em todos os idiomas: a carta do catálogo é a internacional (inglês),
com o nome e a imagem da impressão brasileira quando o set saiu em português. Os preços são os do TCGplayer que o
TCGdex traz em cada carta: normal (ou holo, nas raras que só saem holo) e reverse holo (o "foil" do Pokémon).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from . import lorcast

API = "https://api.tcgdex.net/v2"
PREFIX = "pkm-"
SKIP_SERIES = {"tcgp"}  # Pokémon TCG Pocket: só digital
LOOSE_SERIES = {"misc", "tk", "mc"}  # promos, kits de treinador, McDonald's: não saem em booster
NOT_BOOSTER = re.compile(r"promo|energ|trainer kit|my first battle|deck", re.I)
WORKERS = 8


def _api(path: str):
    return json.loads(lorcast.fetch(API + urllib.parse.quote(path), timeout=60))


def _maybe(path: str):
    try:
        return _api(path)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # o set (ou a carta) não existe nesse idioma
            return None
        raise


def fetch_sets(known: dict[str, dict] | None = None) -> list[dict]:
    """Os sets de papel, no formato da tabela `sets`. A data e a sigla vêm do detalhe de cada set: só os que ainda
    não estão em `known` (código → linha) são consultados."""
    known = known or {}
    pt_names = {s["id"]: s["name"] for s in _api("/pt/sets")}
    brief = []
    for serie in _api("/en/series"):
        if serie["id"] in SKIP_SERIES:
            continue
        for s in (_api(f"/en/series/{serie['id']}") or {}).get("sets", []):
            brief.append((serie["id"], s))

    def detail(item):
        serie, s = item
        row = known.get(PREFIX + s["id"])
        if row is not None and row["released_at"] and row["abbr"]:
            return {"releaseDate": row["released_at"], "abbreviation": {"official": row["abbr"]}}
        return _maybe(f"/en/sets/{s['id']}") or {}

    with ThreadPoolExecutor(WORKERS) as ex:
        details = list(ex.map(detail, brief))
    out = []
    for (serie, s), d in zip(brief, details):
        count = (s.get("cardCount") or {}).get("official") or 0
        out.append({
            "code": PREFIX + s["id"], "id": s["id"], "name": s["name"], "released_at": d.get("releaseDate"),
            "game": "pokemon", "name_pt": pt_names.get(s["id"]),
            "booster": serie not in LOOSE_SERIES and count >= 40 and not s["id"].endswith("p")
            and not NOT_BOOSTER.search(s["name"]),
            "abbr": (d.get("abbreviation") or {}).get("official"),
            "symbol": f"{s['symbol']}.png" if s.get("symbol") else None, "card_count": count or None,
        })
    return out


def _market(tcg: dict, key: str) -> float | None:
    v = tcg.get(key) or {}
    return v.get("marketPrice") if v.get("marketPrice") is not None else v.get("midPrice")


def to_row(card: dict, pt: dict | None = None) -> dict:
    """Uma carta do TCGdex (o detalhe em inglês e, se houver, a versão em português) no formato da tabela."""
    tcg = (card.get("pricing") or {}).get("tcgplayer") or {}
    normal, holo, reverse = _market(tcg, "normal"), _market(tcg, "holofoil"), _market(tcg, "reverse-holofoil")
    product = next((v["productId"] for v in tcg.values() if isinstance(v, dict) and v.get("productId")), None)
    image, image_pt = card.get("image"), (pt or {}).get("image")
    number = str(card["localId"])
    m = re.match(r"\d+", number)
    kind = " · ".join(x for x in (card.get("category"), card.get("stage"), card.get("trainerType"),
                                  card.get("energyType")) if x)
    return {
        "id": PREFIX + card["id"], "game": "pokemon", "set_code": PREFIX + card["set"]["id"], "number": number,
        "sort_number": int(m.group()) if m else None, "name": card["name"], "name_pt": (pt or {}).get("name"),
        "version": None, "rarity": card.get("rarity"), "ink": "/".join(card.get("types") or []) or None,
        "type": kind or None, "cost": None,
        "image_small": f"{image}/low.webp" if image else None, "image_normal": f"{image}/high.webp" if image else None,
        "image_large": f"{image}/high.png" if image else None, "image_pt": f"{image_pt}/high.webp" if image_pt else None,
        "tcgplayer_url": f"https://www.tcgplayer.com/product/{product}" if product else None,
        # rara que só sai holo: o holo é o preço normal dela; o "foil" do Pokémon é o reverse holo
        "usd": normal if normal is not None else holo,
        "usd_foil": reverse if reverse is not None else (holo if normal is not None else None),
        "raw": json.dumps({k: card.get(k) for k in ("hp", "stage", "dexId", "illustrator", "variants", "regulationMark")}),
    }


def fetch_set_cards(set_id: str, langs: tuple[str, ...] = ("en", "pt")) -> list[dict]:
    """As cartas do set (id do TCGdex, sem o prefixo): uma consulta por carta, em paralelo."""
    en = _api(f"/en/sets/{set_id}")
    pt = {}
    if "pt" in langs:
        pt = {c["id"]: c for c in (_maybe(f"/pt/sets/{set_id}") or {}).get("cards", [])}
    with ThreadPoolExecutor(WORKERS) as ex:
        cards = list(ex.map(lambda c: _maybe(f"/en/cards/{c['id']}"), en.get("cards", [])))
    return [to_row(c, pt.get(c["id"])) for c in cards if c]
