"""Coleção: registra as cartas de um run (substituindo as anteriores dele), correções e cartas avulsas."""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from . import db, rarity
from .config import Settings
from .foil import FOIL_ONLY, assign_foils
from .money import money_for


def load_scan(run_dir: Path) -> dict:
    return json.loads((run_dir / "scan.json").read_text())


def save_scan(run_dir: Path, scan: dict) -> None:
    (run_dir / "scan.json").write_text(json.dumps(scan, indent=2, ensure_ascii=False))


def register(con: sqlite3.Connection, run_id: int, scan: dict) -> None:
    """Grava as cartas do scan como as cartas do run (substitui o que o run tinha registrado)."""
    with con:
        con.execute("DELETE FROM collection WHERE run_id = ?", (run_id,))
        con.executemany(
            "INSERT INTO collection(card_id, foil, run_id, pack, slot, video_time, price_usd, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (c["card_id"], int(c["foil"]), run_id, c["pack"], c["slot"], c["t"], c.get("price_usd"), scan["scanned_at"])
                for c in scan["cards"]
            ],
        )


def _fill(c: dict, row) -> None:
    c.update(
        card_id=row["id"], set=row["set_code"], number=row["number"], name=row["name"],
        version=row["version"], rarity=row["rarity"], ink=row["ink"],
    )


def card_uid(c: dict) -> str:
    """Identificador estável da carta no scan (a posição muda quando outra carta sai ou entra)."""
    if c.get("uid"):
        return c["uid"]
    if c.get("crop"):  # scans antigos: o recorte tem o número original da carta
        return Path(c["crop"]).stem
    return f"m{c['t']:g}-{c['card_id'][-6:]}"


AUTO_FOIL = ("booster", "posição")  # foil deduzida pelo sistema (não pela raridade nem à mão)


def _forget_price(c: dict) -> None:
    """Outro acabamento/carta: o preço da abertura é recalculado (pelo dia da abertura) ao reprocessar."""
    c.pop("price_usd", None)
    c.pop("price_key", None)


def _set_foil(scan: dict, c: dict, foil: bool) -> bool:
    """Marca ou desmarca a carta como foil à mão; devolve se algum acabamento mudou."""
    if c["rarity"] in FOIL_ONLY and not foil:
        raise ValueError(f"{rarity.label(c['rarity'])} é sempre foil.")
    changed = c["foil"] != foil
    c["foil"], c["foil_reason"] = foil, "manual"
    if changed:
        _forget_price(c)
    if foil and scan.get("kind", "abertura") == "abertura":  # um booster tem uma foil: a dedução em outra carta sai
        for other in scan["cards"]:
            if other is not c and other["pack"] == c["pack"] and other["foil"] and other.get("foil_reason") in AUTO_FOIL:
                other["foil"], other["foil_reason"] = False, None
                _forget_price(other)
                changed = True
    return changed


def set_card_foil(settings: Settings, run_dir: Path, uid: str, foil: bool) -> bool:
    """Edita o acabamento de uma carta da pipeline; devolve se mudou algo (aí é preciso reprocessar)."""
    scan = load_scan(run_dir)
    c = next((c for c in scan["cards"] if card_uid(c) == uid), None)
    if c is None:
        raise LookupError("Carta não encontrada nesta pipeline.")
    changed = _set_foil(scan, c, foil)
    _renumber(scan, settings.pack_size)
    save_scan(run_dir, scan)
    return changed


def remove_card(settings: Settings, run_dir: Path, uid: str) -> dict:
    """Tira a carta da identificação; ela fica em `removed` para poder ser restaurada."""
    scan = load_scan(run_dir)
    c = next((c for c in scan["cards"] if card_uid(c) == uid), None)
    if c is None:
        raise LookupError("Carta não encontrada nesta pipeline.")
    scan["cards"].remove(c)
    c.update(uid=uid, removed_at=db.now())
    scan.setdefault("removed", []).append(c)
    _renumber(scan, settings.pack_size)
    save_scan(run_dir, scan)
    return c


