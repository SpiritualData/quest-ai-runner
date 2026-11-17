"""The autopilot's prior-task read is bounded by a lookback window (a full-history read was ~7 MB a call on 2026-11-16)."""
from datetime import datetime, timezone

from quest_ai_runner.intelligence.proactivity.autopilot_periods import previous_period_bounds, task_lookback_start

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def test_week_scope_looks_back_before_previous_week_start_only_a_little():
    start = datetime.fromisoformat(task_lookback_start("week", NOW))
    window_start, _ = previous_period_bounds("week", NOW)
    assert start < window_start                      # a task created before the period but finished in it is still read
    assert (window_start - start).days <= 30         # and the window is far shorter than the full history


def test_unscoped_quest_gets_a_default_window():
    start = datetime.fromisoformat(task_lookback_start("", NOW))
    assert 7 <= (NOW - start).days <= 45


def test_year_scope_reaches_the_whole_previous_year():
    start = datetime.fromisoformat(task_lookback_start("year", NOW))
    assert start <= previous_period_bounds("year", NOW)[0]


def test_full_scope_label_is_accepted():
    assert task_lookback_start("week:2026_W47", NOW) == task_lookback_start("week", NOW)
