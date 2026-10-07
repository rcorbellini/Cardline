"""Banco SQLite: catálogo de cartas, histórico de preços, pipelines (runs) e coleção."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    code         TEXT PRIMARY KEY,
    id           TEXT NOT NULL,
    name         TEXT NOT NULL,
    released_at  TEXT,
    icon         TEXT,     -- imagem do set (relativa à raiz do projeto)
    icon_source  TEXT,     -- tcgplayer (foto do booster) | manual (enviada na página)
    tcg_group_id INTEGER   -- grupo do set no TCGplayer
);

CREATE TABLE IF NOT EXISTS cards (
    id                TEXT PRIMARY KEY,
    set_code          TEXT NOT NULL,
    number            TEXT NOT NULL,
    sort_number       INTEGER,
    name              TEXT NOT NULL,
    version           TEXT,
    rarity            TEXT,
    ink               TEXT,
    type              TEXT,
    cost              INTEGER,
    image_small       TEXT,
    image_normal      TEXT,
    image_large       TEXT,
    tcgplayer_url     TEXT,
    usd               REAL,
    usd_foil          REAL,
    prices_updated_at TEXT,
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS cards_set_number ON cards(set_code, number);

CREATE TABLE IF NOT EXISTS price_history (
    card_id  TEXT NOT NULL,
    day      TEXT NOT NULL,
    usd      REAL,
    usd_foil REAL,
    PRIMARY KEY (card_id, day)
);

-- uma execução de pipeline para um vídeo
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL DEFAULT 'abertura',  -- abertura (booster, com vídeo e overlay) | cadastro (só coleção)
    video         TEXT NOT NULL,              -- relativo à raiz do projeto
    video_name    TEXT NOT NULL,              -- nome original do arquivo
    video_sha1    TEXT NOT NULL UNIQUE,       -- impede registrar o mesmo vídeo duas vezes
    dir           TEXT NOT NULL,              -- runs/<id>: scan.json, crops/, overlay.mp4, pipeline.log
    status        TEXT NOT NULL,              -- queued | running | done | failed | interrupted | stale
    step          TEXT,                       -- passo em execução (ou o que falhou)
    progress      REAL,                       -- 0..1 do passo em execução
    message       TEXT,
    error         TEXT,
    resume_from   TEXT,                       -- passo de onde a próxima execução começa
    pid           INTEGER,
    paid          REAL,                       -- valor de compra informado
    paid_currency TEXT,
    paid_usd      REAL,                       -- convertido pela cotação do dia da abertura
    set_hint      TEXT,
    options       TEXT NOT NULL DEFAULT '{}', -- {"overlay": bool, "verify": bool, "currency": "USD"|"BRL", "narration": bool}
    recorded_at   TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS run_steps (
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL,                -- pending | running | done | skipped | failed | stale
    message     TEXT,
    started_at  TEXT,
    finished_at TEXT,
    PRIMARY KEY (run_id, name)
);

CREATE TABLE IF NOT EXISTS collection (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id    TEXT NOT NULL REFERENCES cards(id),
    foil       INTEGER NOT NULL DEFAULT 0,
    run_id     INTEGER REFERENCES runs(id) ON DELETE CASCADE,  -- NULL = adicionada à mão
    pack       INTEGER,
    slot       INTEGER,
    video_time REAL,
    price_usd  REAL,                          -- preço no momento da abertura
    added_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS collection_run ON collection(run_id);

CREATE TABLE IF NOT EXISTS posts (  -- o vídeo de uma abertura numa rede
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    network      TEXT NOT NULL,   -- youtube | instagram | tiktok
    post_id      TEXT,            -- ID na rede (vídeo do YouTube, mídia do Instagram, vídeo do TikTok)
    url          TEXT NOT NULL,
    title        TEXT,
    via          TEXT NOT NULL,   -- api (postado pelo cardline) | link (postado pelo app e vinculado)
    variant      TEXT,            -- narrado | overlay: a versão que foi postada
    privacy      TEXT,            -- public | unlisted | private, quando a rede informa
    posted_at    TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (run_id, network)
);

CREATE TABLE IF NOT EXISTS post_stats (  -- uma linha por leitura: dá para ver a evolução depois
    run_id     INTEGER NOT NULL,
    network    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    views      INTEGER,
    likes      INTEGER,
    comments   INTEGER,
    shares     INTEGER,
    saves      INTEGER,
    manual     INTEGER NOT NULL DEFAULT 0,  -- números informados à mão (rede sem API, como o TikTok)
    PRIMARY KEY (run_id, network, fetched_at),
    FOREIGN KEY (run_id, network) REFERENCES posts(run_id, network) ON DELETE CASCADE
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=15)  # o servidor e o processo do pipeline usam o banco juntos
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        raise SystemExit(f"{path} é de uma versão mais nova do cardline (esquema {version}).")
    con.executescript(SCHEMA)  # banco novo já nasce no esquema atual
    if 0 < version < SCHEMA_VERSION:
        for v in range(version + 1, SCHEMA_VERSION + 1):
            MIGRATIONS[v](con)
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    con.commit()  # migração com INSERT abre transação; sem isso o banco fica travado para os outros processos
    return con


def _add_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:  # outro processo pode ter migrado
        con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _set_icon_columns(con: sqlite3.Connection) -> None:
    for column, ddl in (("icon", "TEXT"), ("icon_source", "TEXT"), ("tcg_group_id", "INTEGER")):
        _add_column(con, "sets", column, ddl)


def _narrate_steps(con: sqlite3.Connection) -> None:
    """Aberturas de antes da narração: o passo novo aparece como pulado (dá para ligar na página)."""
    con.execute("INSERT OR IGNORE INTO run_steps(run_id, name, status, message)"
                " SELECT id, 'narrate', 'skipped', 'desligada' FROM runs WHERE kind = 'abertura'")


def _posts_per_network(con: sqlite3.Connection) -> None:
    """Os vídeos do YouTube passam para a tabela de posts por rede (YouTube, Instagram, TikTok)."""
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "youtube_posts" in tables:
        con.execute("INSERT OR IGNORE INTO posts(run_id, network, post_id, url, title, via, variant, privacy, posted_at,"
                    " published_at) SELECT run_id, 'youtube', video_id, 'https://youtu.be/' || video_id, title, via,"
                    " variant, privacy, posted_at, published_at FROM youtube_posts")
    if "youtube_stats" in tables:
        con.execute("INSERT OR IGNORE INTO post_stats(run_id, network, fetched_at, views, likes, comments)"
                    " SELECT run_id, 'youtube', fetched_at, views, likes, comments FROM youtube_stats"
                    " WHERE run_id IN (SELECT run_id FROM posts WHERE network = 'youtube')")
    con.execute("DROP TABLE IF EXISTS youtube_stats")
    con.execute("DROP TABLE IF EXISTS youtube_posts")


MIGRATIONS = {
    3: lambda con: _add_column(con, "runs", "kind", "TEXT NOT NULL DEFAULT 'abertura'"),
    4: _set_icon_columns,
    5: _narrate_steps,
    6: _posts_per_network,
}


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def upsert_set(con: sqlite3.Connection, s: dict) -> None:
    con.execute(
        "INSERT INTO sets(code, id, name, released_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET id=excluded.id, name=excluded.name, released_at=excluded.released_at",
        (s["code"], s["id"], s["name"], s.get("released_at")),
    )


def upsert_cards(con: sqlite3.Connection, cards: list[dict], fetched_at: str) -> None:
    day = fetched_at[:10]
    for c in cards:
        prices = c.get("prices") or {}
        images = (c.get("image_uris") or {}).get("digital") or {}
        ink = c.get("ink") or "/".join(c.get("inks") or []) or None
        number = str(c["collector_number"])
        m = re.match(r"\d+", number)
        con.execute(
            """INSERT INTO cards(id, set_code, number, sort_number, name, version, rarity, ink, type, cost,
                                 image_small, image_normal, image_large, tcgplayer_url,
                                 usd, usd_foil, prices_updated_at, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 set_code=excluded.set_code, number=excluded.number, sort_number=excluded.sort_number,
                 name=excluded.name, version=excluded.version, rarity=excluded.rarity, ink=excluded.ink,
                 type=excluded.type, cost=excluded.cost, image_small=excluded.image_small,
                 image_normal=excluded.image_normal, image_large=excluded.image_large,
                 tcgplayer_url=excluded.tcgplayer_url, usd=excluded.usd, usd_foil=excluded.usd_foil,
                 prices_updated_at=excluded.prices_updated_at, raw=excluded.raw""",
            (
                c["id"], c["set"]["code"], number, int(m.group()) if m else None,
                c["name"], c.get("version"), c.get("rarity"), ink, " · ".join(c.get("type") or []),
                c.get("cost"), images.get("small"), images.get("normal"), images.get("large"),
                (c.get("purchase_uris") or {}).get("tcgplayer"),
                prices.get("usd"), prices.get("usd_foil"), fetched_at, json.dumps(c),
            ),
        )
        con.execute(
            "INSERT OR REPLACE INTO price_history(card_id, day, usd, usd_foil) VALUES (?, ?, ?, ?)",
            (c["id"], day, prices.get("usd"), prices.get("usd_foil")),
        )


def card(con: sqlite3.Connection, card_id: str) -> sqlite3.Row | None:
    return con.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()


def cards_in_set(con: sqlite3.Connection, set_code: str) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM cards WHERE set_code = ? ORDER BY sort_number, number", (set_code,)
    ).fetchall()


def resolve_card(con: sqlite3.Connection, ref: str) -> sqlite3.Row:
    """Acha uma carta por `SET/NUM` (ex.: `1/169`), id do Lorcast ou nome (`Scar - Fiery Usurper`)."""
    ref = ref.strip()
    if ref.startswith("crd_"):
        row = card(con, ref)
        if row:
            return row
    m = re.fullmatch(r"([A-Za-z0-9]+)\s*[/-]\s*(\w+)", ref)
    if m:
        row = con.execute(
            "SELECT * FROM cards WHERE set_code = ? COLLATE NOCASE AND number = ?", m.groups()
        ).fetchone()
        if row:
            return row
    name, _, version = (p.strip() for p in ref.partition(" - "))
    rows = con.execute(
        "SELECT * FROM cards WHERE name LIKE ? AND (? = '' OR version LIKE ?) ORDER BY set_code, sort_number",
        (f"%{name}%", version, f"%{version}%"),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise SystemExit(f"Nenhuma carta encontrada para {ref!r}.")
    options = "\n".join(f"  {r['set_code']}/{r['number']}  {display_name(r)}" for r in rows[:15])
    raise SystemExit(f"{ref!r} é ambíguo; use SET/NÚMERO. Candidatas:\n{options}")


def display_name(row: sqlite3.Row | dict) -> str:
    return f"{row['name']} - {row['version']}" if row["version"] else row["name"]


def price_on(con: sqlite3.Connection, card_id: str, foil: bool, day: str) -> float | None:
    """Preço de mercado num dia: o último conhecido até ele; senão o primeiro depois; senão o atual."""
    cols = ("usd_foil", "usd") if foil else ("usd", "usd_foil")
    queries = (
        "SELECT usd, usd_foil FROM price_history WHERE card_id = ? AND day <= ?"
        " AND COALESCE(usd, usd_foil) IS NOT NULL ORDER BY day DESC LIMIT 1",
        "SELECT usd, usd_foil FROM price_history WHERE card_id = ? AND day > ?"
        " AND COALESCE(usd, usd_foil) IS NOT NULL ORDER BY day LIMIT 1",
    )
    for q in queries:
        row = con.execute(q, (card_id, day)).fetchone()
        if row:
            return row[cols[0]] if row[cols[0]] is not None else row[cols[1]]
    current = card(con, card_id)
    return price_usd(current, foil) if current else None


def price_usd(row: sqlite3.Row | dict, foil: bool) -> float | None:
    """Preço de mercado; foil sem cotação própria cai no preço normal (e vice-versa)."""
    if foil:
        return row["usd_foil"] if row["usd_foil"] is not None else row["usd"]
    return row["usd"] if row["usd"] is not None else row["usd_foil"]
