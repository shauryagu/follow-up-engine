from robby.app import REASON_LABELS, create_app
from robby.attempts import generate_draft, send_attempt
from robby.db import connect
from robby.draft import DraftGenerationError
from robby.ingest import ingest_events, ingest_quotes

from fastapi.testclient import TestClient

NOW = "2026-08-10T00:00:00Z"
CREATED = "2026-08-01T00:00:00Z"


def _quote(**overrides):
    row = {
        "id": "Q-1003",
        "customer_name": "Karen Nguyen",
        "customer_phone": "+19175552003",
        "tech_name": "Alex",
        "amount": 850,
        "status": "open",
        "created_at": CREATED,
        "last_contact_at": None,
    }
    row.update(overrides)
    return row


def _client():
    conn = connect(":memory:")
    ingest_quotes(
        conn,
        [
            _quote(),
            _quote(id="Q-1025", amount=850),
            _quote(id="Q-1099", status="accepted", customer_phone="+19175552999"),
        ],
    )
    ingest_events(
        conn,
        [
            {
                "event_id": "e-view",
                "quote_id": "Q-1003",
                "type": "quote_viewed",
                "timestamp": "2026-08-09T12:00:00Z",
            }
        ],
    )
    app = create_app(conn=conn)
    return TestClient(app), conn


def test_list_shows_open_quotes_and_reason_labels():
    client, _conn = _client()
    response = client.get("/", params={"now": NOW})
    assert response.status_code == 200
    body = response.text
    assert "Karen Nguyen" in body
    assert "Q-1099" not in body
    assert "last 48 hours" in body or REASON_LABELS["quiet_open"] in body or REASON_LABELS["unanswered_reply"] in body
    assert "shop outbox" in body.lower()


def test_quote_page_shows_customer_and_generate():
    client, _conn = _client()
    response = client.get("/quotes/Q-1003", params={"now": NOW})
    assert response.status_code == 200
    assert "Karen Nguyen" in response.text
    assert "Generate" in response.text


def test_post_drafts_saves_row_and_shows_body():
    client, conn = _client()
    before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    response = client.post(
        "/quotes/Q-1003/drafts",
        data={"now": NOW, "use_template": "1"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    after = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert after == before + 1
    body = conn.execute("SELECT body FROM messages").fetchone()["body"]
    assert "Q-1003" in body
    assert body in response.text or "Q-1003" in response.text


def test_list_explains_hot_view_count():
    client, _conn = _client()
    body = client.get("/", params={"now": NOW}).text.lower()
    assert "viewed" in body
    assert "last 48 hours" in body


def test_quote_page_shows_rank_and_human_time():
    client, _conn = _client()
    page = client.get("/quotes/Q-1003", params={"now": NOW})
    text = page.text
    assert "Why this rank" in text
    assert "Aug 9, 2026" in text
    assert "How ranking works" in text


def test_ranking_page_explains_policy():
    client, _conn = _client()
    page = client.get("/ranking", params={"now": NOW})
    assert page.status_code == 200
    text = page.text.lower()
    assert "value" in text
    assert "48 hours" in text
    assert "cooldown" in text


def test_send_requires_confirm_then_shows_outbox():
    client, conn = _client()
    draft = generate_draft(conn, "Q-1003", NOW, complete=lambda prompt: "Hi Karen")
    skipped = client.post(
        f"/messages/{draft['id']}/send",
        data={"now": NOW, "confirmed": "false"},
        follow_redirects=True,
    )
    assert skipped.status_code == 200
    assert conn.execute("SELECT status FROM messages WHERE id = ?", (draft["id"],)).fetchone()["status"] == "draft"
    sent = client.post(
        f"/messages/{draft['id']}/send",
        data={"now": NOW, "confirmed": "true"},
        follow_redirects=True,
    )
    assert sent.status_code == 200
    assert "In the outbox" in sent.text
    assert "Back to quotes" in sent.text
    assert conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()["n"] == 1


def test_cooldown_keeps_generate_disables_send():
    client, conn = _client()
    draft = generate_draft(conn, "Q-1003", NOW, complete=lambda prompt: "Hi")
    send_attempt(conn, draft["id"], confirmed=True, now=NOW)
    page = client.get("/quotes/Q-1025", params={"now": NOW})
    assert page.status_code == 200
    assert "You already followed up this number" in page.text
    assert "Generate" in page.text


def test_discard_confirmed_cancels_draft():
    client, conn = _client()
    draft = generate_draft(conn, "Q-1003", NOW, complete=lambda prompt: "Hi")
    response = client.post(
        f"/messages/{draft['id']}/discard",
        data={"now": NOW, "confirmed": "true"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert conn.execute("SELECT status FROM messages WHERE id = ?", (draft["id"],)).fetchone()["status"] == "cancelled"


def test_model_error_returns_retry_copy_without_saving():
    client, conn = _client()

    def boom(*_args, **_kwargs):
        raise DraftGenerationError(
            "The model couldn't generate a draft. Wait a moment and try again. "
            "(APIStatusError: Error code: 401)"
        )

    app = client.app
    app.state.generate_draft = boom
    before = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    response = client.post(
        "/quotes/Q-1003/drafts",
        data={"now": NOW},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "try again" in response.text.lower()
    assert "401" in response.text
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == before
