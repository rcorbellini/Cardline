"""Sincroniza catálogo e preços do Lorcast, baixa as imagens oficiais e monta os índices."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import db, lorcast
from .config import Settings
from .index import build_set_index, image_path, index_is_fresh


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
    print("Sync concluído.")


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
