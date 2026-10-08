"""Produtos lacrados da coleção (boosters, caixas, decks, baús...), cotados pelo TCGplayer via tcgcsv.com.

Cada set do Lorcast tem o grupo dele no TCGplayer (sets.tcg_group_id, preenchido na sincronização dos ícones).
Os produtos do grupo sem dados de carta (número, raridade) são os lacrados; o preço é o de mercado do dia.
"""

from __future__ import annotations

import json
import re
import time

from . import db, lorcast
from .catalog import TCGCSV
from .config import Settings
from .money import to_usd

PRODUCTS_TTL = 12 * 3600  # a lista de produtos de um set quase não muda


def _cache(settings: Settings, group: int, what: str):
    return settings.cache_dir / "tcgcsv" / f"{group}-{what}.json"


def _fetch(settings: Settings, group: int, what: str, ttl: float = 0) -> list[dict]:
    path = _cache(settings, group, what)
    if ttl and path.exists() and time.time() - path.stat().st_mtime < ttl:
        return json.loads(path.read_text())
    results = json.loads(lorcast.fetch(f"{TCGCSV}/{group}/{what}"))["results"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results))
    return results


def short_name(name: str, set_name: str | None) -> str:
    """"Disney Lorcana: The First Chapter Booster Box" → "Booster Box"."""
    name = re.sub(r"^Disney Lorcana:?\s*", "", name)
    if set_name and name.lower().startswith(set_name.lower()):
        name = name[len(set_name):].strip(" -:") or name
    return name


def group_of(con, set_code: str) -> tuple[int, str]:
    row = con.execute("SELECT tcg_group_id, name FROM sets WHERE code = ?", (set_code,)).fetchone()
    if row is None or row["tcg_group_id"] is None:
        raise LookupError("Este set não tem produtos no TCGplayer (ou os sets ainda não foram sincronizados).")
    return row["tcg_group_id"], row["name"]


def products(settings: Settings, con, set_code: str) -> list[dict]:
    """Os lacrados do set à venda no TCGplayer, com o preço de mercado de hoje (o booster avulso primeiro)."""
    group, set_name = group_of(con, set_code)
    prices = {p["productId"]: p.get("marketPrice") for p in _fetch(settings, group, "prices")
              if p.get("subTypeName") in (None, "Normal")}
    out = [{"product_id": p["productId"], "name": short_name(p["name"], set_name), "full_name": p["name"],
            "image": p.get("imageUrl"), "usd": prices.get(p["productId"])}
           for p in _fetch(settings, group, "products", PRODUCTS_TTL) if not p.get("extendedData")]
    order = ("Booster Pack", "Booster Box", "Sleeved Booster", "Starter Deck", "Gift Set", "Illumineer's Trove")
    rank = lambda p: next((i for i, k in enumerate(order) if p["name"].startswith(k)), len(order))  # noqa: E731
    return sorted(out, key=lambda p: (rank(p), p["name"]))


def items(con) -> list[dict]:
    """Os lacrados em estoque; `qty` é quanto ainda está fechado (o registrado menos os abertos)."""
    return [{**dict(r), "qty": r["qty"] - r["opened"]} for r in con.execute(
        "SELECT * FROM sealed WHERE qty > opened ORDER BY COALESCE(usd, 0) * (qty - opened) DESC, name")]


def boosters(con) -> list[dict]:
    """Os boosters em estoque, para escolher numa abertura (puxa o set e o valor de registro)."""
    return [x for x in items(con) if re.match(r"(Sleeved )?Booster Pack$", x["name"])]


def take(con, item_id: int, n: int) -> dict:
    """Abre `n` do lacrado (uma pipeline de abertura): saem do estoque, e voltam se a pipeline for excluída."""
    with con:
        if not con.execute("UPDATE sealed SET opened = opened + ? WHERE id = ? AND qty - opened >= ?",
                           (n, item_id, n)).rowcount:
            raise LookupError(f"Não há {n} desse booster fechado nos lacrados.")
    db.record_value(con)
    return dict(con.execute("SELECT * FROM sealed WHERE id = ?", (item_id,)).fetchone())


def give_back(con, item_id: int, n: int) -> None:
    with con:
        con.execute("UPDATE sealed SET opened = MAX(0, opened - ?) WHERE id = ?", (n, item_id))
    db.record_value(con)


def add(settings: Settings, con, set_code: str, product_id: int, qty: int, paid: float | None, currency: str) -> int:
    product = next((p for p in products(settings, con, set_code) if p["product_id"] == product_id), None)
    if product is None:
        raise LookupError("Produto não encontrado entre os lacrados do set.")
    with con:
        new_id = con.execute(
            "INSERT INTO sealed(product_id, set_code, name, image, qty, paid, paid_currency, paid_usd, usd, price_updated_at,"
            " added_at, registered_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, set_code, product["name"], product["image"], qty, paid, currency.upper() if paid else None,
             to_usd(settings, paid, currency), product["usd"], db.now(), db.now(), product["usd"])).lastrowid
    db.record_value(con)
    return new_id


