from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from robby.attempts import SendRejected, discard_attempt, edit_draft, generate_draft, send_attempt
from robby.db import connect
from robby.draft import DraftGenerationError
from robby.display import explain_reason, format_clock, format_when, rank_blurb, ranking_copy
from robby.ingest import ingest_seed
from robby.policy import Candidate, default_now, recommend

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SEED_QUOTES = ROOT / "quotes.json"
SEED_EVENTS = ROOT / "events.jsonl"

BANNER = "Sends write to the shop outbox. Nothing goes to the customer's phone in this demo."
COOLDOWN_COPY = "You already followed up this number. You can still draft. Send unlocks after 48 hours."
RETRY_COPY = "The model couldn't generate a draft. Wait a moment and try again."

REASON_LABELS = {
    "unanswered_reply": "They replied — waiting on us",
    "hot_view": "Opened the quote in the last 48 hours",
    "quiet_open": "Open, no recent engagement",
}


def _db_path() -> str:
    return os.environ.get("ROBBY_DB", str(ROOT / "data" / "robby.sqlite"))


def _writer_label() -> str:
    if (os.environ.get("BASETEN_API_KEY") or "").strip():
        return "Written with AI"
    return "Template (no API key)"


def _now(conn, now: str | None) -> str:
    if now:
        return now
    try:
        return default_now(conn)
    except ValueError:
        return "1970-01-01T00:00:00Z"


def _flash_url(path: str, now: str, flash: str, **extra: str) -> str:
    parts = [f"now={quote_plus(now)}", f"flash={quote_plus(flash)}"]
    for key, value in extra.items():
        if value:
            parts.append(f"{key}={quote_plus(value)}")
    return f"{path}?{'&'.join(parts)}"


def _candidates_by_id(conn, now: str) -> dict[str, Candidate]:
    return {c.quote_id: c for c in recommend(conn, now)}


def _quote_row(conn, quote_id: str):
    row = conn.execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    return dict(row) if row else None


def _thread(conn, quote_id: str, phone: str, now: str) -> list[dict]:
    items = []
    for row in conn.execute(
        "SELECT type, timestamp, payload FROM events WHERE quote_id = ? AND timestamp <= ? ORDER BY timestamp",
        (quote_id, now),
    ):
        items.append(
            {
                "kind": row["type"],
                "at": row["timestamp"],
                "at_label": format_when(row["timestamp"], now),
                "detail": row["payload"],
            }
        )
    for row in conn.execute(
        """
        SELECT id, body, sent_at FROM messages
        WHERE customer_phone = ? AND status = 'sent' AND sent_at <= ?
        ORDER BY sent_at
        """,
        (phone, now),
    ):
        items.append(
            {
                "kind": "outbox_sent",
                "at": row["sent_at"],
                "at_label": format_when(row["sent_at"], now),
                "detail": row["body"],
            }
        )
    items.sort(key=lambda item: item["at"] or "")
    return items


