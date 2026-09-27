"""The Claude subscription usage limit is a WAIT, not a failure.

Before ``core/usage_limit.py``, a keyless (claude_cli) lane that ran out of allowance read Claude
Code's refusal ("You've hit your weekly limit · resets 1pm (America/Los_Angeles)") as ordinary run
output: deep runs reported a failed goal, planner calls swallowed it, and the lane kept claiming
work, marking its whole queue failed in seconds. These tests pin the replacement:

* recognising the refusal in each wording Claude Code uses, and reading when it resets
  (weekly with and without a date, session, the monthly-spend variant, the legacy epoch form,
  a relative "resets in"), and backing off when no reset can be read;
* the structured signal in the run's own session record (``quotaLimits.resetsAt``);
* the lane-wide note (record / active / clear / persisted across a restart);
* the provider raising a typed ``UsageLimitError`` and not spawning while the lane is paused;
* the deep runner returning ``usage_limited`` with the session kept for the resume;
* the executor putting the task back in the queue (never done/failed) with the session to resume,
  and never pausing a run that genuinely succeeded;
* the poller's lane pause holding new work without claiming it, and letting a task its owner
  released ("Start now") through.

Offline: no real ``claude`` binary, no network. Messages below are copied from real transcripts.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

from quest_ai_runner.core import usage_limit
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.orchestrator import OrchestratorResult

WEEKLY_DATED = "You've hit your weekly limit · resets Sep 26, 1pm (America/Los_Angeles)"
WEEKLY = "You've hit your weekly limit · resets 1pm (America/Los_Angeles)"
SESSION = "You've hit your session limit · resets 8:30am (America/Los_Angeles)"
SPEND = ("You've hit your monthly spend limit · raise it at claude.ai/settings/usage?from=cc_cli_"
         "limit_message · your weekly limit resets 1pm (America/Los_Angeles)")

# 2026-09-25 16:05 UTC = 09:05 in Los Angeles (PDT, UTC-7).
NOW = datetime(2026, 9, 25, 16, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fresh_note():
    usage_limit.reset_for_tests()
    yield
    usage_limit.reset_for_tests()


# --- recognising it, and when it resets ----------------------------------------------------------

def test_weekly_with_a_date():
    found = usage_limit.detect_usage_limit(WEEKLY_DATED, now=NOW)
    assert found and found.kind == "weekly" and found.label() == "weekly limit"
    assert found.resets_at == datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)   # 1pm PDT


def test_weekly_clock_time_is_the_next_one():
    found = usage_limit.detect_usage_limit(WEEKLY, now=NOW)
    assert found.resets_at == datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)    # today 1pm
    later = datetime(2026, 9, 25, 21, 0, tzinfo=timezone.utc)                      # 2pm PDT
    assert usage_limit.detect_usage_limit(WEEKLY, now=later).resets_at == \
        datetime(2026, 9, 26, 20, 0, tzinfo=timezone.utc)                           # tomorrow


def test_session_limit_with_minutes():
    found = usage_limit.detect_usage_limit(SESSION, now=NOW)
    assert found.kind == "session"
    # 8:30am PDT already passed at 9:05, so the next one.
    assert found.resets_at == datetime(2026, 9, 26, 15, 30, tzinfo=timezone.utc)


def test_monthly_spend_reads_the_weekly_reset():
    found = usage_limit.detect_usage_limit(SPEND, now=NOW)
    assert found.kind == "monthly spend"
    assert found.resets_at == datetime(2026, 9, 25, 20, 0, tzinfo=timezone.utc)


def test_legacy_epoch_and_relative_forms():
    legacy = usage_limit.detect_usage_limit("Claude AI usage limit reached|1790452800", now=NOW)
    assert legacy.resets_at == datetime.fromtimestamp(1790452800, tz=timezone.utc)
    rel = usage_limit.detect_usage_limit("5-hour limit reached · resets in 2h 30m", now=NOW)
    assert rel.kind == "5-hour" and rel.resets_at == NOW + timedelta(hours=2, minutes=30)


def test_unparseable_reset_backs_off_and_caps():
    found = usage_limit.detect_usage_limit("You've hit your usage limit", now=NOW)
    assert found is not None and found.resets_at is None
    assert found.resume_at(hit_count=1, now=NOW) == NOW + timedelta(minutes=30)
    assert found.resume_at(hit_count=2, now=NOW) == NOW + timedelta(minutes=60)
    assert found.resume_at(hit_count=9, now=NOW) == NOW + timedelta(hours=3)


def test_known_reset_resumes_a_couple_of_minutes_after_it():
    found = usage_limit.detect_usage_limit(WEEKLY, now=NOW)
    assert found.resume_at(now=NOW) == datetime(2026, 9, 25, 20, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize("text", [
    "", "The API rate limit is 60/min, we should add a cache", "increase the limit to 5",
    "Error: 404 model not found", "Failed to authenticate: OAuth session expired",
])
def test_other_errors_are_not_a_usage_limit(text):
    assert usage_limit.detect_usage_limit(text, now=NOW) is None


def test_session_record_gives_the_exact_reset(tmp_path):
    record = {
        "type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
        "quotaLimits": {"status": "rejected", "resetsAt": 1790091000, "rateLimitType": "five_hour"},
        "message": {"role": "assistant", "content": [{"type": "text", "text": SESSION}]},
    }
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n"
                    + json.dumps(record) + "\n")
    found = usage_limit.limit_from_session_file(path)
    assert found.kind == "session"
    assert found.resets_at == datetime.fromtimestamp(1790091000, tz=timezone.utc)

    ordinary = tmp_path / "ok.jsonl"
    ordinary.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "done"}]}}) + "\n")
    assert usage_limit.limit_from_session_file(ordinary) is None


# --- the lane-wide note --------------------------------------------------------------------------

def test_note_is_active_until_the_reset_and_survives_a_restart(tmp_path):
    path = tmp_path / "state_usage_limit.json"
    usage_limit.configure_persistence(str(path))
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    usage_limit.record(usage_limit.UsageLimit(message=WEEKLY, kind="weekly", resets_at=soon))
    assert usage_limit.active_limit() is not None
    assert usage_limit.pause_until() == soon + usage_limit.RESUME_GRACE

    # A restarted lane reads it back.
    usage_limit.reset_for_tests()
    assert usage_limit.active_limit() is None
    usage_limit.configure_persistence(str(path))
    assert usage_limit.active_limit().kind == "weekly"

    # After the reset it is no longer in force; clear() lifts it early.
    assert usage_limit.active_limit(now=soon + timedelta(minutes=5)) is None
    usage_limit.clear("test")
    assert usage_limit.active_limit() is None and not path.exists()


def test_seen_since_only_reports_limits_from_this_run():
    started = time.time()
    assert usage_limit.seen_since(started) is None
    usage_limit.record(usage_limit.UsageLimit(message=WEEKLY))
    assert usage_limit.seen_since(started) is not None
    assert usage_limit.seen_since(time.time() + 10) is None


# --- the provider --------------------------------------------------------------------------------

def test_provider_raises_typed_limit_and_then_stops_spawning(monkeypatch):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class P:
            returncode = 1
            stdout = json.dumps({"is_error": True, "result": WEEKLY}).encode()
            stderr = b""
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = ClaudeCliProvider()
    with pytest.raises(usage_limit.UsageLimitError) as err:
        provider._invoke("hi", model="haiku")
    assert err.value.limit.kind == "weekly"
    assert isinstance(err.value, RuntimeError)          # every old catch still catches it
    spawned = len(calls)

    # While the lane is paused, no CLI is spawned at all, and the planner does not swallow it.
    with pytest.raises(usage_limit.UsageLimitError):
        provider.plan("plan", model="haiku", tool_schema={})
    assert len(calls) == spawned


# --- the deep runner -----------------------------------------------------------------------------

def fake_claude(tmp_path, envelope: dict, code: int) -> str:
    out = tmp_path / "envelope.json"
    out.write_text(json.dumps(envelope))
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat > /dev/null\ncat '{out}'\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_deep_run_refused_on_the_limit_keeps_its_session(tmp_path):
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    binary = fake_claude(tmp_path, {"type": "result", "is_error": True, "result": WEEKLY}, 1)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=binary))
    res = runner.run_goal(goal="do the thing", brief="do the thing", resume_session_id="sess-1")
    assert res.usage_limited and not res.met
    assert res.session_id == "sess-1"
    assert usage_limit.active_limit() is not None

    # While paused, a second deep run does not spawn anything and says the same.
    res2 = runner.run_goal(goal="another", brief="another")
    assert res2.usage_limited


def test_an_ordinary_failure_is_not_a_usage_limit(tmp_path):
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner

    binary = fake_claude(tmp_path, {"type": "result", "is_error": True,
                                    "result": "API Error: 500 internal"}, 1)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=binary))
    res = runner.run_goal(goal="do the thing", brief="do the thing")
    assert not res.usage_limited and not res.met
    assert usage_limit.active_limit() is None


# --- the executor: requeue, never done/failed ----------------------------------------------------

class RecordingClient:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            if name == "requeue_for_usage_limit":
                return {"task_id": args[0], "status": "queued"}
            if name in ("get_task",):
                return {"status": "in_progress"}
            return {}
        return record

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


class FixedOrchestrator:
    def __init__(self, result=None, raises=None, records=None):
        self.result, self.raises, self.records = result, raises, records

    def run(self, *args, **kwargs):
        if self.records is not None:
            usage_limit.record(self.records)
        if self.raises is not None:
            raise self.raises
        return self.result


def execute(orch, task=None):
    from quest_ai_runner.runner.executor import TaskExecutor

    client = RecordingClient()
    executor = TaskExecutor(client, orch)
    outcome = executor.execute(task or {"task_id": "atask_1", "text": "write the report",
                                        "resume_session_id": "old-sess"})
    return outcome, client


def test_limited_deep_run_is_requeued_with_its_session():
    reset = datetime.now(timezone.utc) + timedelta(hours=2)
    usage_limit.record(usage_limit.UsageLimit(message=WEEKLY, kind="weekly", resets_at=reset))
    usage_limit.reset_for_tests()    # the executor must not depend on a pre-existing note
    result = OrchestratorResult(kind="deep", deep_results=[DeepResult(
        met=False, error="Claude Code usage limit: " + WEEKLY, session_id="sess-9",
        usage_limited=True)])
    outcome, client = execute(FixedOrchestrator(result, records=usage_limit.UsageLimit(
        message=WEEKLY, kind="weekly", resets_at=reset)))
    assert outcome.status == "waiting"
    (_, args, kwargs), = client.named("requeue_for_usage_limit")
    assert args == ("atask_1",)
    assert kwargs["start_at"] == reset + usage_limit.RESUME_GRACE
    assert kwargs["resume_session_id"] == "sess-9"
    assert not client.named("report_failed") and not client.named("report_done")


def test_limit_raised_from_a_planner_call_is_requeued_keeping_the_old_session():
    limit = usage_limit.UsageLimit(message=SESSION, kind="session")
    outcome, client = execute(FixedOrchestrator(raises=usage_limit.UsageLimitError(limit)))
    assert outcome.status == "waiting"
    (_, _, kwargs), = client.named("requeue_for_usage_limit")
    assert kwargs["resume_session_id"] == "old-sess"
    # No reset time: backed off from now, not left unscheduled.
    assert kwargs["start_at"] > datetime.now(timezone.utc) + timedelta(minutes=25)
    assert not client.named("report_failed")


def test_a_run_that_succeeded_is_reported_even_if_a_limit_appeared_meanwhile():
    result = OrchestratorResult(kind="deep", deep_results=[DeepResult(met=True, output="All done.")])
    outcome, client = execute(FixedOrchestrator(result, records=usage_limit.UsageLimit(message=WEEKLY)))
    assert outcome.status == "done"
    assert not client.named("requeue_for_usage_limit")


def test_an_ordinary_failure_is_still_reported_failed():
    result = OrchestratorResult(kind="deep", deep_results=[DeepResult(met=False, error="boom")])
    outcome, client = execute(FixedOrchestrator(result))
    assert outcome.status == "failed"
    assert not client.named("requeue_for_usage_limit")


def test_a_failed_requeue_write_is_reported_failed_not_left_in_progress():
    from quest_ai_runner.runner.executor import TaskExecutor

    class NoRequeue(RecordingClient):
        def requeue_for_usage_limit(self, *a, **k):
            return None

    client = NoRequeue()
    outcome = TaskExecutor(client, FixedOrchestrator(
        raises=usage_limit.UsageLimitError(usage_limit.UsageLimit(message=WEEKLY)))).execute(
        {"task_id": "atask_2", "text": "x"})
    assert outcome.status == "failed"
    assert client.named("report_failed")


# --- the poller's lane pause ---------------------------------------------------------------------

class PauseClient:
    configured = True

    def __init__(self):
        self.holds, self.claims = [], []

    def hold_for_usage_limit(self, task_id, *, start_at, detail):
        self.holds.append((task_id, start_at, detail))
        return {"task_id": task_id}

    def claim(self, task_id, handler=None):
        self.claims.append(task_id)
        return None      # stop right after the claim decision


def pause_poller(tmp_path, client):
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller
    from tests.conftest import StubProvider, StubRetrieval

    cfg = RunnerConfig(quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
                       retrieval=StubRetrieval({}), model_provider=StubProvider(decisions=[]))
    return Poller(cfg, state_path=str(tmp_path / "state.json"), client=client)


def test_lane_pause_holds_new_work_without_claiming_it(tmp_path):
    client = PauseClient()
    poller = pause_poller(tmp_path, client)
    reset = datetime.now(timezone.utc) + timedelta(hours=3)
    usage_limit.record(usage_limit.UsageLimit(message=WEEKLY, kind="weekly", resets_at=reset))

    assert poller._handle_one({"task_id": "atask_new", "text": "x", "status": "queued"}) is None
    assert client.claims == []
    assert client.holds == [("atask_new", reset + usage_limit.RESUME_GRACE, WEEKLY)]
    # The note was persisted beside the lane's state file.
    assert (tmp_path / "state_usage_limit.json").exists()


def test_a_task_released_by_its_owner_goes_through_the_pause(tmp_path):
    client = PauseClient()
    poller = pause_poller(tmp_path, client)
    usage_limit.record(usage_limit.UsageLimit(
        message=WEEKLY, resets_at=datetime.now(timezone.utc) + timedelta(hours=3)))
    released = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat()
    poller._handle_one({"task_id": "atask_now", "text": "x", "status": "queued",
                        "start_requested_at": released})
    assert client.holds == []
    assert client.claims == ["atask_now"]
    assert usage_limit.active_limit() is None     # lifted, so the run really tries Claude Code


def test_start_at_tasks_are_due_whatever_the_runner_clock(tmp_path):
    from quest_ai_runner.runner.poller import _due_now_locally

    task = {"task_id": "t", "scheduled_date": "2999-01-01", "scheduled_time": "09:00",
            "start_at": "2026-09-25T16:00:00Z"}
    due, deferred = _due_now_locally([task])
    assert due == [task] and deferred == []
