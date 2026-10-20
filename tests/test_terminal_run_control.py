"""The terminal steers, stops and pauses a RUNNING deep worker, like typing into Claude Code.

Two layers, both offline (a scripted stand-in for ``claude``, no model, no cost):

* ``TerminalRunControl`` and the real ``StreamJsonGoalRunner`` over ``tests/fakes/fake_claude_stream.py``:
  a typed message reaches the live worker exactly once, a stop or a pause keeps the session, and a
  message the worker never confirmed is returned, not lost.
* ``QuestAITerminal``'s side: ``/pause``, ``/resume``, what ``_finish_turn`` does with a paused or
  stopped run, and that a worker that does not hold a live session (the one-shot runner) is never
  handed the control, so the old queued-message behaviour is untouched.
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.inbox import InMemoryInbox
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig, OrchestratorResult
from quest_ai_runner.core.run_control import RunControl
from quest_ai_runner.textual_ui import QuestAITerminal
from quest_ai_runner.textual_ui_control import TerminalRunControl

from .conftest import StubProvider, StubRetrieval
from .test_goal_runner_stream import STEPS3, make_claude, make_runner, read_log

KEY = "terminal-test-session"


def make_control(inbox, **kwargs) -> TerminalRunControl:
    cancel = threading.Event()
    control = TerminalRunControl(inbox, KEY, cancel_check=cancel.is_set, poll_seconds=0.1, **kwargs)
    control.cancel = cancel
    return control


def run_in_thread(runner, control: TerminalRunControl, **kwargs):
    box: Dict[str, Any] = {}

    def go() -> None:
        box["result"] = runner.run_goal(goal="do the steps", brief="steps",
                                        control=control.run_control(),
                                        cancel_check=control.cancel.is_set, **kwargs)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, box


# --- the control against the real stream-json runner -------------------------------------------

def test_a_message_typed_while_the_worker_runs_reaches_it_once(tmp_path):
    inbox = InMemoryInbox()
    sent: List[Any] = []
    seen: List[Any] = []
    control = make_control(inbox, on_sent=lambda texts, rest: sent.append((texts, rest)),
                           on_seen=lambda texts: seen.append(texts))
    runner = make_runner(make_claude(tmp_path, {"steps": STEPS3}), tmp_path)
    thread, box = run_in_thread(runner, control)
    time.sleep(0.3)
    inbox.push(KEY, "skip step3")        # exactly what the terminal's queued line does
    thread.join(timeout=15)
    assert not thread.is_alive()
    res = box["result"]
    assert res.output == "ran step1,step2"            # the live worker obeyed it
    assert sent == [(["skip step3"], 0)]               # taken once, queue empty afterwards
    assert seen == [["skip step3"]]                    # and confirmed once
    assert inbox.peek(KEY) == []                       # nothing left for the end-of-turn flush
    assert control.unconfirmed(res.unseen_inputs) == []
    assert control.in_use is True


def test_stop_keeps_the_session_and_an_unconfirmed_message_is_put_back_first(tmp_path):
    inbox = InMemoryInbox()
    control = make_control(inbox)
    # The worker never replays what it is sent, so the message can never be confirmed.
    claude = make_claude(tmp_path, {"steps": [{"name": "slow", "seconds": 30}], "no_echo": True})
    runner = make_runner(claude, tmp_path)
    thread, box = run_in_thread(runner, control)
    time.sleep(0.4)
    inbox.push(KEY, "first thought")
    time.sleep(0.5)                                    # taken by the live worker
    assert inbox.peek(KEY) == []
    control.cancel.set()                               # Esc
    thread.join(timeout=15)
    res = box["result"]
    assert res.cancelled is True and res.session_id
    assert res.unseen_inputs == ["first thought"]
    # The terminal's own ledger and the worker's report name the SAME message: one entry, not two.
    assert control.unconfirmed(res.unseen_inputs) == ["first thought"]
    inbox.push(KEY, "typed after")
    control.put_back(control.unconfirmed(res.unseen_inputs))
    assert inbox.drain(KEY) == ["first thought", "typed after"]


def test_pause_ends_the_run_paused_and_resume_continues_the_same_session(tmp_path):
    inbox = InMemoryInbox()
    control = make_control(inbox)
    claude = make_claude(tmp_path, {"steps": [{"name": "quick", "seconds": 0.1},
                                              {"name": "slow", "seconds": 30}]})
    runner = make_runner(claude, tmp_path)
    thread, box = run_in_thread(runner, control)
    time.sleep(0.6)
    control.pause_event.set()                          # /pause
    thread.join(timeout=15)
    res = box["result"]
    assert res.paused is True and res.cancelled is True and res.session_id
    events = [e["event"] for e in read_log(tmp_path)]
    assert "interrupt" in events

    resumed = make_runner(make_claude(tmp_path, {"steps": [{"name": "after", "seconds": 0.1}]},
                                      name="claude2"), tmp_path)
    again = resumed.run_goal(goal="do the steps", brief="steps", resume_session_id=res.session_id,
                             control=make_control(inbox).run_control())
    argv = [e for e in read_log(tmp_path, "claude2") if e["event"] == "argv"][0]
    assert "--resume" in argv["argv"] and res.session_id in argv["argv"]
    assert again.met is True


def test_a_message_is_never_taken_by_two_consumers():
    """The live worker, the orchestrator's own drain and the end-of-turn flush share one inbox, and
    ``drain`` is atomic: whoever drains first owns the message."""
    inbox = InMemoryInbox()
    control = make_control(inbox)
    inbox.push(KEY, "only once")
    claimed = control.claim_inputs()
    assert [m.text for m in claimed] == ["only once"]
    assert [m.id for m in claimed] == ["terminal-1"]
    assert inbox.drain(KEY) == []                       # the flush finds nothing: no double send
    assert control.claim_inputs() == []
    control.mark_seen(["terminal-1"], "deep_worker")
    assert control.unconfirmed() == []


def test_ids_are_stable_and_count_up():
    inbox = InMemoryInbox()
    control = make_control(inbox)
    inbox.push(KEY, "a")
    inbox.push(KEY, "b")
    assert [m.id for m in control.claim_inputs()] == ["terminal-1", "terminal-2"]
    inbox.push(KEY, "c")
    assert [m.id for m in control.claim_inputs()] == ["terminal-3"]


def test_a_failing_callback_never_breaks_the_run():
    inbox = InMemoryInbox()

    def boom(*args):
        raise RuntimeError("ui gone")

    control = make_control(inbox, on_sent=boom, on_seen=boom, on_live=boom)
    inbox.push(KEY, "still delivered")
    assert [m.text for m in control.claim_inputs()] == ["still delivered"]
    control.mark_seen(["terminal-1"], "deep_worker")
    assert control.unconfirmed() == []


# --- through the orchestrator: who gets the control -----------------------------------------------

class ControlAwareRunner:
    def __init__(self) -> None:
        self.controls: List[Any] = []
        self.resumed: List[Any] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, control=None,
                 resume_session_id=None) -> DeepResult:
        self.controls.append(control)
        self.resumed.append(resume_session_id)
        return DeepResult(met=True, output="done")


class OneShotRunner:
    """Like the one-shot subprocess runner: it does not name ``control``."""

    def __init__(self) -> None:
        self.kwargs: List[Dict[str, Any]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, cancel_check=None) -> DeepResult:
        self.kwargs.append({"goal": goal})
        return DeepResult(met=True, output="done")


def deep_orchestrator(runner):
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "G", "deep_brief": "B", "rationale": "work"},
        {"met": True, "reason": "done"},
    ])
    cfg = OrchestratorConfig(deep_goal_max_iterations=1)
    cfg.overseer = False
    return Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider),
                        deep_runner=runner, config=cfg)


def test_run_stream_hands_the_control_and_the_session_to_a_live_worker():
    runner = ControlAwareRunner()
    control = RunControl(label="t")
    items = list(deep_orchestrator(runner).run_stream("fix it", run_control=control,
                                                      resume_session_id="sess-1"))
    assert isinstance(items[-1], OrchestratorResult)
    assert runner.controls == [control]
    assert runner.resumed == ["sess-1"]


def test_a_one_shot_worker_is_not_given_the_control_and_the_queue_is_untouched():
    inbox = InMemoryInbox()
    inbox.push(KEY, "left for the end-of-turn flush")
    control = make_control(inbox)
    runner = OneShotRunner()
    list(deep_orchestrator(runner).run_stream("fix it", run_control=control.run_control()))
    assert runner.kwargs                                # it ran
    assert control.in_use is False                      # nothing ever asked the control
    assert inbox.peek(KEY) == ["left for the end-of-turn flush"]


def test_a_stopped_worker_result_carries_its_session_to_the_caller():
    class Stopped:
        def run_goal(self, *, goal, brief, model=None, max_turns=None, control=None) -> DeepResult:
            return DeepResult(met=False, cancelled=True, paused=True, session_id="sess-9",
                              unseen_inputs=["lost?"])

    items = list(deep_orchestrator(Stopped()).run_stream("fix it", run_control=RunControl()))
    final = items[-1]
    assert final.kind == "cancelled"
    assert [(d.session_id, d.paused, d.unseen_inputs) for d in final.deep_results] == [
        ("sess-9", True, ["lost?"])]


# --- the terminal: /pause, /resume, wrap-up ---------------------------------------------------------

def stub_session(inbox: InMemoryInbox) -> SimpleNamespace:
    return SimpleNamespace(
        _orch=SimpleNamespace(input_inbox=inbox, context_assembler=None),
        _last_user="", _last_assistant="", _session_history=[], _turns=[], _turn_count=0,
        _write_session_file=lambda: None, _maybe_refresh_next_steps=lambda final: None)


def run_app(build):
    async def go():
        app = QuestAITerminal(None, _config=None)
        app._build_session_worker = lambda: None
        async with app.run_test() as pilot:
            await pilot.pause()
            return await build(app, pilot)

    return asyncio.run(go())


def paused_result(unseen=()):
    return OrchestratorResult(
        kind="cancelled", goals=["build it"], exit_reason="cancelled",
        deep_results=[DeepResult(met=False, cancelled=True, paused=True, session_id="sess-7",
                                 unseen_inputs=list(unseen))])


def test_pause_without_a_live_worker_says_so_and_changes_nothing():
    async def build(app, pilot):
        app.sess = stub_session(InMemoryInbox())
        app._turn_active = True
        app._run_control = app._new_terminal_control()
        app._cmd_pause()
        return app._run_control.pause_event.is_set()

    assert run_app(build) is False


def test_pause_with_a_live_worker_sets_the_pause_flag():
    async def build(app, pilot):
        app.sess = stub_session(InMemoryInbox())
        app._turn_active = True
        app._run_control = app._new_terminal_control()
        app._run_control._live = True
        app._cmd_pause()
        return app._run_control.pause_event.is_set()

    assert run_app(build) is True


def test_a_paused_turn_is_resumable_and_keeps_queued_messages_for_the_resume():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = stub_session(inbox)
        app._run_control = app._new_terminal_control()
        app._turn_request = "build it"
        inbox.push(app._session_id, "typed while paused")
        started: List[Any] = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))
        app._finish_turn("build it", paused_result(unseen=["never confirmed"]), 1.0, False, None)
        held = list(app._held_for_resume)
        inbox_left = inbox.peek(app._session_id)
        began_new_turn = list(started)
        app._cmd_resume()
        return held, inbox_left, began_new_turn, started, app._resumable

    held, inbox_left, began_new_turn, started, resumable = run_app(build)
    assert held == ["never confirmed", "typed while paused"]    # in the order typed, none lost
    assert inbox_left == []                                      # not flushed into a fresh turn
    assert began_new_turn == []
    assert len(started) == 1
    text, kw = started[0]
    assert kw["resume_session_id"] == "sess-7" and kw["echo"] is False
    assert text.startswith("build it") and "never confirmed" in text and "typed while paused" in text
    assert resumable is None                                     # consumed once


def test_resume_with_nothing_paused_says_so():
    async def build(app, pilot):
        app.sess = stub_session(InMemoryInbox())
        started: List[Any] = []
        app._begin_turn = lambda text, **kw: started.append(text)
        app._cmd_resume()
        return started

    assert run_app(build) == []


def test_an_unconfirmed_message_is_sent_again_by_the_end_of_turn_flush_ahead_of_newer_ones():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = stub_session(inbox)
        app._run_control = app._new_terminal_control()
        app._turn_request = "build it"
        inbox.push(app._session_id, "taken, never confirmed")
        app._run_control.claim_inputs()                  # the live worker took it
        inbox.push(app._session_id, "typed later")
        started: List[Any] = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))
        stopped = OrchestratorResult(kind="cancelled", goals=["build it"], exit_reason="cancelled",
                                     deep_results=[DeepResult(met=False, cancelled=True,
                                                              session_id="sess-3")])
        app._finish_turn("build it", stopped, 1.0, True, None)   # Esc
        return started, app._resumable

    started, resumable = run_app(build)
    assert started == [("taken, never confirmed\n\ntyped later", {"echo": True, "auto": False})]
    assert resumable is not None and resumable["session_id"] == "sess-3"   # a stop is resumable too


def test_an_errored_turn_keeps_an_unconfirmed_message_queued():
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = stub_session(inbox)
        app._run_control = app._new_terminal_control()
        inbox.push(app._session_id, "taken, never confirmed")
        app._run_control.claim_inputs()
        app._begin_turn = lambda text, **kw: None
        app._finish_turn("build it", None, 1.0, False, RuntimeError("boom"))
        return inbox.peek(app._session_id)

    assert run_app(build) == ["taken, never confirmed"]


def test_a_cancel_with_no_live_worker_is_exactly_what_it_was():
    """The one-shot worker never asks the control for anything, so Esc looks and acts as before."""
    async def build(app, pilot):
        inbox = InMemoryInbox()
        app.sess = stub_session(inbox)
        app._run_control = app._new_terminal_control()
        inbox.push(app._session_id, "m1")
        started: List[Any] = []
        app._begin_turn = lambda text, **kw: started.append((text, kw))
        app._finish_turn("original request", None, 1.0, True, None)
        return started, app._resumable

    started, resumable = run_app(build)
    assert started == [("m1", {"echo": True, "auto": False})]
    assert resumable is None
