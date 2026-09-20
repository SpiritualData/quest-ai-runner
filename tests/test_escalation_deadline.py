"""Escalation gains an optional deadline + kind, parsed from the planner's own words.

``parse_deadline`` is the single place a planner-supplied string ("in 48h", an ISO datetime, or
garbage) becomes a real datetime (or None) before it reaches an EscalationSink. Also covers the
default (concrete, non-abstract) ``EscalationSinkBase.open_decision_ids_for_quest`` -- the optional
capability an orphan-decision recovery probe uses (see test_orchestrator_decision_recovery.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from quest_ai_runner.core.adapters import Escalation, EscalationSinkBase, parse_deadline

NOW = datetime(2026, 9, 19, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_deadline_none_and_blank_give_no_deadline():
    assert parse_deadline(None) is None
    assert parse_deadline("") is None
    assert parse_deadline("   ") is None


def test_parse_deadline_relative_hours():
    assert parse_deadline("in 48h", now=NOW) == NOW + timedelta(hours=48)
    assert parse_deadline("in 2 hours", now=NOW) == NOW + timedelta(hours=2)
    assert parse_deadline("IN 1HR", now=NOW) == NOW + timedelta(hours=1)


def test_parse_deadline_relative_days_and_minutes():
    assert parse_deadline("in 2 days", now=NOW) == NOW + timedelta(days=2)
    assert parse_deadline("in 3d", now=NOW) == NOW + timedelta(days=3)
    assert parse_deadline("in 30m", now=NOW) == NOW + timedelta(minutes=30)
    assert parse_deadline("in 45 minutes", now=NOW) == NOW + timedelta(minutes=45)


def test_parse_deadline_iso_datetime_with_and_without_offset():
    assert parse_deadline("2026-09-25T18:00:00+00:00") == datetime(
        2026, 9, 25, 18, 0, 0, tzinfo=timezone.utc)
    assert parse_deadline("2026-09-25T18:00:00Z") == datetime(
        2026, 9, 25, 18, 0, 0, tzinfo=timezone.utc)
    # No offset at all: treated as UTC rather than rejected.
    assert parse_deadline("2026-09-25T18:00:00") == datetime(
        2026, 9, 25, 18, 0, 0, tzinfo=timezone.utc)


def test_parse_deadline_unparseable_degrades_to_none():
    assert parse_deadline("sometime next week") is None
    assert parse_deadline("in a bit") is None
    assert parse_deadline("not-a-date") is None


def test_escalation_defaults_to_no_deadline():
    esc = Escalation(summary="Approve this?")
    assert esc.deadline is None
    assert esc.kind == "approve"


def test_escalation_sink_base_default_has_no_recovery_capability():
    class MinimalSink(EscalationSinkBase):
        def escalate(self, escalation: Escalation) -> str:
            return "dec_1"

    sink = MinimalSink()
    # Concrete default: every existing subclass keeps working without implementing this.
    assert sink.open_decision_ids_for_quest("quest_1") == frozenset()
