"""Resource guard: overload detection, hysteresis, env parsing, and graceful poller pickup-pause.

All offline and deterministic — the guard takes an injected sampler, so no test depends on the
actual host's memory or load.
"""
from quest_ai_runner.resources import (
    ResourceGuard,
    ResourceLimits,
    ResourceSnapshot,
    sample_resources,
)

from .conftest import StubProvider, StubRetrieval


def _guard(limits, *snapshots):
    """A guard whose sampler replays the given snapshots (the last one repeats)."""
    seq = list(snapshots)

    def sampler():
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return ResourceGuard(limits, sampler=sampler)


# --- ResourceLimits.from_env -----------------------------------------------------


def test_from_env_all_unset_means_disabled():
    limits = ResourceLimits.from_env({})
    assert not limits.enabled()
    assert ResourceGuard(limits).check() is False


def test_from_env_parses_each_limit_and_tuning():
    limits = ResourceLimits.from_env({
        "QAR_MAX_MEMORY_PERCENT": "90",
        "QAR_MIN_FREE_MEMORY_MB": "512",
        "QAR_MAX_LOAD_PER_CORE": "2.5",
        "QAR_RESOURCE_RESUME_MARGIN": "20",
        "QAR_RESOURCE_CHECK_INTERVAL": "15",
    })
    assert limits.enabled()
    assert limits.max_memory_percent == 90.0
    assert limits.min_free_memory_mb == 512.0
    assert limits.max_load_per_core == 2.5
    assert limits.resume_margin_percent == 20.0
    assert limits.check_interval_seconds == 15.0


def test_from_env_blank_and_garbage_values_are_not_enforced():
    limits = ResourceLimits.from_env({
        "QAR_MAX_MEMORY_PERCENT": "  ",
        "QAR_MAX_LOAD_PER_CORE": "lots",
    })
    assert limits.max_memory_percent is None
    assert limits.max_load_per_core is None
    assert not limits.enabled()


# --- overload detection ------------------------------------------------------------


def test_memory_percent_limit_trips_and_names_the_reason(caplog):
    guard = _guard(ResourceLimits(max_memory_percent=90),
                   ResourceSnapshot(memory_percent=95.0))
    assert guard.check() is True
    assert guard.paused
    assert any("pausing new task pickup" in r.message for r in caplog.records)


def test_min_free_memory_limit_trips_on_low_remaining_mb():
    guard = _guard(ResourceLimits(min_free_memory_mb=512),
                   ResourceSnapshot(free_memory_mb=200.0))
    assert guard.check() is True


def test_load_per_core_limit_trips():
    guard = _guard(ResourceLimits(max_load_per_core=2.0),
                   ResourceSnapshot(load_per_core=3.4))
    assert guard.check() is True


def test_below_all_limits_is_not_overloaded():
    guard = _guard(
        ResourceLimits(max_memory_percent=90, min_free_memory_mb=512, max_load_per_core=2.0),
        ResourceSnapshot(memory_percent=50.0, free_memory_mb=4096.0, load_per_core=0.5))
    assert guard.check() is False
    assert not guard.paused


def test_unreadable_metric_never_pauses_and_warns_once(caplog):
    guard = _guard(ResourceLimits(max_memory_percent=90), ResourceSnapshot())  # all None
    assert guard.check() is False
    assert guard.check() is False
    warnings = [r for r in caplog.records if "unreadable" in r.message]
    assert len(warnings) == 1  # warned exactly once, not per check


def test_broken_sampler_treats_resources_as_ok():
    def boom():
        raise RuntimeError("sampling exploded")

    guard = ResourceGuard(ResourceLimits(max_memory_percent=90), sampler=boom)
    assert guard.check() is False  # never raises into the poll loop


# --- hysteresis: resume only once clearly below the limit -------------------------


def test_hysteresis_stays_paused_until_margin_cleared(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="quest-ai-runner.resources")
    limits = ResourceLimits(max_memory_percent=90, resume_margin_percent=10)
    guard = _guard(
        limits,
        ResourceSnapshot(memory_percent=95.0),   # over the limit -> pause
        ResourceSnapshot(memory_percent=85.0),   # below 90 but above 81 (=90*0.9) -> still paused
        ResourceSnapshot(memory_percent=80.0),   # below the resume threshold -> resume
    )
    assert guard.check() is True
    assert guard.check() is True                 # hovering near the limit does not flap
    assert guard.check() is False
    assert any("recovered" in r.message for r in caplog.records)


def test_hysteresis_applies_to_min_free_memory_too():
    limits = ResourceLimits(min_free_memory_mb=500, resume_margin_percent=10)
    guard = _guard(
        limits,
        ResourceSnapshot(free_memory_mb=400.0),  # below the minimum -> pause
        ResourceSnapshot(free_memory_mb=520.0),  # above 500 but below 550 (=500*1.1) -> paused
        ResourceSnapshot(free_memory_mb=600.0),  # clearly recovered -> resume
    )
    assert guard.check() is True
    assert guard.check() is True
    assert guard.check() is False


