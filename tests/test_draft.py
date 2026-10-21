import pytest

from robby.attempts import generate_draft
from robby.db import connect
from robby.draft import first_name, render_v1, template_body
from robby.ingest import ingest_quotes

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
    assert body == (
        "Hi Karen, it's Alex following up. Open quotes: Q-1003 ($850), Q-1025 ($850). "
        "Reason: quiet_open."
    )


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
