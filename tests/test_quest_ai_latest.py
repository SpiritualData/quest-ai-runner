"""The quest-ai-latest entry point: due by the quest's activity, a past revisit date or a fully answered card; the minimum gap; the order of what to look into."""
from datetime import datetime, timezone

from quest_ai_runner.api import QUEST_AI_LATEST_KIND_ORDER, quest_ai_latest_next_look as next_look

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
LAST = "2026-11-01T00:00:00Z"


def look(**kw):
    return next_look(now=NOW, **kw)


def test_never_looked_and_nothing_happened_is_not_due():
    assert look()["due"] is False


def test_activity_after_the_last_look_is_due_once_the_gap_has_passed():
    found = look(last_look_at=LAST, activity=[{"at": "2026-11-05T00:00:00Z", "kind": "note"}])
    assert found["due"] and found["trigger"] == "event"


def test_activity_inside_the_gap_waits():
    assert look(last_look_at="2026-11-09T00:00:00Z", activity=[{"at": "2026-11-09T06:00:00Z", "kind": "note"}], min_gap_days=3)["due"] is False


def test_activity_before_the_last_look_is_not_due():
    assert look(last_look_at=LAST, activity=[{"at": "2026-10-20T00:00:00Z", "kind": "note"}])["due"] is False


def test_an_unreadable_activity_time_never_makes_a_quest_due():
    assert look(last_look_at=LAST, activity=[{"at": "yesterday-ish", "kind": "note"}, {"kind": "note"}])["due"] is False


def test_a_past_revisit_date_is_due_inside_the_gap():
    found = look(last_look_at="2026-11-09T00:00:00Z", revisit_dates=["2026-11-09T12:00:00Z"])
    assert found["due"] and found["trigger"] == "revisit"


def test_every_finding_acted_on_is_due_inside_the_gap():
    found = look(last_look_at="2026-11-09T00:00:00Z", acted_on_at="2026-11-09T08:00:00Z")
    assert found["due"] and found["trigger"] == "acted_on"


def test_switching_the_gap_off_makes_activity_due_at_once():
    assert look(last_look_at="2026-11-09T00:00:00Z", activity=[{"at": "2026-11-09T06:00:00Z", "kind": "habit"}], min_gap_days=0)["due"]


def test_what_to_look_into_follows_the_kind_order_then_the_deadline():
    found = look(last_look_at=LAST, activity=[{"at": "2026-11-05T00:00:00Z", "kind": "note"}], candidates=[
        {"id": "q", "kind": "plan_question"}, {"id": "h", "kind": "check_habit"}, {"id": "r2", "kind": "refresh", "deadline": "2026-11-08"}, {"id": "r1", "kind": "refresh", "deadline": "2026-11-02"}])
    assert QUEST_AI_LATEST_KIND_ORDER[0] == "refresh"
    assert found["order"] == ["r1", "r2", "h", "q"]


def test_nothing_is_ordered_when_nothing_is_due():
    assert look(last_look_at=LAST, candidates=[{"id": "q", "kind": "plan_question"}])["order"] == []


def test_the_evidence_line_names_what_a_finding_rests_on_and_never_a_verdict():
    from quest_ai_runner.api import evidence_line
    assert evidence_line(["research"]) == "1 study"
    assert evidence_line(["guide", "guide", "guide"]) == "3 guides agree"
    assert evidence_line(["personal"]) == "1 person's account"
    assert evidence_line(["vendor"]) == "a company page"
    assert evidence_line(["research", "guide", "guide"], True) == "1 study and 2 guides agree, read in full"
    assert evidence_line([]) == ""
    assert not any(word in evidence_line(["personal"]) for word in ("sure", "weak", "confident"))
