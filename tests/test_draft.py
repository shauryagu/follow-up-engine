import pytest

from robby.attempts import generate_draft
from robby.db import connect
from robby.draft import DraftGenerationError, first_name, render_v1, template_body
from robby.ingest import ingest_events, ingest_quotes

PHONE = "+19175552003"
NOW = "2026-08-10T00:00:00Z"
CREATED = "2026-08-01T00:00:00Z"

TEMPLATE_KW = dict(
    customer_name="Karen Nguyen",
    tech_name="Alex",
    reason="quiet_open",
    primary_quote_id="Q-1003",
    open_quotes=[
        {"id": "Q-1025", "amount": 850, "status": "open"},
        {"id": "Q-1003", "amount": 850, "status": "open"},
    ],
)


@pytest.fixture(autouse=True)
def _never_hit_network(monkeypatch):
    monkeypatch.delenv("BASETEN_API_KEY", raising=False)


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


def test_template_body_lists_open_quotes_primary_first():
    assert first_name("Karen Nguyen") == "Karen"
    body = template_body(**TEMPLATE_KW)
    assert "Q-1003 ($850)" in body
    assert body.index("Q-1003") < body.index("Q-1025")
    assert "following up" in body.lower()


def test_template_waiting_on_reply_differs_from_check_back():
    reply = template_body(**{**TEMPLATE_KW, "reason": "unanswered_reply"})
    ours = template_body(**{**TEMPLATE_KW, "reason": "quiet_open", "stance": "check_back"})
    first = template_body(**TEMPLATE_KW)
    assert "reply" in reply.lower()
    assert "last note" in ours.lower() or "checking back" in ours.lower()
    assert reply != ours
    assert ours != first
    assert "reply" not in ours.lower()


def test_render_v1_complete_none_equals_template():
    assert render_v1(now=NOW, complete=None, **TEMPLATE_KW) == template_body(**TEMPLATE_KW)


def test_render_v1_complete_hook_returns_llm_sms():
    assert render_v1(now=NOW, complete=lambda prompt: "LLM SMS", **TEMPLATE_KW) == "LLM SMS"


def test_render_v1_whitespace_complete_falls_back_to_template():
    expected = template_body(**TEMPLATE_KW)
    assert render_v1(now=NOW, complete=lambda prompt: "  ", **TEMPLATE_KW) == expected


def test_generate_draft_complete_hook_stores_llm_sms_as_revision_v1():
    conn = _conn_with(_quote())
    draft = generate_draft(conn, "Q-1003", NOW, complete=lambda prompt: "LLM SMS")
    assert draft["body"] == "LLM SMS"
    rev = conn.execute(
        "SELECT version, body, author FROM message_revisions WHERE message_id = ?",
        (draft["id"],),
    ).fetchone()
    assert rev["version"] == 1
    assert rev["body"] == "LLM SMS"
    assert rev["author"] == "engine"


def test_prompt_omits_phone_and_last_name():
    captured = {}

    def complete(prompt):
        captured["prompt"] = prompt
        return "Hi Karen — checking in on the quote."

    render_v1(now=NOW, complete=complete, **TEMPLATE_KW)
    prompt = captured["prompt"]
    assert PHONE not in prompt
    assert "Nguyen" not in prompt
    assert "Karen" in prompt
    assert "first follow-up" in prompt.lower() or "first follow up" in prompt.lower()


def test_prompt_references_prior_contact_not_first_touch():
    captured = {}

    def complete(prompt):
        captured["prompt"] = prompt
        return "Following up on my last note."

    render_v1(
        now=NOW,
        complete=complete,
        contact={
            "prior_contact": True,
            "last_outbound": "2026-08-08T12:00:00Z",
            "last_reply": None,
            "last_view": "2026-08-09T00:00:00Z",
        },
        **TEMPLATE_KW,
    )
    prompt = captured["prompt"].lower()
    assert "our own" in prompt or "our last" in prompt or "check back" in prompt
    assert "this is a first follow-up" not in prompt
    assert "they replied" not in prompt
    assert PHONE not in captured["prompt"]


