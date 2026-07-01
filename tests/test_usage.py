"""Daily token usage tracker: limit parsing, persistence, reset, and poller integration.

All offline and deterministic — no filesystem side-effects leak between tests.
"""
import json
import threading

import pytest

from quest_ai_runner.usage import DailyUsageLimits, DailyUsageTracker


# ---------------------------------------------------------------------------
# DailyUsageLimits.from_env
# ---------------------------------------------------------------------------

def test_limits_from_env_unset_uses_default():
    from quest_ai_runner.usage import DEFAULT_DAILY_TOKEN_LIMIT
    limits = DailyUsageLimits.from_env({})
    assert limits.enabled()
    assert limits.max_daily_tokens == DEFAULT_DAILY_TOKEN_LIMIT


def test_limits_from_env_parses_integer():
    limits = DailyUsageLimits.from_env({"QAR_DAILY_TOKEN_LIMIT": "500000"})
    assert limits.enabled()
    assert limits.max_daily_tokens == 500_000


def test_limits_from_env_disable_keywords():
    for val in ("0", "off", "none", "false", "disabled"):
        limits = DailyUsageLimits.from_env({"QAR_DAILY_TOKEN_LIMIT": val})
        assert not limits.enabled(), f"expected disabled for {val!r}"


def test_limits_from_env_garbage_value_falls_back_to_default(caplog):
    from quest_ai_runner.usage import DEFAULT_DAILY_TOKEN_LIMIT
    limits = DailyUsageLimits.from_env({"QAR_DAILY_TOKEN_LIMIT": "lots"})
    assert limits.enabled()
    assert limits.max_daily_tokens == DEFAULT_DAILY_TOKEN_LIMIT
    assert any("ignoring" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# DailyUsageTracker — in-memory (no file)
# ---------------------------------------------------------------------------

def _tracker(limit=1000, date_override=None):
    """Build an in-memory tracker with an optional forced date for testing day-rollover."""
    t = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=limit))
    if date_override:
        t._date = date_override
    return t


def test_tracker_starts_at_zero():
    t = _tracker()
    assert t.total_tokens() == 0
    assert not t.over_limit()


def test_tracker_records_tokens():
    t = _tracker(limit=1000)
    t.record(300, 100)
    assert t.total_tokens() == 400
    assert not t.over_limit()


def test_tracker_over_limit_when_reached():
    t = _tracker(limit=500)
    t.record(300, 200)
    assert t.total_tokens() == 500
    assert t.over_limit()


def test_tracker_over_limit_when_exceeded():
    t = _tracker(limit=500)
    t.record(600, 0)
    assert t.over_limit()


def test_tracker_no_limit_never_over():
    t = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=None))
    t.record(10_000_000, 5_000_000)
    assert not t.over_limit()


def test_tracker_resets_on_new_day(caplog):
    t = _tracker(limit=500)
    t.record(300, 200)          # day 1: at the limit
    assert t.over_limit()
    t._date = "2000-01-01"      # simulate a new day by forcing a stale date
    assert not t.over_limit()   # counter resets on the next check
    assert t.total_tokens() == 0


def test_tracker_status_string():
    t = _tracker(limit=2_000_000)
    t.record(400_000, 100_000)
    s = t.status()
    assert "500,000" in s
    assert "2,000,000" in s
    assert "25%" in s


def test_tracker_status_no_limit():
    t = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=None))
    t.record(100, 50)
    assert "no limit" in t.status()


def test_tracker_thread_safe():
    t = _tracker(limit=100_000)
    threads = [threading.Thread(target=t.record, args=(100, 50)) for _ in range(100)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert t.total_tokens() == 100 * 150


# ---------------------------------------------------------------------------
# DailyUsageTracker — file persistence
# ---------------------------------------------------------------------------

def test_tracker_persists_and_loads(tmp_path):
    path = str(tmp_path / "usage.json")
    t1 = DailyUsageTracker(path=path, limits=DailyUsageLimits(max_daily_tokens=5000))
    t1.record(1000, 500)
    assert (tmp_path / "usage.json").exists()

    t2 = DailyUsageTracker(path=path, limits=DailyUsageLimits(max_daily_tokens=5000))
    assert t2.total_tokens() == 1500


def test_tracker_resets_file_on_new_day(tmp_path):
    path = str(tmp_path / "usage.json")
    # Write a stale file (yesterday).
    (tmp_path / "usage.json").write_text(json.dumps({
        "date": "2000-01-01", "tokens_in": 999999, "tokens_out": 0
    }))
    t = DailyUsageTracker(path=path, limits=DailyUsageLimits(max_daily_tokens=5000))
    assert t.total_tokens() == 0  # stale date: started fresh


def test_tracker_corrupt_file_starts_fresh(tmp_path):
    path = str(tmp_path / "usage.json")
    (tmp_path / "usage.json").write_text("NOT JSON{{{{")
    t = DailyUsageTracker(path=path, limits=DailyUsageLimits(max_daily_tokens=5000))
    assert t.total_tokens() == 0


def test_tracker_failed_write_leaves_previous_file_intact(tmp_path, monkeypatch):
    """_save() writes via temp-file + os.replace(); if the temp write raises partway, the
    previously-saved usage file must be untouched, and a later, un-interrupted record() still
    round-trips normally."""
    from pathlib import Path as _Path

    path = str(tmp_path / "usage.json")
    t = DailyUsageTracker(path=path, limits=DailyUsageLimits(max_daily_tokens=5000))
    t.record(100, 50)
    good_content = (tmp_path / "usage.json").read_text()

    real_write_text = _Path.write_text
    calls = {"n": 0}

    def _flaky_write_text(self, *args, **kwargs):
        if self.suffix == ".tmp" and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("simulated disk-full mid-write")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(_Path, "write_text", _flaky_write_text)
    t.record(200, 0)  # this record's write fails partway

    # The on-disk file must still be exactly the last GOOD content (untouched).
    assert (tmp_path / "usage.json").read_text() == good_content

    monkeypatch.setattr(_Path, "write_text", real_write_text)

    # Normal roundtrip still works after the failure is past.
    t.record(50, 0)
    t2 = DailyUsageTracker(path=path, limits=DailyUsageLimits(max_daily_tokens=5000))
    assert t2.total_tokens() == t.total_tokens()


# ---------------------------------------------------------------------------
# MultiProvider — token recording and limit-reached responses
# ---------------------------------------------------------------------------

def test_multi_provider_records_tokens_to_tracker():
    from tests.conftest import StubProvider
    from quest_ai_runner.adapters.multi_provider import MultiProvider

    stub = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    # Give the stub provider trackable token fields.
    stub.tokens_in = 0
    stub.tokens_out = 0

    tracker = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=1_000_000))
    mp = MultiProvider(stub, {}, usage_tracker=tracker)

    # Simulate a plan call that consumes tokens.
    stub.tokens_in += 200
    stub.tokens_out += 50
    # Call plan through multi-provider (delta computed from before/after snapshot).
    # We directly exercise _record_token_delta here since StubProvider.plan() doesn't update fields.
    mp._record_token_delta(stub, before_in=0, before_out=0)
    assert tracker.total_tokens() == 250


