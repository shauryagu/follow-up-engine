from robby.db import connect
from robby.outbox import enqueue, get_by_key

NOW = "2026-08-10T00:00:00Z"
PAYLOAD = {"customer_phone": "+19175552003", "body": "hi"}


def test_enqueue_then_get_by_key_returns_payload():
    conn = connect(":memory:")
    enqueue(conn, idempotency_key="k1", payload=PAYLOAD, created_at=NOW)
    row = get_by_key(conn, "k1")
    assert row is not None
    assert row["idempotency_key"] == "k1"
    assert row["payload"] == PAYLOAD
    assert row["created_at"] == NOW
    assert row["id"]


def test_enqueue_twice_same_key_one_row():
    conn = connect(":memory:")
    enqueue(conn, idempotency_key="k1", payload=PAYLOAD, created_at=NOW)
    first = get_by_key(conn, "k1")
    enqueue(
        conn,
        idempotency_key="k1",
        payload={"other": True},
        created_at="2026-08-11T00:00:00Z",
    )
    second = get_by_key(conn, "k1")
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    assert second == first
    assert second["payload"] == PAYLOAD