def test_wait_until_ok_returns_immediately_when_disabled_or_ok():
    assert ResourceGuard(ResourceLimits()).wait_until_ok() is True
    guard = _guard(ResourceLimits(max_memory_percent=90),
                   ResourceSnapshot(memory_percent=10.0))
    assert guard.wait_until_ok() is True


def test_wait_until_ok_blocks_then_resumes_when_resources_recover():
    limits = ResourceLimits(max_memory_percent=90, check_interval_seconds=1)
    guard = _guard(
        limits,
        ResourceSnapshot(memory_percent=95.0),
        ResourceSnapshot(memory_percent=50.0),
    )
    import threading
    stop = threading.Event()  # not set: prove it exits because resources recovered, via wait()
    assert guard.wait_until_ok(stop_event=stop) is True
    assert not guard.paused


def test_wait_until_ok_honors_stop_event_while_paused():
    import threading
    guard = _guard(ResourceLimits(max_memory_percent=90, check_interval_seconds=1),
                   ResourceSnapshot(memory_percent=95.0))  # never recovers
    stop = threading.Event()
    stop.set()
    assert guard.wait_until_ok(stop_event=stop) is False


# --- real sampler smoke test -------------------------------------------------------


def test_sample_resources_returns_a_snapshot_without_raising():
    snap = sample_resources()
    assert isinstance(snap, ResourceSnapshot)
    assert isinstance(snap.describe(), str)
    # Whatever this platform can read must be sane.
    if snap.memory_percent is not None:
        assert 0.0 <= snap.memory_percent <= 100.0
    if snap.free_memory_mb is not None:
        assert snap.free_memory_mb >= 0.0
    if snap.load_per_core is not None:
        assert snap.load_per_core >= 0.0


# --- poller integration: pause is lossless, tasks resume after recovery -----------


def _poller(client, provider, guard):
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=provider,
    )
    return Poller(cfg, state_path=None, client=client, resource_guard=guard)


def test_poller_skips_pickup_while_overloaded_then_resumes_the_same_task():
    """The graceful-degradation contract end to end: an overloaded scan claims NOTHING (the task
    stays queued on the backend), the heartbeat still fires (the env stays live), and the SAME
    task is picked up and completed on the next scan once resources recover."""
    from .test_runner import MockQuestClient

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient(
        [{"id": "task-1", "text": "do it", "status": "queued", "team_id": "team1"}])
    guard = _guard(
        ResourceLimits(max_memory_percent=90),
        ResourceSnapshot(memory_percent=97.0),   # scan 1: overloaded
        ResourceSnapshot(memory_percent=40.0),   # scan 2+: recovered
    )
    poller = _poller(client, provider, guard)

    assert poller.run_once() == []               # paused: nothing claimed, nothing lost
    assert client.claimed == []
    assert len(client.heartbeats) == 1           # the env still heartbeats while paused

    assert poller.run_once() == ["task-1"]       # recovered: the SAME task runs
    assert client.claimed == ["task-1"]
    assert client.reports[0][:2] == ("task-1", "done")


def test_poller_defers_unclaimed_tasks_when_overload_begins_mid_scan():
    """Overload can start mid-batch (earlier tasks may be what pushed the host over). A task whose
    per-task check trips is deferred BEFORE being marked or claimed, so it re-fires later."""
    from .test_runner import MockQuestClient

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"},
                                       {"action": "answer", "rationale": "ok"}])
    client = MockQuestClient(
        [{"id": "task-a", "text": "a", "status": "queued", "team_id": "team1"},
         {"id": "task-b", "text": "b", "status": "queued", "team_id": "team1"}])
    # Sequential checks: scan-gate OK, first task OK, then overload, then recovered.
    guard = _guard(
        ResourceLimits(max_memory_percent=90),
        ResourceSnapshot(memory_percent=40.0),   # run_once scan gate
        ResourceSnapshot(memory_percent=40.0),   # first task's per-task check
        ResourceSnapshot(memory_percent=97.0),   # second task's per-task check -> deferred
        ResourceSnapshot(memory_percent=97.0),   # next scan gate: still overloaded
        ResourceSnapshot(memory_percent=40.0),   # recovered
    )
    poller = _poller(client, provider, guard)
    poller.cfg.max_concurrent_tasks = 1          # deterministic order: a then b

    assert poller.run_once() == ["task-a"]       # b was deferred, not marked, not claimed
    assert client.claimed == ["task-a"]
    assert poller.run_once() == []               # still overloaded: still nothing
    assert poller.run_once() == ["task-b"]       # recovered: the deferred task resumes
    assert client.claimed == ["task-a", "task-b"]


def test_poller_with_no_limits_behaves_exactly_as_before():
    """Backward compatibility: nothing configured -> the guard is a no-op and a normal scan runs."""
    from .test_runner import MockQuestClient

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient(
        [{"id": "task-z", "text": "do it", "status": "queued", "team_id": "team1"}])
    poller = _poller(client, provider, ResourceGuard(ResourceLimits()))
    assert poller.run_once() == ["task-z"]
    assert client.claimed == ["task-z"]