def test_multi_provider_answer_returns_limit_message_when_over():
    from tests.conftest import StubProvider
    from quest_ai_runner.adapters.multi_provider import MultiProvider

    stub = StubProvider(decisions=[])
    tracker = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=100))
    tracker.record(100, 0)  # already at limit
    mp = MultiProvider(stub, {}, usage_tracker=tracker)

    reply = mp.answer([{"role": "user", "content": "hi"}], model="gemini-2.0-flash")
    assert "Daily token limit reached" in reply
    assert "QAR_DAILY_TOKEN_LIMIT" in reply


def test_multi_provider_plan_returns_answer_action_when_over():
    from tests.conftest import StubProvider
    from quest_ai_runner.adapters.multi_provider import MultiProvider

    stub = StubProvider(decisions=[])
    tracker = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=100))
    tracker.record(200, 0)  # over limit
    mp = MultiProvider(stub, {}, usage_tracker=tracker)

    decision = mp.plan("do something", model="gemini-2.0-flash", tool_schema={})
    assert decision.get("action") == "answer"


# ---------------------------------------------------------------------------
# Poller integration — daily limit pauses pickup (mirrors test_resources.py style)
# ---------------------------------------------------------------------------

def _poller_with_tracker(client, provider, tracker):
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller
    from quest_ai_runner.resources import ResourceGuard, ResourceLimits
    from tests.conftest import StubRetrieval

    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=provider,
        usage_tracker=tracker,
    )
    guard = ResourceGuard(ResourceLimits())  # resource guard disabled (no limits)
    return Poller(cfg, state_path=None, client=client, resource_guard=guard)


def test_poller_pauses_when_daily_limit_exceeded():
    from tests.test_runner import MockQuestClient
    from tests.conftest import StubProvider

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient(
        [{"id": "task-1", "text": "do it", "status": "queued", "team_id": "team1"}])

    # Tracker already over limit.
    tracker = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=100))
    tracker.record(200, 0)

    poller = _poller_with_tracker(client, provider, tracker)
    result = poller.run_once()
    assert result == []          # nothing claimed
    assert client.claimed == []


def test_poller_runs_when_daily_limit_not_reached():
    from tests.test_runner import MockQuestClient
    from tests.conftest import StubProvider

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient(
        [{"id": "task-2", "text": "do it", "status": "queued", "team_id": "team1"}])

    tracker = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=1_000_000))
    tracker.record(100, 50)  # well under the limit

    poller = _poller_with_tracker(client, provider, tracker)
    result = poller.run_once()
    assert result == ["task-2"]


def test_poller_resumes_after_day_rollover():
    from tests.test_runner import MockQuestClient
    from tests.conftest import StubProvider

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"},
                                       {"action": "answer", "rationale": "ok"}])
    client = MockQuestClient(
        [{"id": "task-3", "text": "do it", "status": "queued", "team_id": "team1"}])

    tracker = DailyUsageTracker(path=None, limits=DailyUsageLimits(max_daily_tokens=100))
    tracker.record(200, 0)  # over limit today

    poller = _poller_with_tracker(client, provider, tracker)
    assert poller.run_once() == []       # paused

    # Simulate midnight rollover.
    tracker._date = "2000-01-01"
    assert poller.run_once() == ["task-3"]   # resumed after reset


def test_poller_default_limit_does_not_block_normal_scan():
    """Default-on tracker with 0 tokens used must not block a normal scan."""
    from tests.test_runner import MockQuestClient
    from tests.conftest import StubProvider
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller
    from quest_ai_runner.resources import ResourceGuard, ResourceLimits
    from tests.conftest import StubRetrieval

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient(
        [{"id": "task-4", "text": "do it", "status": "queued", "team_id": "team1"}])
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=provider,
    )
    poller = Poller(cfg, state_path=None, client=client,
                    resource_guard=ResourceGuard(ResourceLimits()))
    # Tracker auto-created with default limit; 0 tokens used -> not over limit.
    assert poller._usage_tracker is not None
    assert not poller._usage_tracker.over_limit()
    assert poller.run_once() == ["task-4"]
