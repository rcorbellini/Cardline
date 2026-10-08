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