def test_prompt_waiting_on_reply_is_not_check_back():
    captured = {}

    def complete(prompt):
        captured["prompt"] = prompt
        return "Got your note — what works?"

    render_v1(
        now=NOW,
        complete=complete,
        contact={
            "prior_contact": True,
            "stance": "waiting_on_us",
            "last_outbound": "2026-08-08T12:00:00Z",
            "last_reply": "2026-08-09T12:00:00Z",
            "last_view": "2026-08-09T00:00:00Z",
        },
        **{**TEMPLATE_KW, "reason": "unanswered_reply"},
    )
    prompt = captured["prompt"].lower()
    assert "replied" in prompt
    assert "waiting on us" in prompt or "have not answered" in prompt
    assert "this is a first follow-up" not in prompt
    assert "follow-up on our own" not in prompt


def test_on_chunk_receives_text_and_generate_saves_only_after_complete():
    chunks = []
    conn = _conn_with(_quote())

    def complete(prompt):
        assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0
        return "Saved after stream"

    draft = generate_draft(
        conn,
        "Q-1003",
        NOW,
        complete=complete,
        on_chunk=chunks.append,
    )
    assert chunks == ["Saved after stream"]
    assert draft["body"] == "Saved after stream"
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 1


def test_model_failure_does_not_save_and_asks_to_retry():
    conn = _conn_with(_quote())

    def complete(prompt):
        raise RuntimeError("connection reset")

    with pytest.raises(DraftGenerationError, match="try again") as caught:
        generate_draft(conn, "Q-1003", NOW, complete=complete)
    assert "connection reset" in str(caught.value)
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 0


def test_generate_draft_uses_events_as_prior_contact():
    captured = {}

    def complete(prompt):
        captured["prompt"] = prompt
        return "Checking back as promised."

    conn = _conn_with(_quote(last_contact_at="2026-08-08T12:00:00Z"))
    ingest_events(
        conn,
        [
            {
                "event_id": "e1",
                "quote_id": "Q-1003",
                "type": "message_sent",
                "timestamp": "2026-08-08T12:00:00Z",
            }
        ],
    )
    generate_draft(conn, "Q-1003", NOW, complete=complete)
    prompt = captured["prompt"].lower()
    assert "our own" in prompt or "our last" in prompt or "check back" in prompt
    assert "this is a first follow-up" not in prompt


def test_generate_draft_reply_stance_from_events():
    captured = {}

    def complete(prompt):
        captured["prompt"] = prompt
        return "Got your note."

    conn = _conn_with(_quote())
    ingest_events(
        conn,
        [
            {
                "event_id": "e-out",
                "quote_id": "Q-1003",
                "type": "message_sent",
                "timestamp": "2026-08-08T12:00:00Z",
            },
            {
                "event_id": "e-in",
                "quote_id": "Q-1003",
                "type": "customer_replied",
                "timestamp": "2026-08-09T12:00:00Z",
            },
        ],
    )
    generate_draft(conn, "Q-1003", NOW, reason="unanswered_reply", complete=complete)
    prompt = captured["prompt"].lower()
    assert "replied" in prompt
    assert "this is a first follow-up" not in prompt


def test_generate_draft_omits_accepted_sibling():
    conn = _conn_with(
        _quote(id="Q-1003"),
        _quote(id="Q-1025"),
        _quote(id="Q-1099", status="accepted"),
    )
    draft = generate_draft(conn, "Q-1003", NOW)
    expected = template_body(
        customer_name="Karen Nguyen",
        tech_name="Alex",
        reason="quiet_open",
        primary_quote_id="Q-1003",
        open_quotes=[
            {"id": "Q-1003", "amount": 850, "status": "open"},
            {"id": "Q-1025", "amount": 850, "status": "open"},
        ],
    )
    assert draft["body"] == expected
    assert "Q-1099" not in draft["body"]
    quote_ids = {
        row["quote_id"]
        for row in conn.execute(
            "SELECT quote_id FROM message_quotes WHERE message_id = ?",
            (draft["id"],),
        )
    }
    assert quote_ids == {"Q-1003", "Q-1025"}


def test_load_env_overrides_stale_shell_key(tmp_path, monkeypatch):
    import os

    from robby.draft import _load_env

    envfile = tmp_path / ".env"
    envfile.write_text("BASETEN_API_KEY=from-file\n")
    monkeypatch.setenv("BASETEN_API_KEY", "stale-shell")
    _load_env(envfile)
    assert os.environ["BASETEN_API_KEY"] == "from-file"
