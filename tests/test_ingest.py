import json
from pathlib import Path

from robby.db import connect
from robby.ingest import ingest_events, ingest_seed

REPO_ROOT = Path(__file__).resolve().parents[1]
QUOTES_PATH = REPO_ROOT / "quotes.json"
EVENTS_PATH = REPO_ROOT / "events.jsonl"

SIX_TABLES = {
    "quotes",
    "events",
    "messages",
    "message_quotes",
    "message_revisions",
    "outbox",
}


def test_ingest_seed_counts():
    conn = connect(":memory:")
    ingest_seed(conn, str(QUOTES_PATH), str(EVENTS_PATH))
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 30
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 82


def test_ingest_seed_idempotent():
    conn = connect(":memory:")
    ingest_seed(conn, str(QUOTES_PATH), str(EVENTS_PATH))
    ingest_seed(conn, str(QUOTES_PATH), str(EVENTS_PATH))
    assert conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 30
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 82


def test_duplicate_event_id_inserts_one_row():
    conn = connect(":memory:")
    event = {
        "event_id": "dup-1",
        "quote_id": "Q-1",
        "type": "quote_viewed",
        "timestamp": "2026-08-01T00:00:00Z",
    }
    ingest_events(conn, [event, dict(event)])
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_extra_keys_land_in_payload_and_unknown_type_is_allowed():
    conn = connect(":memory:")
    ingest_events(
        conn,
        [
            {
                "event_id": "e1",
                "quote_id": "Q-1",
                "type": "unknown_event",
                "timestamp": "2026-08-01T00:00:00Z",
                "channel": "sms",
                "direction": "outbound",
            }
        ],
    )
    row = conn.execute(
        "SELECT type, payload FROM events WHERE event_id = ?",
        ("e1",),
    ).fetchone()
    assert row["type"] == "unknown_event"
    payload = json.loads(row["payload"])
    assert payload == {"channel": "sms", "direction": "outbound"}


def test_events_query_is_chronological_after_reverse_insert():
    conn = connect(":memory:")
    ingest_events(
        conn,
        [
            {
                "event_id": "e3",
                "quote_id": "Q-1",
                "type": "quote_viewed",
                "timestamp": "2026-08-03T00:00:00Z",
            },
            {
                "event_id": "e2",
                "quote_id": "Q-1",
                "type": "quote_viewed",
                "timestamp": "2026-08-02T00:00:00Z",
            },
            {
                "event_id": "e1",
                "quote_id": "Q-1",
                "type": "quote_viewed",
                "timestamp": "2026-08-01T00:00:00Z",
            },
        ],
    )
    rows = conn.execute(
        "SELECT event_id FROM events WHERE quote_id = ? AND timestamp <= ? ORDER BY timestamp",
        ("Q-1", "2026-08-03T00:00:00Z"),
    ).fetchall()
    assert [row["event_id"] for row in rows] == ["e1", "e2", "e3"]


def test_connect_memory_has_six_tables():
    conn = connect(":memory:")
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert names == SIX_TABLES
