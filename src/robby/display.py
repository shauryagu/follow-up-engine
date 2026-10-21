from __future__ import annotations

from datetime import datetime

from robby.policy import HOT_VIEW_HOURS, NEGLECT_DAYS, Candidate


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def format_clock(ts: str) -> str:
    dt = _parse(ts)
    hour = dt.strftime("%I").lstrip("0") or "12"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour}:{dt.strftime('%M')} {dt.strftime('%p')} UTC"


def format_when(ts: str | None, now: str) -> str:
    if not ts:
        return ""
    clock = format_clock(ts)
    delta = _parse(now) - _parse(ts)
    secs = delta.total_seconds()
    if abs(secs) < 90:
        rel = "just now"
    elif secs > 0:
        hours = secs / 3600.0
        if hours < 48:
            count = max(1, round(hours))
            rel = f"{count} hour{'s' if count != 1 else ''} ago"
        else:
            count = max(1, round(secs / 86400.0))
            rel = f"{count} day{'s' if count != 1 else ''} ago"
    else:
        rel = "after now"
    return f"{clock} · {rel}"


def explain_reason(candidate: Candidate) -> str:
    if candidate.reason == "hot_view":
        n = candidate.views_in_hot_window or candidate.view_count
        times = "once" if n == 1 else f"{n} times"
        return f"Viewed {times} in the last {HOT_VIEW_HOURS} hours"
    if candidate.reason == "unanswered_reply":
        return "They replied — waiting on us"
    days = int(candidate.days_since_contact)
    if days >= 1:
        unit = "day" if days == 1 else "days"
        return f"Open, no recent engagement · {days} {unit} since last contact"
    return "Open, no recent engagement"


def rank_blurb(candidate: Candidate, position: int, total: int) -> str:
    amount = f"${candidate.amount:,.0f}"
    days = max(0, round(candidate.days_since_contact))
    wait = f"{days} day{'s' if days != 1 else ''} since last contact"
    return (
        f"Ranked #{position} of {total}. {explain_reason(candidate)}. "
        f"{amount} ticket · {wait}."
    )


def ranking_copy() -> dict[str, str]:
    return {
        "title": "How ranking works",
        "lede": "We rank by value at risk: the quote that would hurt most to miss sits at the top. Size, urgency, and how long it has sat all matter. The reason is a label, not a separate queue.",
        "size": "Larger open tickets rank higher, with diminishing returns so a $12,500 job outranks a $400 job without drowning everything else.",
        "urgency": "A homeowner who replied and is waiting on us outranks a recent view, which outranks a quiet open quote.",
        "views": f"A quote viewed in the last {HOT_VIEW_HOURS} hours is treated as hot. More views in that window nudge it up.",
        "neglect": f"Each week without contact raises the quote. After {NEGLECT_DAYS} quiet days the neglect factor has doubled.",
        "filters": "Accepted and dismissed quotes never appear. A send starts a 48-hour cooldown on that phone: you can still draft, but you cannot send again until the window ends.",
    }
