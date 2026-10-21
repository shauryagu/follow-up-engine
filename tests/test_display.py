from robby.display import explain_reason, format_clock, format_when
from robby.policy import Candidate, HOT_VIEW_HOURS


def test_format_clock_drops_iso_compact():
    assert format_clock("2026-08-03T03:00:00Z") == "Aug 3, 2026, 3:00 AM UTC"
    assert "T03:00:00Z" not in format_clock("2026-08-03T03:00:00Z")


def test_format_when_includes_relative_to_now():
    text = format_when("2026-08-09T12:00:00Z", "2026-08-10T00:00:00Z")
    assert "Aug 9, 2026" in text
    assert "12 hours ago" in text


def test_hot_view_explain_uses_count_and_window():
    candidate = Candidate(
        quote_id="Q-1003",
        customer_phone="+19175552003",
        score=1.0,
        reason="hot_view",
        amount=850,
        sendable=True,
        view_count=3,
        views_in_hot_window=2,
        days_since_contact=1.5,
        last_view="2026-08-09T12:00:00Z",
        last_reply=None,
        last_outbound=None,
    )
    line = explain_reason(candidate)
    assert "2 times" in line
    assert str(HOT_VIEW_HOURS) in line
    assert "viewed" in line.lower()
