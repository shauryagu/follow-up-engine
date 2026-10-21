from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_CORE_EVENT_KEYS = frozenset({"event_id", "quote_id", "type", "timestamp"})

_UPSERT_QUOTE_SQL = """
INSERT INTO quotes (
  id, customer_name, customer_phone, tech_name, amount, status, created_at, last_contact_at
) VALUES (
  :id, :customer_name, :customer_phone, :tech_name, :amount, :status, :created_at, :last_contact_at
)
ON CONFLICT(id) DO UPDATE SET
  customer_name = excluded.customer_name,
  customer_phone = excluded.customer_phone,
  tech_name = excluded.tech_name,
  amount = excluded.amount,
  status = excluded.status,
  created_at = excluded.created_at,
  last_contact_at = excluded.last_contact_at
"""

_INSERT_EVENT_SQL = """
INSERT OR IGNORE INTO events (event_id, quote_id, type, timestamp, payload)
VALUES (:event_id, :quote_id, :type, :timestamp, :payload)
"""


def ingest_quotes(conn: sqlite3.Connection, quotes: Iterable[dict[str, Any]]) -> None:
    conn.executemany(_UPSERT_QUOTE_SQL, quotes)
    conn.commit()


def ingest_events(conn: sqlite3.Connection, events: Iterable[dict[str, Any]]) -> None:
    rows = []
    for event in events:
        payload = {key: value for key, value in event.items() if key not in _CORE_EVENT_KEYS}
        rows.append(
            {
                "event_id": event["event_id"],
                "quote_id": event["quote_id"],
                "type": event["type"],
                "timestamp": event["timestamp"],
                "payload": json.dumps(payload),
            }
        )
    conn.executemany(_INSERT_EVENT_SQL, rows)
    conn.commit()


def ingest_seed(conn: sqlite3.Connection, quotes_path: str, events_path: str) -> None:
    quotes = json.loads(Path(quotes_path).read_text())
    events = [
        json.loads(line)
        for line in Path(events_path).read_text().splitlines()
        if line.strip()
    ]
    ingest_quotes(conn, quotes)
    ingest_events(conn, events)
