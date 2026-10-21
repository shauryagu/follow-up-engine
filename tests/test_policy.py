from robby.db import connect
from robby.ingest import ingest_events, ingest_quotes
from robby.policy import Candidate, default_now, recommend

NOW_AUG_02 = "2026-08-02T00:00:00Z"
NOW_AUG_15 = "2026-08-15T00:00:00Z"
CONTACT_AUG_01 = "2026-08-01T00:00:00Z"
SIGNAL_AUG_01_20 = "2026-08-01T20:00:00Z"


def _quote(**overrides):
    row = {
        "id": "A",
        "customer_name": "Pat",
        "customer_phone": "+15550000001",
        "tech_name": "Alex",
        "amount": 5000,
        "status": "open",
        "created_at": CONTACT_AUG_01,
        "last_contact_at": CONTACT_AUG_01,
    }
    row.update(overrides)
    return row


def _event(event_id, quote_id, type, timestamp):
    return {
        "event_id": event_id,
        "quote_id": quote_id,
        "type": type,
        "timestamp": timestamp,
    }


def _conn_with(quotes, events=()):
    conn = connect(":memory:")
    ingest_quotes(conn, quotes)
    if events:
        ingest_events(conn, events)
    return conn


def _by_id(candidates):
    return {c.quote_id: c for c in candidates}


def _insert_sent(conn, *, phone, quote_id, sent_at, message_id="m-sent"):
    conn.execute(
        """
        INSERT INTO messages (
          id, customer_phone, primary_quote_id, body, reason, status,
          idempotency_key, outbox_id, decision, created_at, updated_at, sent_at
        ) VALUES (?, ?, ?, 'hi', 'quiet_open', 'sent', ?, NULL, 'approved_as_is', ?, ?, ?)
        """,
        (message_id, phone, quote_id, f"key-{message_id}", sent_at, sent_at, sent_at),
    )
    conn.commit()


def test_unanswered_reply_beats_same_size_hot_view():
    conn = _conn_with(
        [
            _quote(id="A", customer_phone="+15550000001", amount=5000),
            _quote(id="B", customer_phone="+15550000002", amount=5000),
        ],
        [
            _event("e-a", "A", "customer_replied", SIGNAL_AUG_01_20),
            _event("e-b", "B", "quote_viewed", SIGNAL_AUG_01_20),
        ],
    )
    ranked = recommend(conn, NOW_AUG_02)
    by_id = _by_id(ranked)
    assert by_id["A"].reason == "unanswered_reply"
    assert by_id["B"].reason == "hot_view"
    assert by_id["A"].score > by_id["B"].score


def test_two_week_neglected_20000_quiet_beats_same_day_400_hot_view():
    conn = _conn_with(
        [
            _quote(id="A", amount=20000, customer_phone="+15550000001"),
            _quote(
                id="B",
                amount=400,
                customer_phone="+15550000002",
                created_at=NOW_AUG_15,
                last_contact_at=NOW_AUG_15,
            ),
        ],
        [_event("e-b", "B", "quote_viewed", NOW_AUG_15)],
    )
    ranked = recommend(conn, NOW_AUG_15)
    assert [c.quote_id for c in ranked][0] == "A"
    by_id = _by_id(ranked)
    assert by_id["A"].reason == "quiet_open"
    assert by_id["B"].reason == "hot_view"


def test_same_day_20000_hot_view_beats_two_week_neglected_400_quiet():
    conn = _conn_with(
        [
            _quote(id="A", amount=400, customer_phone="+15550000001"),
            _quote(
                id="B",
                amount=20000,
                customer_phone="+15550000002",
                created_at=NOW_AUG_15,
                last_contact_at=NOW_AUG_15,
            ),
        ],
        [_event("e-b", "B", "quote_viewed", NOW_AUG_15)],
    )
    ranked = recommend(conn, NOW_AUG_15)
    assert [c.quote_id for c in ranked][0] == "B"


def test_view_after_now_does_not_count():
    conn = _conn_with(
        [_quote(id="A", amount=5000)],
        [_event("e-future", "A", "quote_viewed", "2026-08-16T00:00:00Z")],
    )
    ranked = recommend(conn, NOW_AUG_15)
    assert len(ranked) == 1
    assert ranked[0].reason == "quiet_open"


def test_cooldown_marks_sendable_false_but_still_lists_quote():
    conn = _conn_with(
        [
            _quote(id="A", amount=20000, customer_phone="+15550000001"),
            _quote(
                id="B",
                amount=400,
                customer_phone="+15550000002",
                created_at=NOW_AUG_15,
                last_contact_at=NOW_AUG_15,
            ),
        ],
        [_event("e-b", "B", "quote_viewed", NOW_AUG_15)],
    )
    _insert_sent(conn, phone="+15550000002", quote_id="B", sent_at=NOW_AUG_15)
    ranked = recommend(conn, NOW_AUG_15)
    by_id = _by_id(ranked)
    assert "B" in by_id
    assert by_id["B"].sendable is False
    assert by_id["A"].sendable is True


def test_recommend_sorts_by_score_descending():
    conn = _conn_with(
        [
            _quote(id="Q-low", amount=100, customer_phone="+15550000001"),
            _quote(id="Q-high", amount=9000, customer_phone="+15550000002"),
            _quote(id="Q-mid", amount=800, customer_phone="+15550000003"),
        ]
    )
    ranked = recommend(conn, NOW_AUG_15)
    scores = [c.score for c in ranked]
    assert scores == sorted(scores, reverse=True)
    assert [c.quote_id for c in ranked] == ["Q-high", "Q-mid", "Q-low"]
    assert all(isinstance(c, Candidate) for c in ranked)


def test_only_open_quotes_are_recommended():
    conn = _conn_with(
        [
            _quote(id="A", status="open"),
            _quote(id="B", status="accepted", customer_phone="+15550000002"),
            _quote(id="C", status="dismissed", customer_phone="+15550000003"),
        ]
    )
    ranked = recommend(conn, NOW_AUG_15)
    assert [c.quote_id for c in ranked] == ["A"]


def test_default_now_is_max_event_timestamp():
    conn = _conn_with(
        [_quote()],
        [
            _event("e1", "A", "quote_viewed", "2026-08-01T00:00:00Z"),
            _event("e2", "A", "quote_viewed", "2026-08-10T12:00:00Z"),
        ],
    )
    assert default_now(conn) == "2026-08-10T12:00:00Z"
