from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from robby.attempts import COOLDOWN_HOURS

HOT_VIEW_HOURS = 48
URGENCY_REPLY = 1.0
URGENCY_HOT_VIEW = 0.6
URGENCY_QUIET = 0.25
NEGLECT_DAYS = 7
VIEW_COUNT_EPS = 0.02

_URGENCY = {
    "unanswered_reply": URGENCY_REPLY,
    "hot_view": URGENCY_HOT_VIEW,
    "quiet_open": URGENCY_QUIET,
}


@dataclass(frozen=True)
class Candidate:
    quote_id: str
    customer_phone: str
    score: float
    reason: str
    amount: float
    sendable: bool
    view_count: int = 0
    views_in_hot_window: int = 0
    days_since_contact: float = 0.0
    last_view: str | None = None
    last_reply: str | None = None
    last_outbound: str | None = None


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def default_now(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT MAX(timestamp) FROM events").fetchone()
    ts = row[0] if row is not None else None
    if ts is None:
        raise ValueError("no events")
    return ts


def _last_of(timestamps: list[str]) -> str | None:
    return max(timestamps) if timestamps else None


def _reason(last_reply: str | None, last_view: str | None, last_outbound: str | None, now_dt: datetime) -> str:
    if last_reply is not None and (last_outbound is None or last_reply > last_outbound):
        return "unanswered_reply"
    if last_view is not None:
        within_hot = now_dt - _parse(last_view) <= timedelta(hours=HOT_VIEW_HOURS)
        after_outbound = last_outbound is None or last_view > last_outbound
        if within_hot and after_outbound:
            return "hot_view"
    return "quiet_open"


def _last_contact_dt(
    last_contact_at: str | None,
    last_outbound: str | None,
    created_at: str,
    now_dt: datetime,
) -> datetime:
    times = []
    if last_contact_at:
        times.append(_parse(last_contact_at))
    if last_outbound:
        times.append(_parse(last_outbound))
    if not times:
        return _parse(created_at)
    return min(max(times), now_dt)


def _phones_on_cooldown(conn: sqlite3.Connection, now_dt: datetime) -> set[str]:
    window = timedelta(hours=COOLDOWN_HOURS)
    rows = conn.execute(
        "SELECT customer_phone, sent_at FROM messages WHERE status = 'sent' AND sent_at IS NOT NULL"
    ).fetchall()
    cooled: set[str] = set()
    for row in rows:
        delta = now_dt - _parse(row["sent_at"])
        if timedelta(0) <= delta < window:
            cooled.add(row["customer_phone"])
    return cooled


def recommend(conn: sqlite3.Connection, now: str) -> list[Candidate]:
    now_dt = _parse(now)
    quotes = conn.execute(
        """
        SELECT id, customer_phone, amount, created_at, last_contact_at
        FROM quotes
        WHERE status = 'open'
        """
    ).fetchall()

    events_by_quote: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        "SELECT quote_id, type, timestamp FROM events WHERE timestamp <= ?",
        (now,),
    ):
        events_by_quote[row["quote_id"]].append(row)

    outbound_from_messages: dict[str, str] = {}
    for row in conn.execute(
        """
        SELECT mq.quote_id, m.sent_at
        FROM messages m
        JOIN message_quotes mq ON mq.message_id = m.id
        WHERE m.status = 'sent' AND m.sent_at IS NOT NULL AND m.sent_at <= ?
        """,
        (now,),
    ):
        prev = outbound_from_messages.get(row["quote_id"])
        if prev is None or row["sent_at"] > prev:
            outbound_from_messages[row["quote_id"]] = row["sent_at"]

    cooled = _phones_on_cooldown(conn, now_dt)
    candidates: list[Candidate] = []
    for quote in quotes:
        qid = quote["id"]
        events = events_by_quote[qid]
        last_reply = _last_of([e["timestamp"] for e in events if e["type"] == "customer_replied"])
        last_view = _last_of([e["timestamp"] for e in events if e["type"] == "quote_viewed"])
        last_sent_event = _last_of([e["timestamp"] for e in events if e["type"] == "message_sent"])
        outbound_times = [ts for ts in (last_sent_event, outbound_from_messages.get(qid)) if ts]
        last_outbound = _last_of(outbound_times)
        view_count = sum(1 for e in events if e["type"] == "quote_viewed")
        hot_cutoff = now_dt - timedelta(hours=HOT_VIEW_HOURS)
        views_in_hot_window = sum(
            1
            for e in events
            if e["type"] == "quote_viewed" and _parse(e["timestamp"]) >= hot_cutoff
        )
        reason = _reason(last_reply, last_view, last_outbound, now_dt)
        last_contact_dt = _last_contact_dt(
            quote["last_contact_at"],
            last_outbound,
            quote["created_at"],
            now_dt,
        )
        days_since_contact = (now_dt - last_contact_dt).total_seconds() / 86400.0
        amount = float(quote["amount"])
        score = (
            math.log1p(amount)
            * _URGENCY[reason]
            * (1 + days_since_contact / NEGLECT_DAYS)
            * (1 + VIEW_COUNT_EPS * math.log1p(view_count))
        )
        candidates.append(
            Candidate(
                quote_id=qid,
                customer_phone=quote["customer_phone"],
                score=score,
                reason=reason,
                amount=amount,
                sendable=quote["customer_phone"] not in cooled,
                view_count=view_count,
                views_in_hot_window=views_in_hot_window,
                days_since_contact=days_since_contact,
                last_view=last_view,
                last_reply=last_reply,
                last_outbound=last_outbound,
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.quote_id))
    return candidates