def _drafts_for_phone(conn, phone: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, body, status, reason, created_at, primary_quote_id
        FROM messages
        WHERE customer_phone = ? AND status IN ('draft', 'failed', 'cancelled', 'sent')
        ORDER BY created_at DESC
        """,
        (phone,),
    ).fetchall()
    return [dict(row) for row in rows]


def _siblings(conn, phone: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, amount, status FROM quotes WHERE customer_phone = ? AND status = 'open' ORDER BY id",
        (phone,),
    ).fetchall()
    return [dict(row) for row in rows]


def create_app(conn=None) -> FastAPI:
    app = FastAPI()
    app.state.conn = conn
    app.state.generate_draft = generate_draft

    def get_conn():
        if app.state.conn is None:
            connection = connect(_db_path())
            count = connection.execute("SELECT COUNT(*) AS n FROM quotes").fetchone()["n"]
            if count == 0:
                ingest_seed(connection, str(SEED_QUOTES), str(SEED_EVENTS))
            app.state.conn = connection
        return app.state.conn

    @app.get("/", response_class=HTMLResponse)
    def list_quotes(request: Request, now: str | None = None, flash: str | None = None):
        connection = get_conn()
        clock = _now(connection, now)
        rows = []
        ranked = recommend(connection, clock)
        for index, candidate in enumerate(ranked, start=1):
            quote = _quote_row(connection, candidate.quote_id)
            rows.append(
                {
                    "candidate": candidate,
                    "quote": quote,
                    "reason_label": explain_reason(candidate),
                    "rank": index,
                }
            )
        return TEMPLATES.TemplateResponse(
            request,
            "list.html",
            {
                "banner": BANNER,
                "now": clock,
                "now_label": format_clock(clock),
                "flash": flash,
                "rows": rows,
                "writer": _writer_label(),
            },
        )

    @app.get("/ranking", response_class=HTMLResponse)
    def ranking_page(request: Request, now: str | None = None):
        connection = get_conn()
        clock = _now(connection, now)
        return TEMPLATES.TemplateResponse(
            request,
            "ranking.html",
            {
                "banner": BANNER,
                "now": clock,
                "now_label": format_clock(clock),
                "flash": None,
                "writer": _writer_label(),
                "copy": ranking_copy(),
            },
        )

    @app.get("/quotes/{quote_id}", response_class=HTMLResponse)
    def quote_workspace(
        request: Request,
        quote_id: str,
        now: str | None = None,
        flash: str | None = None,
        draft: str | None = None,
    ):
        connection = get_conn()
        clock = _now(connection, now)
        quote = _quote_row(connection, quote_id)
        if quote is None:
            return RedirectResponse(_flash_url("/", clock, "Quote not found."), status_code=303)
        ranked = recommend(connection, clock)
        by_id = {c.quote_id: (i, c) for i, c in enumerate(ranked, start=1)}
        rank_pos, candidate = by_id.get(quote_id, (None, None))
        sendable = bool(candidate.sendable) if candidate else quote["status"] == "open"
        open_status = quote["status"] == "open"
        compose = None
        if draft:
            row = connection.execute("SELECT * FROM messages WHERE id = ?", (draft,)).fetchone()
            if row:
                compose = dict(row)
                compose["created_label"] = format_when(compose.get("created_at"), clock)
                compose["sent_label"] = format_when(compose.get("sent_at"), clock) if compose.get("sent_at") else ""
        drafts = _drafts_for_phone(connection, quote["customer_phone"])
        for item in drafts:
            item["created_label"] = format_when(item.get("created_at"), clock)
        why = ""
        if candidate and rank_pos:
            why = rank_blurb(candidate, rank_pos, len(ranked))
        return TEMPLATES.TemplateResponse(
            request,
            "quote.html",
            {
                "banner": BANNER,
                "now": clock,
                "now_label": format_clock(clock),
                "flash": flash,
                "quote": quote,
                "candidate": candidate,
                "reason_label": explain_reason(candidate) if candidate else "",
                "why_rank": why,
                "sendable": sendable and open_status,
                "open_status": open_status,
                "cooldown_copy": COOLDOWN_COPY,
                "writer": _writer_label(),
                "siblings": _siblings(connection, quote["customer_phone"]),
                "thread": _thread(connection, quote_id, quote["customer_phone"], clock),
                "drafts": drafts,
                "compose": compose,
            },
        )

    def _make_draft(connection, quote_id: str, clock: str, use_template: bool, on_chunk=None):
        quote = _quote_row(connection, quote_id)
        if quote is None or quote["status"] != "open":
            raise SendRejected("not_open")
        by_id = _candidates_by_id(connection, clock)
        reason = by_id[quote_id].reason if quote_id in by_id else "quiet_open"
        return app.state.generate_draft(
            connection,
            quote_id,
            clock,
            reason=reason,
            on_chunk=on_chunk,
            use_template=use_template,
        )

    @app.post("/quotes/{quote_id}/drafts")
    def create_draft(
        quote_id: str,
        now: str = Form(""),
        use_template: str = Form(""),
    ):
        connection = get_conn()
        clock = _now(connection, now or None)
        try:
            message = _make_draft(connection, quote_id, clock, use_template=bool(use_template))
        except DraftGenerationError as exc:
            return RedirectResponse(_flash_url(f"/quotes/{quote_id}", clock, str(exc)), status_code=303)
        except SendRejected as exc:
            return RedirectResponse(_flash_url(f"/quotes/{quote_id}", clock, exc.code), status_code=303)
        return RedirectResponse(
            _flash_url(f"/quotes/{quote_id}", clock, "Draft saved.", draft=message["id"]),
            status_code=303,
        )

    @app.get("/quotes/{quote_id}/draft-stream")
    def draft_stream(quote_id: str, now: str | None = None, use_template: str | None = None):
        connection = get_conn()
        clock = _now(connection, now)
        chunks: list[str] = []

        def events():
            try:
                message = _make_draft(
                    connection,
                    quote_id,
                    clock,
                    use_template=bool(use_template),
                    on_chunk=lambda text: chunks.append(text),
                )
                for text in chunks:
                    yield f"data: {json.dumps({'type': 'chunk', 'text': text})}\n\n"
                yield f"data: {json.dumps({'type': 'done', 'message_id': message['id']})}\n\n"
            except DraftGenerationError as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            except SendRejected as exc:
                yield f"data: {json.dumps({'type': 'error', 'message': exc.code})}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/messages/{message_id}/edit")
    def edit_message(message_id: str, body: str = Form(""), now: str = Form(""), quote_id: str = Form("")):
        connection = get_conn()
        clock = _now(connection, now or None)
        try:
            message = edit_draft(connection, message_id, body, clock)
        except SendRejected as exc:
            target = quote_id or ""
            return RedirectResponse(_flash_url(f"/quotes/{target}", clock, exc.code), status_code=303)
        target = quote_id or message["primary_quote_id"]
        return RedirectResponse(
            _flash_url(f"/quotes/{target}", clock, "Draft updated.", draft=message["id"]),
            status_code=303,
        )

    @app.post("/messages/{message_id}/send")
    def send_message(
        message_id: str,
        now: str = Form(""),
        confirmed: str = Form("false"),
        quote_id: str = Form(""),
    ):
        connection = get_conn()
        clock = _now(connection, now or None)
        ok = confirmed == "true"
        try:
            message = send_attempt(connection, message_id, confirmed=ok, now=clock)
        except SendRejected as exc:
            target = quote_id or ""
            copy = COOLDOWN_COPY if exc.code == "cooldown" else exc.code
            return RedirectResponse(_flash_url(f"/quotes/{target}", clock, copy, draft=message_id), status_code=303)
        target = quote_id or message["primary_quote_id"]
        if message["status"] != "sent":
            return RedirectResponse(
                _flash_url(f"/quotes/{target}", clock, "Send cancelled.", draft=message["id"]),
                status_code=303,
            )
        return RedirectResponse(
            _flash_url(f"/quotes/{target}", clock, "In the outbox", draft=message["id"]),
            status_code=303,
        )

    @app.post("/messages/{message_id}/discard")
    def discard_message(
        message_id: str,
        now: str = Form(""),
        confirmed: str = Form("false"),
        quote_id: str = Form(""),
    ):
        connection = get_conn()
        clock = _now(connection, now or None)
        ok = confirmed == "true"
        message = discard_attempt(connection, message_id, confirmed=ok, now=clock)
        target = quote_id or message["primary_quote_id"]
        return RedirectResponse(_flash_url(f"/quotes/{target}", clock, "Draft discarded."), status_code=303)

    return app


app = create_app()
