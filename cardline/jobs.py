"""Atualizações disparadas na página (preços, números das redes, sincronização de sets): cada execução vira
uma linha na aba Pipelines, com o andamento enquanto roda e o resultado depois."""

from __future__ import annotations

import json

from . import db

KINDS = {"precos": "Atualização de preços", "redes": "Números das redes", "sync": "Sincronização de sets"}


def create(con, kind: str, user_id: int | None = None) -> int:
    with con:
        return con.execute("INSERT INTO jobs(kind, status, message, started_at, user_id) VALUES (?, 'running', 'Começando', ?, ?)",
                           (kind, db.now(), user_id)).lastrowid


def finish(con, job_id: int, status: str, message: str | None, result: dict | None = None, log: str | None = None) -> None:
    with con:
        con.execute("UPDATE jobs SET status = ?, message = ?, result = ?, log = COALESCE(?, log), finished_at = ? WHERE id = ?",
                    (status, message, json.dumps(result, ensure_ascii=False) if result is not None else None, log,
                     db.now(), job_id))


def as_json(r, with_log: bool = False) -> dict:
    out = {"id": r["id"], "kind": r["kind"], "label": KINDS.get(r["kind"], r["kind"]), "status": r["status"],
           "message": r["message"], "result": json.loads(r["result"]) if r["result"] else None,
           "started_at": r["started_at"], "finished_at": r["finished_at"]}
    if with_log:
        out["log"] = r["log"]
    return out


def recent(con, user_id: int, limit: int = 100) -> list[dict]:
    return [as_json(r) for r in con.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit))]


def get(con, job_id: int, user_id: int) -> dict | None:
    r = con.execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, user_id)).fetchone()
    return as_json(r, with_log=True) if r else None


def interrupted(con) -> None:
    """Atualização que estava rodando quando o servidor parou."""
    with con:
        con.execute("UPDATE jobs SET status = 'interrupted', message = 'O servidor reiniciou no meio da atualização.',"
                    " finished_at = ? WHERE status = 'running'", (db.now(),))
