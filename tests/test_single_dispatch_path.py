"""One dispatch path: the short-cadence dispatch loop picks up any due task, a waiting human's
task first, while the long scan is housekeeping only."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .test_context_request_fast_lane import _poller_with_assembler
from .test_runner import MockQuestClient


def _ctx(i, **extra):
    return {"id": f"ctx-{i}", "status": "queued", "team_id": "team1",
            "context_request": {"query": "q", "max_chars": None}, **extra}


def test_housekeeping_alone_dispatches_nothing():
    client = MockQuestClient([_ctx(1)])
    poller, _ = _poller_with_assembler(client)

    assert poller._housekeeping() is True

    assert client.claimed == []


def test_dispatch_goes_real_time_first_then_oldest():
    client = MockQuestClient([
        _ctx(1, created_at="2026-10-05T10:00:00Z"),
        _ctx(2, created_at="2026-10-05T09:00:00Z"),
        _ctx(3, created_at="2026-10-05T11:00:00Z", real_time=True),
    ])
    poller, _ = _poller_with_assembler(client, max_concurrent_tasks=1)

    with ThreadPoolExecutor(max_workers=1) as pool:
        submitted = poller._dispatch_due(pool)

    assert submitted == ["ctx-3"]            # one free slot: the waiting human's task takes it


def test_dispatch_only_submits_free_slots_and_leaves_the_rest_queued():
    client = MockQuestClient([_ctx(1), _ctx(2), _ctx(3)])
    poller, _ = _poller_with_assembler(client, max_concurrent_tasks=2)
    gate = threading.Event()
    poller._handle_one = lambda t: (gate.wait(5), str(t["id"]))[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = poller._dispatch_due(pool)
        again = poller._dispatch_due(pool)   # slots still held by the running tasks
        gate.set()

    assert len(first) == 2
    assert again == []


def test_dispatch_interval_defaults_to_seconds():
    poller, _ = _poller_with_assembler(MockQuestClient([]))
    assert 0 < poller.cfg.dispatch_interval_seconds <= 30


def test_run_forever_dispatches_on_its_own_short_loop_not_the_long_scan():
    import time
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller
    from .conftest import StubProvider, StubRetrieval

    client = MockQuestClient([_ctx(1)])
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=StubProvider(decisions=[]),
        poll_interval_seconds=3600, dispatch_interval_seconds=0.05,
        wait_channel_enabled=False, context_poll_seconds=0,
    )
    poller = Poller(cfg, state_path=None, client=client)
    stop = threading.Event()
    t = threading.Thread(target=poller.run_forever, kwargs={"stop_event": stop}, daemon=True)
    t.start()
    deadline = time.time() + 5
    while not client.claimed and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    t.join(5)

    assert client.claimed == ["ctx-1"]   # picked up within seconds, with a one-hour scan interval
