from __future__ import annotations

import json
import sqlite3
import uuid


def enqueue(conn: sqlite3.Connection, *, idempotency_key: str, payload: dict, created_at: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO outbox (id, idempotency_key, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (str(uuid.uuid4()), idempotency_key, json.dumps(payload), created_at),
    )
    conn.commit()


def get_by_key(conn: sqlite3.Connection, idempotency_key: str) -> dict | None:
    row = conn.execute(
        "SELECT id, idempotency_key, payload, created_at FROM outbox WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "payload": json.loads(row["payload"]),
        "created_at": row["created_at"],
    }