def update(settings: Settings, con, item_id: int, qty: int | None, paid: float | None, currency: str | None,
           paid_sent: bool) -> None:
    row = con.execute("SELECT * FROM sealed WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise LookupError("Lacrado não encontrado.")
    with con:
        if qty is not None:  # a quantidade da página é a que está fechada: os abertos continuam contados
            con.execute("UPDATE sealed SET qty = ? + opened WHERE id = ?", (qty, item_id))
        if paid_sent:
            cur = (currency or row["paid_currency"] or "BRL").upper()
            con.execute("UPDATE sealed SET paid = ?, paid_currency = ?, paid_usd = ? WHERE id = ?",
                        (paid, cur if paid else None, to_usd(settings, paid, cur), item_id))
    db.record_value(con)


def remove(con, item_id: int) -> None:
    with con:
        if not con.execute("DELETE FROM sealed WHERE id = ?", (item_id,)).rowcount:
            raise LookupError("Lacrado não encontrado.")
    db.record_value(con)


def refresh_prices(settings: Settings, con, progress=lambda fraction, message=None: None) -> int:
    """Preço de mercado de hoje dos lacrados da coleção: uma consulta ao tcgcsv por set."""
    sets = [r[0] for r in con.execute("SELECT DISTINCT set_code FROM sealed WHERE product_id IS NOT NULL")]
    n = 0
    for i, code in enumerate(sets):
        group, set_name = group_of(con, code)
        progress(i / max(1, len(sets)), f"Lacrados {i + 1} de {len(sets)}: {set_name}")
        prices = {p["productId"]: p.get("marketPrice") for p in _fetch(settings, group, "prices")
                  if p.get("subTypeName") in (None, "Normal")}
        with con:
            for r in con.execute("SELECT id, product_id FROM sealed WHERE set_code = ?", (code,)).fetchall():
                if prices.get(r["product_id"]) is not None:
                    con.execute("UPDATE sealed SET usd = ?, price_updated_at = ? WHERE id = ?",
                                (prices[r["product_id"]], db.now(), r["id"]))
                    n += 1
    db.record_value(con)
    return n


# --- pipeline de lacrados: os boosters do vídeo ---------------------------------------------


def booster_product(settings: Settings, con, set_code: str) -> dict | None:
    """O booster avulso do set no TCGplayer (todas as artes têm o mesmo produto e preço)."""
    return next((p for p in products(settings, con, set_code) if p["name"] == "Booster Pack"), None)


def price_packs(settings: Settings, con, scan: dict, progress=lambda fraction, message=None: None) -> tuple[int, list[str]]:
    """Preço de mercado de cada booster na hora do registro. Fica como estava ao reprocessar (o booster que
    trocou de set recebe o preço de agora). Devolve (quantos mantiveram o preço, sets sem preço agora)."""
    found, missing = {}, []
    names = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM sets")}
    for code in sorted({p["set"] for p in scan["packs"]}):
        progress(None, f"Buscando o preço do booster de {names.get(code, code)}")
        try:
            found[code] = booster_product(settings, con, code)
        except (OSError, ValueError, KeyError, LookupError):
            found[code] = None
        if found[code] is None:
            missing.append(code)
    kept = 0
    for p in scan["packs"]:
        product = found.get(p["set"])
        if p.get("price_usd") is not None and p.get("price_key") == p["set"]:
            kept += 1
        elif product:
            p.update(price_usd=product["usd"], price_key=p["set"])
        if product:
            p.update(product_id=product["product_id"], image=product["image"])
    return kept, missing


def register_packs(settings: Settings, con, run_id: int, scan: dict, paid: float | None, currency: str | None) -> int:
    """Os boosters do vídeo viram lacrados da coleção, um item por set (substitui o que esta pipeline tinha
    registrado; os já abertos numa abertura continuam abertos). O valor pago é dividido igualmente entre eles."""
    groups: dict[str, list[dict]] = {}
    for p in scan["packs"]:
        groups.setdefault(p["set"], []).append(p)
    each = round(paid / len(scan["packs"]), 2) if paid and scan["packs"] else None
    each_usd = to_usd(settings, each, currency or "BRL") if each else None
    now = db.now()
    with con:
        existing = {r["set_code"]: r for r in con.execute("SELECT * FROM sealed WHERE run_id = ?", (run_id,))}
        for code, items in groups.items():
            first = items[0]
            prices = [i["price_usd"] for i in items if i.get("price_usd") is not None]
            registered = round(sum(prices) / len(prices), 4) if prices else None
            values = (first.get("product_id"), first.get("image"), len(items), each, currency if each else None, each_usd,
                      registered)
            if code in existing:
                con.execute("UPDATE sealed SET product_id = ?, image = ?, qty = MAX(?, opened), paid = ?, paid_currency = ?,"
                            " paid_usd = ?, registered_usd = ?, usd = COALESCE(usd, ?), name = 'Booster Pack' WHERE id = ?",
                            (*values, registered, existing[code]["id"]))
            else:
                con.execute("INSERT INTO sealed(product_id, image, qty, paid, paid_currency, paid_usd, registered_usd, usd,"
                            " set_code, name, price_updated_at, added_at, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (*values, registered, code, "Booster Pack", now, now, run_id))
        for code, row in existing.items():
            if code not in groups:  # saiu da identificação: o que já foi aberto fica registrado como aberto
                if row["opened"]:
                    con.execute("UPDATE sealed SET qty = opened WHERE id = ?", (row["id"],))
                else:
                    con.execute("DELETE FROM sealed WHERE id = ?", (row["id"],))
    db.record_value(con)
    return sum(len(v) for v in groups.values())