def restore_card(settings: Settings, run_dir: Path, uid: str) -> dict:
    scan = load_scan(run_dir)
    c = next((c for c in scan.get("removed", []) if card_uid(c) == uid), None)
    if c is None:
        raise LookupError("Carta removida não encontrada nesta pipeline.")
    scan["removed"].remove(c)
    c.pop("removed_at", None)
    scan["cards"].append(c)
    _renumber(scan, settings.pack_size)
    save_scan(run_dir, scan)
    return c


def _renumber(scan: dict, pack_size: int) -> None:
    """Reordena pelo vídeo e refaz boosters e foil (a composição de cada booster pode ter mudado)."""
    cards = scan["cards"]
    cards.sort(key=lambda c: c["t"])
    booster = scan.get("kind", "abertura") == "abertura"
    for i, c in enumerate(cards):
        c["pack"], c["slot"] = (i // pack_size + 1, i % pack_size + 1) if booster else (None, i + 1)
        c["t_end"] = cards[i + 1]["t"] if i + 1 < len(cards) else scan["duration"]
    if not booster:  # cadastro: sem booster, a foil é só por raridade ou à mão
        return
    manual = {c["pack"] for c in cards if c.get("foil_reason") == "manual"}  # escolha manual vale para o booster
    assign_foils([c for c in cards if c["pack"] not in manual], pack_size)


def edit_scan(
    settings: Settings,
    run_dir: Path,
    index: int | None,
    card_ref: str | None = None,
    foil: bool | None = None,
    remove: bool = False,
    add_at: float | None = None,
) -> str:
    """Corrige o resultado da identificação (carta errada, foil, carta a mais ou faltando).

    Só muda o `scan.json`: os passos seguintes (preço, coleção, vídeo) ficam desatualizados até o
    run ser executado de novo a partir dali.
    """
    scan = load_scan(run_dir)
    cards = scan["cards"]
    con = db.connect(settings.db_path)
    if add_at is not None:
        if not card_ref:
            raise SystemExit("--at precisa de --card com a carta a inserir.")
        c = {"uid": uuid.uuid4().hex[:8], "t": add_at, "foil": bool(foil), "foil_reason": "manual" if foil else None,
             "quad": None, "crop": None, "inliers": None, "frames": 0, "alternatives": [], "manual": True}
        _fill(c, db.resolve_card(con, card_ref))
        cards.append(c)
        action = "Adicionada"
    else:
        if index is None or not 1 <= index <= len(cards):
            raise SystemExit(f"Informe o número da carta (1 a {len(cards)}), como aparece na página.")
        c = cards[index - 1]
        if remove:
            c = remove_card(settings, run_dir, card_uid(c))
            return f"Removida: {db.display_name(c)} (dá para restaurar na página)"
        else:
            if card_ref:
                _fill(c, db.resolve_card(con, card_ref))
                c["manual"] = True
                c.pop("check", None)  # a conferência era sobre a carta antiga
                _forget_price(c)
            if foil is not None:
                try:
                    _set_foil(scan, c, foil)
                except ValueError as e:
                    raise SystemExit(str(e)) from e
            action = "Corrigida"
    if c["rarity"] in FOIL_ONLY:
        c["foil"] = True
    _renumber(scan, settings.pack_size)
    save_scan(run_dir, scan)
    where = f" como #{cards.index(c) + 1} (a numeração das seguintes mudou)" if action == "Adicionada" else ""
    return f"{action}{where}: {db.display_name(c)}{' (foil)' if c['foil'] else ''}"


def add(settings: Settings, card_ref: str, foil: bool, qty: int) -> None:
    """Carta obtida fora de vídeo (troca, compra avulsa...)."""
    con = db.connect(settings.db_path)
    row = db.resolve_card(con, card_ref)
    foil = foil or row["rarity"] in FOIL_ONLY
    price = db.price_usd(row, foil)
    with con:
        con.executemany(
            "INSERT INTO collection(card_id, foil, price_usd, added_at) VALUES (?, ?, ?, ?)",
            [(row["id"], int(foil), price, db.now())] * qty,
        )
    print(f"Adicionada: {qty}× {db.display_name(row)} ({row['set_code']}/{row['number']}){' foil' if foil else ''}"
          f" — {money_for(settings).fmt(price)} cada")
