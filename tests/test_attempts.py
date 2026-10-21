import pytest

from robby.attempts import (
    SendRejected,
    discard_attempt,
    edit_draft,
    generate_draft,
    retry_attempt,
    send_attempt,
)
from robby.db import connect
from robby.ingest import ingest_quotes
from robby.outbox import enqueue

PHONE = "+19175552003"
NOW = "2026-08-10T00:00:00Z"
CREATED = "2026-08-01T00:00:00Z"


def _quote(**overrides):
    row = {
        "id": "Q-1003",
        "customer_name": "Karen Nguyen",
        "customer_phone": PHONE,
        "tech_name": "Alex",
        "amount": 850,
        "status": "open",
        "created_at": CREATED,
        "last_contact_at": None,
    }
    row.update(overrides)
    return row


def _conn_with(*quotes):
    conn = connect(":memory:")
    ingest_quotes(conn, quotes)
    return conn


def _assert_no_sending(conn):
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'sending'").fetchone()[0]
    assert n == 0


def test_unconfirmed_send_does_not_enqueue_leaves_draft():
    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW)
    result = send_attempt(conn, draft["id"], confirmed=False, now=NOW)
    assert result["status"] == "draft"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_unconfirmed_discard_leaves_draft():
    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW)
    result = discard_attempt(conn, draft["id"], confirmed=False, now=NOW)
    assert result["status"] == "draft"


def test_verify_miss_fails_then_retry_sends_once():
    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW)

    def miss(_conn, _key):
        return None

    failed = send_attempt(conn, draft["id"], confirmed=True, now=NOW, get_by_key=miss)
    assert failed["status"] == "failed"
    _assert_no_sending(conn)

    sent = retry_attempt(conn, draft["id"], confirmed=True, now=NOW)
    assert sent["status"] == "sent"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    _assert_no_sending(conn)


def test_crash_recovery_sends_without_second_outbox_row():
    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW)
    enqueue(
        conn,
        idempotency_key=draft["idempotency_key"],
        payload={"message_id": draft["id"]},
        created_at=NOW,
    )
    sent = send_attempt(conn, draft["id"], confirmed=True, now=NOW)
    assert sent["status"] == "sent"
    assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    _assert_no_sending(conn)


def test_phone_cooldown_blocks_send_not_draft():
    conn = _conn_with(_quote(id="Q-1003"), _quote(id="Q-1025"))
    first = generate_draft(conn, "Q-1003", NOW)
    sent = send_attempt(conn, first["id"], confirmed=True, now=NOW)
    assert sent["status"] == "sent"

    second = generate_draft(conn, "Q-1025", NOW)
    assert second["status"] == "draft"
    with pytest.raises(SendRejected) as exc:
        send_attempt(conn, second["id"], confirmed=True, now=NOW)
    assert exc.value.code == "cooldown"

    quote_ids = {
        row["quote_id"]
        for row in conn.execute(
            "SELECT quote_id FROM message_quotes WHERE message_id = ?",
            (second["id"],),
        )
    }
    assert quote_ids == {"Q-1003", "Q-1025"}
    _assert_no_sending(conn)


def test_generate_draft_twice_two_rows():
    conn = _conn_with(_quote())
    first = generate_draft(conn, "Q-1003", NOW)
    second = generate_draft(conn, "Q-1003", NOW)
    assert first["id"] != second["id"]
    assert first["status"] == "draft"
    assert second["status"] == "draft"
    n = conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'draft'").fetchone()[0]
    assert n == 2


def test_primary_not_open_rejects_generate_and_send():
    conn = _conn_with(_quote(status="accepted"))
    with pytest.raises(SendRejected) as exc:
        generate_draft(conn, "Q-1003", NOW)
    assert exc.value.code == "not_open"

    conn = _conn_with(_quote(status="dismissed"))
    with pytest.raises(SendRejected) as exc:
        generate_draft(conn, "Q-1003", NOW)
    assert exc.value.code == "not_open"

    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW)
    conn.execute("UPDATE quotes SET status = 'accepted' WHERE id = 'Q-1003'")
    conn.commit()
    with pytest.raises(SendRejected) as exc:
        send_attempt(conn, draft["id"], confirmed=True, now=NOW)
    assert exc.value.code == "not_open"
    assert (
        conn.execute("SELECT status FROM messages WHERE id = ?", (draft["id"],)).fetchone()[0]
        == "draft"
    )


def test_confirmed_discard_cancels_draft():
    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW)
    result = discard_attempt(conn, draft["id"], confirmed=True, now=NOW)
    assert result["status"] == "cancelled"


def test_decision_as_is_unless_edited():
    conn = _conn_with(
        _quote(id="Q-1003"),
        _quote(id="Q-2000", customer_phone="+15550001111", customer_name="Other"),
    )
    first = generate_draft(conn, "Q-1003", NOW)
    sent = send_attempt(conn, first["id"], confirmed=True, now=NOW)
    assert sent["decision"] == "approved_as_is"

    second = generate_draft(conn, "Q-2000", NOW)
    edit_draft(conn, second["id"], "hello from human", NOW)
    sent_edited = send_attempt(conn, second["id"], confirmed=True, now=NOW)
    assert sent_edited["decision"] == "approved_with_edits"
    _assert_no_sending(conn)


def test_messages_never_persist_sending():
    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW)
    send_attempt(conn, draft["id"], confirmed=True, now=NOW)
    _assert_no_sending(conn)

    crashed = _conn_with(_quote())
    crash_draft = generate_draft(crashed, "Q-1003", NOW)
    enqueue(
        crashed,
        idempotency_key=crash_draft["idempotency_key"],
        payload={"message_id": crash_draft["id"]},
        created_at=NOW,
    )
    send_attempt(crashed, crash_draft["id"], confirmed=True, now=NOW)
    _assert_no_sending(crashed)

    missed = _conn_with(_quote())
    miss_draft = generate_draft(missed, "Q-1003", NOW)
    send_attempt(missed, miss_draft["id"], confirmed=True, now=NOW, get_by_key=lambda _c, _k: None)
    _assert_no_sending(missed)
