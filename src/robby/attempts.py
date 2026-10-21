from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from robby import outbox as outbox_mod
from robby.draft import render_v1

COOLDOWN_HOURS = 48

_MESSAGE_KEYS = (
    "id",
    "customer_phone",
    "primary_quote_id",
    "body",
    "reason",
    "status",
    "idempotency_key",
    "outbox_id",
    "decision",
    "created_at",
    "updated_at",
    "sent_at",
)


class SendRejected(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _parse_now(now: str) -> datetime:
    return datetime.fromisoformat(now.replace("Z", "+00:00"))


def _as_message(row: sqlite3.Row) -> dict:
    return {key: row[key] for key in _MESSAGE_KEYS}


def _load_message(conn: sqlite3.Connection, message_id: str) -> dict:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _as_message(row)


def _quote_ids(conn: sqlite3.Connection, message_id: str, primary_quote_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT quote_id FROM message_quotes WHERE message_id = ?",
        (message_id,),
    ).fetchall()
    ids = [row["quote_id"] for row in rows]
    others = sorted(qid for qid in ids if qid != primary_quote_id)
    return [primary_quote_id, *others]


def _mark_sent(conn: sqlite3.Connection, message: dict, outbox_id: str, now: str) -> dict:
    v1 = conn.execute(
        "SELECT body FROM message_revisions WHERE message_id = ? AND version = 1",
        (message["id"],),
    ).fetchone()
    decision = "approved_as_is" if message["body"] == v1["body"] else "approved_with_edits"
    conn.execute(
        """
        UPDATE messages
        SET status = 'sent', outbox_id = ?, sent_at = ?, updated_at = ?, decision = ?
        WHERE id = ?
        """,
        (outbox_id, now, now, decision, message["id"]),
    )
    conn.commit()
    return _load_message(conn, message["id"])


def generate_draft(
    conn: sqlite3.Connection,
    primary_quote_id: str,
    now: str,
    *,
    reason: str = "quiet_open",
    complete=None,
) -> dict:
    quote = conn.execute("SELECT * FROM quotes WHERE id = ?", (primary_quote_id,)).fetchone()
    if quote is None or quote["status"] != "open":
        raise SendRejected("not_open")

    open_rows = conn.execute(
        "SELECT id, amount, status FROM quotes WHERE customer_phone = ? AND status = 'open'",
        (quote["customer_phone"],),
    ).fetchall()
    open_quotes = [
        {"id": row["id"], "amount": row["amount"], "status": row["status"]}
        for row in open_rows
    ]
    body = render_v1(
        customer_name=quote["customer_name"],
        tech_name=quote["tech_name"],
        reason=reason,
        primary_quote_id=primary_quote_id,
        open_quotes=open_quotes,
        now=now,
        complete=complete,
    )
    message_id = str(uuid.uuid4())
    idempotency_key = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO messages (
          id, customer_phone, primary_quote_id, body, reason, status,
          idempotency_key, outbox_id, decision, created_at, updated_at, sent_at
        ) VALUES (?, ?, ?, ?, ?, 'draft', ?, NULL, 'pending', ?, ?, NULL)
        """,
        (
            message_id,
            quote["customer_phone"],
            primary_quote_id,
            body,
            reason,
            idempotency_key,
            now,
            now,
        ),
    )
    conn.executemany(
        "INSERT INTO message_quotes (message_id, quote_id) VALUES (?, ?)",
        [(message_id, row["id"]) for row in open_quotes],
    )
    conn.execute(
        """
        INSERT INTO message_revisions (message_id, version, body, author, created_at)
        VALUES (?, 1, ?, 'engine', ?)
        """,
        (message_id, body, now),
    )
    conn.commit()
    return _load_message(conn, message_id)


def edit_draft(conn: sqlite3.Connection, message_id: str, body: str, now: str) -> dict:
    message = _load_message(conn, message_id)
    if message["status"] != "draft":
        raise SendRejected("illegal_status")
    next_version = conn.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM message_revisions WHERE message_id = ?",
        (message_id,),
    ).fetchone()[0]
    conn.execute(
        "UPDATE messages SET body = ?, updated_at = ? WHERE id = ?",
        (body, now, message_id),
    )
    conn.execute(
        """
        INSERT INTO message_revisions (message_id, version, body, author, created_at)
        VALUES (?, ?, ?, 'human', ?)
        """,
        (message_id, next_version, body, now),
    )
    conn.commit()
    return _load_message(conn, message_id)


def discard_attempt(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    confirmed: bool,
    now: str,
) -> dict:
    message = _load_message(conn, message_id)
    if not confirmed:
        return message
    if message["status"] not in ("draft", "failed"):
        raise SendRejected("illegal_status")
    conn.execute(
        "UPDATE messages SET status = 'cancelled', updated_at = ? WHERE id = ?",
        (now, message_id),
    )
    conn.commit()
    return _load_message(conn, message_id)


def send_attempt(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    confirmed: bool,
    now: str,
    enqueue=None,
    get_by_key=None,
) -> dict:
    if enqueue is None:
        enqueue = outbox_mod.enqueue
    if get_by_key is None:
        get_by_key = outbox_mod.get_by_key

    message = _load_message(conn, message_id)
    if not confirmed:
        return message
    if message["status"] not in ("draft", "failed"):
        raise SendRejected("illegal_status")

    quote = conn.execute(
        "SELECT * FROM quotes WHERE id = ?",
        (message["primary_quote_id"],),
    ).fetchone()
    if quote is None or quote["status"] != "open":
        raise SendRejected("not_open")

    now_dt = _parse_now(now)
    window = timedelta(hours=COOLDOWN_HOURS)
    sent_rows = conn.execute(
        """
        SELECT sent_at FROM messages
        WHERE customer_phone = ? AND status = 'sent' AND id != ?
        """,
        (message["customer_phone"], message_id),
    ).fetchall()
    for row in sent_rows:
        if row["sent_at"] is None:
            continue
        delta = now_dt - _parse_now(row["sent_at"])
        if timedelta(0) <= delta < window:
            raise SendRejected("cooldown")

    existing = get_by_key(conn, message["idempotency_key"])
    if existing is not None:
        return _mark_sent(conn, message, existing["id"], now)

    enqueue(
        conn,
        idempotency_key=message["idempotency_key"],
        payload={
            "customer_phone": message["customer_phone"],
            "body": message["body"],
            "quote_ids": _quote_ids(conn, message_id, message["primary_quote_id"]),
            "reason": message["reason"],
            "message_id": message["id"],
        },
        created_at=now,
    )
    verified = get_by_key(conn, message["idempotency_key"])
    if verified is None:
        conn.execute(
            "UPDATE messages SET status = 'failed', updated_at = ? WHERE id = ?",
            (now, message_id),
        )
        conn.commit()
        return _load_message(conn, message_id)
    return _mark_sent(conn, message, verified["id"], now)


def retry_attempt(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    confirmed: bool,
    now: str,
    enqueue=None,
    get_by_key=None,
) -> dict:
    return send_attempt(
        conn,
        message_id,
        confirmed=confirmed,
        now=now,
        enqueue=enqueue,
        get_by_key=get_by_key,
    )
