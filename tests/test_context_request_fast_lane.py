"""Work package D: quest-context hub parity -- the runner side.

Covers:
  * D1 -- a claimed ``context_request`` task is answered by assembling context LOCALLY (no goal
    loop, no LLM plan/answer call), truncated to ``max_chars``, reported done with card metadata.
  * D2 revised -- the fast lane: QuestClient.wait_for_interactive (long-poll) and
    list_interactive_due (fallback short poll), and the Poller's ``_fast_lane_loop`` dispatch logic.
  * D2b -- claim ordering: the in-process guard (``_claim_slot``/``_release_slot``) prevents the
    background scan and the fast lane from BOTH handling the same task.
  * D3 -- ``env_id`` passes through ``discover_due`` so multi-environment teams route correctly.

No network; MockQuestClient (test_runner.py) and a local StubContextAssembler stand in for the
backend and the card store.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.core.adapters import AssembledContext
from quest_ai_runner.runner.poller import Poller, _task_signature

from .conftest import StubProvider, StubRetrieval
from .test_runner import MockQuestClient


class StubContextAssembler:
    """A ContextAssembler that returns a scripted AssembledContext, recording every call."""

    def __init__(self, context_view: str = "ASSEMBLED CONTEXT",
                 card_metadata: Optional[List[Dict[str, Any]]] = None,
                 raise_on_assemble: bool = False):
        self._context_view = context_view
        self._card_metadata = card_metadata or []
        self._raise = raise_on_assemble
        self.assemble_calls: List[str] = []

    def assemble(self, task_text: str, *, meta=None, on_event=None) -> AssembledContext:
        self.assemble_calls.append(task_text)
        if self._raise:
            raise RuntimeError("assembler exploded")
        return AssembledContext(context_view=self._context_view, card_metadata=list(self._card_metadata))

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        pass


def _poller_with_assembler(client, *, context_assembler=..., **kw) -> Poller:
    """Build a Poller whose orchestrator has an EXPLICIT context_assembler (no default FileContextStore
    bootstrap -- keeps these tests fast and filesystem-free). ``context_assembler=None`` explicitly
    disables context handling (a valid RunnerConfig state); the ellipsis default installs a fresh
    StubContextAssembler."""
    provider = StubProvider(decisions=[])
    if context_assembler is ...:
        context_assembler = StubContextAssembler()
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=provider,
        context_assembler=context_assembler,
        **kw,
    )
    return Poller(cfg, state_path=None, client=client), provider


# --- D1: context_request handling ------------------------------------------------

def test_context_request_answered_without_goal_loop():
    assembler = StubContextAssembler(context_view="local corpus says X", card_metadata=[{"id": "c1"}])
    client = MockQuestClient([{
        "id": "ctx-1", "status": "queued", "team_id": "team1",
        "context_request": {"query": "what happened wednesday", "max_chars": None},
    }])
    poller, provider = _poller_with_assembler(client, context_assembler=assembler)

    handled = poller.run_once()

    assert handled == ["ctx-1"]
    assert client.claimed == ["ctx-1"]
    assert client.claim_handlers == [("ctx-1", "context-request")]
    # No goal loop: the planner/LLM was never invoked for a context-request.
    assert provider.plan_calls == 0
    assert assembler.assemble_calls == ["what happened wednesday"]
    assert client.result_data_reports == [
        ("ctx-1", "local corpus says X", {"card_metadata": [{"id": "c1"}]}),
    ]
    # A context-request never goes through the ordinary status=done report path's text-only PATCH;
    # report_done_with_data folds into client.reports too so existing assertions on "done" still see it.
    assert client.reports[0][:2] == ("ctx-1", "done")


def test_context_request_falls_back_to_task_text_when_query_missing():
    assembler = StubContextAssembler(context_view="ok")
    client = MockQuestClient([{
        "id": "ctx-2", "status": "queued", "team_id": "team1", "text": "fallback query text",
        "context_request": {},
    }])
    poller, _provider = _poller_with_assembler(client, context_assembler=assembler)
    poller.run_once()
    assert assembler.assemble_calls == ["fallback query text"]


def test_context_request_truncates_to_max_chars():
    assembler = StubContextAssembler(context_view="0123456789" * 5)  # 50 chars
    client = MockQuestClient([{
        "id": "ctx-3", "status": "queued", "team_id": "team1",
        "context_request": {"query": "q", "max_chars": 10},
    }])
    poller, _provider = _poller_with_assembler(client, context_assembler=assembler)
    poller.run_once()
    text = client.result_data_reports[0][1]
    assert text.startswith("0123456789")
    assert "[truncated]" in text
    assert len(text) < 50


def test_context_request_no_assembler_reports_empty_done():
    client = MockQuestClient([{
        "id": "ctx-4", "status": "queued", "team_id": "team1",
        "context_request": {"query": "q"},
    }])
    poller, _provider = _poller_with_assembler(client, context_assembler=None)
    handled = poller.run_once()
    assert handled == ["ctx-4"]
    assert client.result_data_reports == [("ctx-4", "", None)]


def test_context_request_assembler_failure_reports_failed_not_raise():
    assembler = StubContextAssembler(raise_on_assemble=True)
    client = MockQuestClient([{
        "id": "ctx-5", "status": "queued", "team_id": "team1",
        "context_request": {"query": "q"},
    }])
    poller, _provider = _poller_with_assembler(client, context_assembler=assembler)
    handled = poller.run_once()
    assert handled == ["ctx-5"]
    assert client.reports[-1][0] == "ctx-5"
    assert client.reports[-1][1] == "failed"
    assert "assembler exploded" in client.reports[-1][2]


def test_context_request_claim_failure_skips_cleanly():
    class _ClaimFails(MockQuestClient):
        def claim(self, task_id, handler=None):
            self.claimed.append(task_id)
            return None

    assembler = StubContextAssembler()
    client = _ClaimFails([{
        "id": "ctx-6", "status": "queued", "team_id": "team1",
        "context_request": {"query": "q"},
    }])
    poller, _provider = _poller_with_assembler(client, context_assembler=assembler)
    handled = poller.run_once()
    assert handled == []
    assert assembler.assemble_calls == []          # never even tried to assemble
    assert client.result_data_reports == []


# --- D3: env_id threads through discover_due --------------------------------------

def test_discover_due_passes_env_id_through():
    client = MockQuestClient([])
    poller, _provider = _poller_with_assembler(client, env_id="env-A")
    poller.run_once()
    assert client.discover_env_ids == ["env-A"]


def test_discover_due_env_id_defaults_to_none():
    client = MockQuestClient([])
    poller, _provider = _poller_with_assembler(client)
    poller.run_once()
    assert client.discover_env_ids == [None]


# --- D2b: claim ordering / in-process guard ---------------------------------------

def test_claim_slot_guards_concurrent_handling_of_same_task():
    client = MockQuestClient([])
    poller, _provider = _poller_with_assembler(client)
    assert poller._claim_slot("dup-1") is True
    # A second attempt for the SAME task_id, before release, must be refused.
    assert poller._claim_slot("dup-1") is False
    poller._release_slot("dup-1")
    # After release, it can be claimed again.
    assert poller._claim_slot("dup-1") is True


def test_dispatch_fast_task_skips_when_background_scan_holds_the_slot():
    assembler = StubContextAssembler()
    client = MockQuestClient([])
    poller, _provider = _poller_with_assembler(client, context_assembler=assembler)
    task = {"id": "ctx-7", "status": "queued", "team_id": "team1",
            "context_request": {"query": "q"}}
    poller._claim_slot("ctx-7")   # simulate the background scan already handling it
    poller._dispatch_fast_task(task)
    assert client.claimed == []                 # the fast lane never even tried to claim
    assert assembler.assemble_calls == []


def test_dispatch_fast_task_dedupes_via_state_store(tmp_path):
    assembler = StubContextAssembler()
    client = MockQuestClient([])
    state_path = str(tmp_path / "qar_state.json")
    provider = StubProvider(decisions=[])
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({}), model_provider=provider, context_assembler=assembler,
    )
    poller = Poller(cfg, state_path=state_path, client=client)
    task = {"id": "ctx-8", "status": "queued", "team_id": "team1",
            "context_request": {"query": "q"}}
    poller._dispatch_fast_task(task)
    assert client.claimed == ["ctx-8"]
    # Same signature again -> the fast lane must not re-handle it (it was already marked handled).
    poller._dispatch_fast_task(task)
    assert client.claimed == ["ctx-8"]


# --- D2 revised: the fast-lane loop (long-poll + fallback poll) --------------------

def test_fast_lane_long_poll_dispatches_delivered_task():
    """The wait-channel path: wait_for_interactive() returning a task gets dispatched immediately;
    an empty/None result reconnects (loop continues) until stop_event is set."""
    assembler = StubContextAssembler(context_view="from wait channel")
    delivered = {"id": "ctx-9", "status": "queued", "team_id": "team1",
                 "context_request": {"query": "q"}}
    client = MockQuestClient([])
    calls = {"n": 0}

    def _wait_for_interactive(*, team_id=None, env_id=None, timeout=25.0):
        calls["n"] += 1
        if calls["n"] == 1:
            return delivered
        stop_event.set()
        return None

    client.wait_for_interactive = _wait_for_interactive
    poller, _provider = _poller_with_assembler(client, context_assembler=assembler)

    stop_event = threading.Event()
    poller._fast_lane_loop(stop_event)

    assert calls["n"] >= 2
    assert client.claimed == ["ctx-9"]
    assert assembler.assemble_calls == ["q"]


def test_fast_lane_falls_back_to_short_poll_when_wait_channel_disabled():
    assembler = StubContextAssembler()
    delivered = {"id": "ctx-10", "status": "queued", "team_id": "team1", "real_time": True,
                 "context_request": {"query": "q"}}
    client = MockQuestClient([])
    calls = {"n": 0}

    def _list_interactive_due(*, team_id=None, env_id=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return [delivered]
        stop_event.set()
        return []

    client.list_interactive_due = _list_interactive_due
    poller, _provider = _poller_with_assembler(
        client, context_assembler=assembler,
        wait_channel_enabled=False, context_poll_seconds=0.01,
    )

    stop_event = threading.Event()
    poller._fast_lane_loop(stop_event)

    assert calls["n"] >= 2
    assert client.claimed == ["ctx-10"]


def test_fast_lane_disabled_entirely_returns_immediately():
    """context_poll_seconds<=0 with the wait channel off means the fast lane is fully off."""
    client = MockQuestClient([])
    poller, _provider = _poller_with_assembler(
        client, wait_channel_enabled=False, context_poll_seconds=0,
    )
    stop_event = threading.Event()
    start = time.monotonic()
    poller._fast_lane_loop(stop_event)  # must return promptly, not hang
    assert time.monotonic() - start < 1.0


def test_fast_lane_skips_when_unconfigured_or_teamless():
    client = MockQuestClient([])
    client.configured = False
    poller, _provider = _poller_with_assembler(client)
    stop_event = threading.Event()
    poller._fast_lane_loop(stop_event)  # returns immediately -- nothing to attach to

    client2 = MockQuestClient([])
    provider2 = StubProvider(decisions=[])
    cfg2 = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="",
        retrieval=StubRetrieval({}), model_provider=provider2,
    )
    poller2 = Poller(cfg2, state_path=None, client=client2)
    poller2._fast_lane_loop(threading.Event())  # no team_id -- returns immediately


def test_run_forever_starts_and_stops_the_fast_lane_thread():
    """run_forever spawns the fast-lane daemon thread and stops it via its own internal event when
    the caller's stop_event fires -- proven indirectly via the background scan running at least
    once and the process exiting cleanly (no hang)."""
    client = MockQuestClient([{"id": "rf-1", "status": "queued", "team_id": "team1"}])
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="team1",
        retrieval=StubRetrieval({"README.md": "fact"}), model_provider=provider,
        poll_interval_seconds=0.01, wait_channel_enabled=False, context_poll_seconds=0,
    )
    poller = Poller(cfg, state_path=None, client=client)
    stop_event = threading.Event()
    stop_event.set()  # stop immediately after the first scan
    poller.run_forever(stop_event=stop_event)
    assert client.claimed == ["rf-1"]


# --- QuestClient: the new HTTP surfaces (no network; monkeypatch urlopen) ----------

def test_quest_client_wait_for_interactive_builds_url_and_pads_timeout(monkeypatch):
    import json
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"task": {"id": "t1", "context_request": {"query": "q"}}}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    client = QuestClient("http://quest.example", "qsk_test", team_id="team1")
    task = client.wait_for_interactive(env_id="env-A", timeout=20.0)

    assert task == {"id": "t1", "context_request": {"query": "q"}}
    assert captured["url"].startswith("http://quest.example/api/assistant-tasks/wait?")
    assert "real_time=true" in captured["url"]
    assert "timeout=20" in captured["url"]
    assert "team_id=team1" in captured["url"]
    assert "env_id=env-A" in captured["url"]
    assert captured["timeout"] == 30.0  # padded past the server's own wait bound


def test_quest_client_wait_for_interactive_empty_wait_returns_none(monkeypatch):
    import json
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"task": None}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())

    client = QuestClient("http://quest.example", "qsk_test", team_id="team1")
    assert client.wait_for_interactive(timeout=5.0) is None


def test_quest_client_wait_for_interactive_never_raises_on_transport_error(monkeypatch):
    import urllib.error
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    def _boom(req, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    client = QuestClient("http://quest.example", "qsk_test", team_id="team1")
    assert client.wait_for_interactive(timeout=5.0) is None


def test_quest_client_list_interactive_due_builds_url(monkeypatch):
    import json
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"tasks": [], "count": 0}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = QuestClient("http://quest.example", "qsk_test", team_id="team1")
    client.list_interactive_due(env_id="env-B")
    assert "real_time=true" in captured["url"]
    assert "status=queued" in captured["url"]
    assert "env_id=env-B" in captured["url"]


def test_quest_client_discover_due_includes_env_id(monkeypatch):
    import json
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"tasks": []}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = QuestClient("http://quest.example", "qsk_test", team_id="team1")
    client.discover_due(now=datetime(2026, 1, 1, tzinfo=timezone.utc), env_id="env-C")
    assert "env_id=env-C" in captured["url"]


def test_quest_client_report_done_with_data_omits_result_data_when_empty(monkeypatch):
    import json
    import urllib.request

    from quest_ai_runner.runner.quest_client import QuestClient

    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"status": "done"}).encode()

    def _fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    client = QuestClient("http://quest.example", "qsk_test", team_id="team1")

    client.report_done_with_data("t1", "plain text result")
    assert captured["body"] == {"status": "done", "result": "plain text result"}

    client.report_done_with_data("t2", "with cards", {"card_metadata": [{"id": "c1"}]})
    assert captured["body"] == {
        "status": "done", "result": "with cards",
        "result_data": {"card_metadata": [{"id": "c1"}]},
    }


# --- the fast lane must share the background scan's discovery scope ---------------

def test_fast_lane_wait_uses_discovery_team_id_not_team_id():
    """An owner-scoped lane (``discovery_team_id=""``) must wait owner-scoped too.

    Regression: the fast lane scoped its long-poll by ``cfg.team_id`` while ``run_once`` used
    ``discovery_team_id``. Quest's UI creates a personal chat task with ``team_id=None``, so the
    team-filtered wait matched nothing and every real-time chat task fell through to the slow
    background scan -- indistinguishable, from the chat, from the lane being dead.
    """
    seen: Dict[str, Any] = {}

    def _wait_for_interactive(*, team_id=None, env_id=None, timeout=25.0):
        seen["team_id"] = team_id
        seen["env_id"] = env_id
        stop_event.set()
        return None

    client = MockQuestClient([])
    client.wait_for_interactive = _wait_for_interactive
    poller, _provider = _poller_with_assembler(
        client, discovery_team_id="", env_id="env-personal")

    stop_event = threading.Event()
    poller._fast_lane_loop(stop_event)

    assert seen["team_id"] == ""          # owner-scoped: the client sends no team filter
    assert seen["env_id"] == "env-personal"


def test_fast_lane_short_poll_uses_discovery_team_id_not_team_id():
    """Same scoping fix on the wait-channel-disabled fallback path."""
    seen: Dict[str, Any] = {}

    def _list_interactive_due(*, team_id=None, env_id=None):
        seen["team_id"] = team_id
        stop_event.set()
        return []

    client = MockQuestClient([])
    client.list_interactive_due = _list_interactive_due
    poller, _provider = _poller_with_assembler(
        client, discovery_team_id="", wait_channel_enabled=False, context_poll_seconds=0.01)

    stop_event = threading.Event()
    poller._fast_lane_loop(stop_event)

    assert seen["team_id"] == ""


def test_fast_lane_still_scopes_by_team_when_no_discovery_override():
    """A team-bound lane keeps its per-team isolation: no override means scope by team_id."""
    seen: Dict[str, Any] = {}

    def _wait_for_interactive(*, team_id=None, env_id=None, timeout=25.0):
        seen["team_id"] = team_id
        stop_event.set()
        return None

    client = MockQuestClient([])
    client.wait_for_interactive = _wait_for_interactive
    poller, _provider = _poller_with_assembler(client)  # team_id="team1", no discovery override

    stop_event = threading.Event()
    poller._fast_lane_loop(stop_event)

    assert seen["team_id"] == "team1"


def test_fast_lane_attaches_for_a_teamless_owner_scoped_lane():
    """team_id="" but discovery_team_id="" set: owner-scoped discovery is intended, so attach."""
    seen = {"n": 0}

    def _wait_for_interactive(*, team_id=None, env_id=None, timeout=25.0):
        seen["n"] += 1
        stop_event.set()
        return None

    client = MockQuestClient([])
    client.wait_for_interactive = _wait_for_interactive
    provider = StubProvider(decisions=[])
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test", team_id="",
        discovery_team_id="", retrieval=StubRetrieval({}), model_provider=provider,
    )
    poller = Poller(cfg, state_path=None, client=client)

    stop_event = threading.Event()
    poller._fast_lane_loop(stop_event)

    assert seen["n"] == 1  # attached, rather than returning immediately
