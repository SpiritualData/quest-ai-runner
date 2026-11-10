"""What a run says about its structure, its limits and why it ended reaches Quest in the shapes Quest validates, and a Resume continues the session.

Pinned here, offline with fakes (no model, no network, no server):
  * the relay (``run_facts.RunFacts``) turns a deep runner's events (a raw Claude Code stream with helpers, a ``tokens`` tick with the ceilings, the hub's
    own resume decision) into ``agent``, ``structure``, ``spend``, ``tokens`` and ``stop_decision`` ticks; a structure goes out only when the tree
    changed, a spend only when it changed and after its gap, and a fact the run does not know is left out, never sent as zero;
  * ``end_facts`` gives the end of a run (end reason, resume handle only for a run that did not finish, spend, ceilings, outside folder, left, stop decision);
  * a whole turn through the orchestrator with a scripted worker streams the facts for a chat turn and posts them as progress ticks and ``result_data`` for a task;
  * when the sibling Quest backend checkout is present, every tick and end fact is run through ITS validator and must come back unchanged;
  * Resume: the short message is recognised from the person's own words, a chat turn continues the session the run left, a task continues the handle Quest or
    its earlier report holds, and a resume with no handle is not claimed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from hubofhubs.api import DeepResult, Mode, ProgressEvent
from quest_ai_runner.the_consciousness.request_intake.executor import TaskExecutor
from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
from quest_ai_runner.the_intelligence.mind.delegation import resume_request
from quest_ai_runner.the_intelligence.mind.delegation.run_facts import RunFacts, end_facts, stop_decision_of
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig, OrchestratorResult
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def stream(**line: Any) -> ProgressEvent:
    return ProgressEvent(type="stream", text="", data=line)


def delegate(ident: str, description: str, parent: Optional[str] = None) -> ProgressEvent:
    line: Dict[str, Any] = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": ident, "name": "Task", "input": {"description": description}}]}}
    if parent:
        line["parent_tool_use_id"] = parent
    return stream(**line)


def finish_delegate(ident: str) -> ProgressEvent:
    return stream(type="user", message={"content": [{"type": "tool_result", "tool_use_id": ident, "content": "ok"}]})


def tokens(cost: Optional[float], **more: Any) -> ProgressEvent:
    data = dict(more)
    if cost is not None:
        data["cost_usd"] = cost
    return ProgressEvent(type="tokens", text="spent", data=data)


def relay(clock: Optional[Clock] = None, **kw: Any):
    sent: List[ProgressEvent] = []
    return RunFacts(sent.append, clock=clock or Clock(), **kw), sent


def of(sent: List[ProgressEvent], kind: str) -> List[Dict[str, Any]]:
    return [e.data for e in sent if e.type == kind]


# --- the relay ------------------------------------------------------------------------------------

def test_helpers_are_announced_with_their_parent_and_solution_and_the_structure_follows_only_when_it_changed():
    facts, sent = relay()
    facts.take(delegate("a1", "[the-mind] check the plan"))
    facts.take(delegate("a2", "read the sources", parent="a1"))
    facts.take(finish_delegate("a2"))
    started = [d for d in of(sent, "agent") if d["type"] == "agent-started"]
    assert started[0] == {"type": "agent-started", "agent_id": "a1", "parent_id": "main", "task": "check the plan", "solution_id": "the-mind"}
    assert started[1]["parent_id"] == "a1" and "solution_id" not in started[1]
    assert {"type": "agent-ended", "agent_id": "a2", "state": "met"} in of(sent, "agent")
    shapes = of(sent, "structure")
    assert len(shapes) == 1 and shapes[0]["tree"][0]["solution"] == "the-mind"      # the first tick; the next two came inside the gap
    facts.clock.now = 10.0
    facts.take(delegate("a3", "write the notes", parent="a1"))
    assert len(of(sent, "structure")) == 2
    facts.clock.now = 20.0
    facts.take(stream(type="system", subtype="init", session_id="s1"))          # nothing about the tree changed
    assert len(of(sent, "structure")) == 2


def test_an_agent_ended_after_the_structure_names_the_solution_of_later_helpers():
    facts, sent = relay()
    facts.take(delegate("a1", "[the-mind] check the plan"))
    facts.clock.now = 5.0
    facts.take(delegate("a2", "[the-mind] check again"))
    started = [d for d in of(sent, "agent") if d["type"] == "agent-started"]
    assert started[1]["solution_name"] == "the-mind"          # the name the structure view gave it (the id when the records do not know it)


def test_spend_and_cost_go_out_only_when_reported_changed_and_after_their_gap_and_a_missing_cost_is_never_zero():
    facts, sent = relay()
    facts.take(tokens(None, tokens=500, turns=2))
    assert of(sent, "spend") == [] and of(sent, "tokens") == []          # no cost reported: nothing about money
    facts.take(tokens(1.25, tokens=900, turns=3, ceiling_usd=5.0, ceiling_seconds=3600.0))
    assert of(sent, "tokens") == []          # the raw tick above already carried the cost; it is not sent twice
    assert of(sent, "spend") == [{"spend_usd": 1.25, "elapsed_seconds": 0.0, "ceiling_usd": 5.0, "ceiling_seconds": 3600.0}]
    facts.clock.now = 1.0
    facts.take(tokens(1.5, tokens=950, turns=4, ceiling_usd=5.0, ceiling_seconds=3600.0))      # inside the gap
    assert len(of(sent, "spend")) == 1
    facts.clock.now = 6.0
    facts.take(tokens(1.5, tokens=950, turns=4, ceiling_usd=5.0, ceiling_seconds=3600.0))
    assert of(sent, "spend")[-1]["spend_usd"] == 1.5 and of(sent, "spend")[-1]["elapsed_seconds"] == 6.0
    facts.clock.now = 30.0
    facts.take(tokens(1.5, tokens=950, turns=4))          # unchanged: not sent again
    assert len(of(sent, "spend")) == 2


def test_a_claude_code_stream_that_reports_its_cost_gets_a_cost_tick_and_a_spend():
    facts, sent = relay()
    facts.take(stream(type="result", total_cost_usd=0.42, num_turns=7, usage={"input_tokens": 100, "output_tokens": 50}))
    assert of(sent, "tokens") == [{"cost_usd": 0.42, "calls": 7}]
    assert of(sent, "spend") == [{"spend_usd": 0.42, "elapsed_seconds": 0.0}]


def test_a_run_with_no_ceiling_sends_no_ceiling_key():
    facts, sent = relay()
    facts.take(tokens(0.4, tokens=10, turns=1))
    assert of(sent, "spend") == [{"spend_usd": 0.4, "elapsed_seconds": 0.0}]


def test_the_hubs_own_decision_to_go_on_is_a_stop_decision_tick():
    facts, sent = relay()
    facts.take(ProgressEvent(type="status", text="The hub's code check found 2 things; resumed.", data={"phase": "hub_resume", "resumes": 1, "reason": "code_finding"}))
    assert of(sent, "stop_decision") == [{"continue": True, "decision": "continue", "reason": "code_finding"}]


def test_chatter_and_a_sink_that_fails_never_stop_the_run():
    sent: List[Any] = []

    def broken(event: Any) -> None:
        sent.append(event)
        raise RuntimeError("stream dropped")

    facts = RunFacts(broken, clock=Clock())
    facts.take(ProgressEvent(type="exec", text="reading", data={"phase": "heartbeat"}))
    facts.take(delegate("a1", "do it"))
    facts.take("not an event")
    assert any(e.type == "agent" for e in sent)


# --- the end -----------------------------------------------------------------------------------------

def limit_result(**more: Any) -> DeepResult:
    res = DeepResult(met=False, session_id="sess-9", verdict_next_action="Read the last two sources.", **more)
    res.end_reason, res.spend_usd, res.elapsed_seconds, res.resume_handle = "stopped_at_hard_limit:spend", 20.5, 900.0, "sess-9"        # type: ignore[attr-defined]
    res.outside_folder = ["Write wrote /etc/x", "Write wrote /etc/x"]                                                                   # type: ignore[attr-defined]
    return res


def test_a_run_stopped_at_its_limit_ends_with_every_fact_it_knows_and_offers_the_session():
    facts, sent = relay()
    facts.take(tokens(16.0, tokens=1, turns=1, ceiling_usd=20.0))
    result = limit_result()
    ended = facts.finish([result])
    assert ended == {"end_reason": "stopped_at_hard_limit:spend", "resume_handle": "sess-9", "spend_usd": 20.5, "elapsed_seconds": 900.0, "ceiling_usd": 20.0,
                     "outside_folder": ["Write wrote /etc/x"], "left": "Read the last two sources.",
                     "stop_decision": {"continue": False, "reason": "ceiling_spend", "left": "Read the last two sources."}}
    assert of(sent, "stop_decision")[-1]["reason"] == "ceiling_spend" and of(sent, "spend")[-1]["spend_usd"] == 20.5


def test_a_finished_run_offers_no_resume_and_an_unreported_spend_is_left_out():
    done = DeepResult(met=True, session_id="sess-1")
    assert end_facts([done]) == {}
    parked = DeepResult(met=False, session_id="sess-2", decision_id="dec_1")
    assert "resume_handle" not in end_facts([parked])                  # the person's answer resumes it
    assert "spend_usd" not in end_facts([DeepResult(met=False, session_id="s")])            # cost_usd 0.0 is the default, not a report
    assert end_facts([DeepResult(met=False, session_id="s", cost_usd=0.3)])["spend_usd"] == 0.3


def test_a_stop_decision_is_made_only_for_an_end_that_names_one():
    assert stop_decision_of("resume_stopped:resume_limit") == {"continue": False, "reason": "resume_limit"}
    assert stop_decision_of("stopped_at_hard_limit:time") == {"continue": False, "reason": "ceiling_time"}
    assert stop_decision_of("wrapped_up_at_soft_limit") is None and stop_decision_of("met") is None


# --- the backend's own validator -----------------------------------------------------------------------

def backend_validator():
    """The backend's validator, loaded by file with its package ``__init__``s left out (they import the whole app); skipped when the checkout is absent."""
    root = Path(__file__).resolve().parents[2] / "quest-backend"
    folder = root / "app" / "business" / "quests"
    if not (folder / "run_state_facts.py").is_file():
        pytest.skip("the sibling Quest backend checkout is not here")
    import types
    added = []
    try:
        for name, where in (("app", root / "app"), ("app.business", root / "app" / "business"), ("app.business.quests", folder)):
            if name not in sys.modules:
                package = types.ModuleType(name)
                package.__path__ = [str(where)]
                sys.modules[name] = package
                added.append(name)
        for name in ("task_run_facts", "run_state_facts"):
            spec = importlib.util.spec_from_file_location(f"app.business.quests.{name}", folder / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            added.append(spec.name)
            spec.loader.exec_module(module)
        return sys.modules["app.business.quests.run_state_facts"]
    except Exception:  # noqa: BLE001
        pytest.skip("the backend validator could not be loaded on its own")
    finally:
        for name in added:
            sys.modules.pop(name, None)


def test_every_tick_and_end_fact_comes_back_from_the_backends_validator_unchanged():
    check = backend_validator()
    facts, sent = relay()
    facts.take(delegate("a1", "[the-mind] check the plan"))
    facts.take(tokens(16.0, tokens=1, turns=1, ceiling_usd=20.0, ceiling_seconds=3600.0))
    facts.take(ProgressEvent(type="status", text="hub", data={"phase": "hub_resume", "reason": "code_finding"}))
    ended = facts.finish([limit_result()])
    for event in sent:
        if event.type in ("structure", "spend", "stop_decision"):
            accepted, cleaned = check.clean_tick(event.type, event.data)
            assert accepted, event.type
            if event.type != "structure":
                assert cleaned == event.data, (event.type, cleaned, event.data)
            else:
                assert cleaned["tree"][0]["solution"] == "the-mind" and "truncated" not in cleaned
    assert check.with_end_facts(dict(ended)) == ended


# --- a whole turn: chat events and task ticks ------------------------------------------------------------

PLAN = {"action": "deep", "goal": "Do the work", "deep_subtasks": [{"goal": "Implement feature X", "brief": "implement X"}], "rationale": "deep"}


class ScriptedWorker:
    """A deep runner that reports like the hub's: helpers on its stream, spend with ceilings, a soft limit, then ends at the limit."""
    emits_session = True

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None, max_turns: Optional[int] = None, emit: Any = None,
                 context_preamble: Optional[str] = None, resume_session_id: Optional[str] = None) -> DeepResult:
        self.calls.append({"goal": goal, "resume_session_id": resume_session_id})
        if emit is not None:
            emit(delegate("a1", "[the-mind] check the plan"))
            emit(tokens(16.5, tokens=1000, turns=5, ceiling_usd=20.0, ceiling_seconds=3600.0))
            emit(ProgressEvent(type="status", text="The soft limit is reached.", data={"phase": "soft_limit", "ceiling_usd": 20.0}))
            emit(ProgressEvent(type="status", text="Outside.", data={"phase": "outside_folder", "items": ["Write wrote /etc/x"]}))
        res = DeepResult(met=False, output="Two sources are not read yet.", session_id="sess-9")
        res.end_reason, res.spend_usd, res.elapsed_seconds, res.resume_handle = "wrapped_up_at_soft_limit", 16.5, 900.0, "sess-9"      # type: ignore[attr-defined]
        res.outside_folder = ["Write wrote /etc/x"]                                                                                    # type: ignore[attr-defined]
        return res


def orchestrator(worker: Any) -> Orchestrator:
    provider = ScriptedProvider(plans=[PLAN, PLAN], verdicts=[{"met": False, "reason": "two sources are not read", "next_action": "read the last two"}] * 6)
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), deep_runner=worker,
                        config=OrchestratorConfig(deep_max_turns=30, deep_goal_max_iterations=1))


def test_a_chat_turn_streams_helpers_structure_spend_and_ends_with_the_facts_and_a_resume_continues_the_session():
    frames: List[Dict[str, Any]] = []
    worker = ScriptedWorker()
    orch = orchestrator(worker)
    orch.run("grow the list", conv_id="conv-1", sink=StreamSink(frames.append), mode=Mode.LIVE)
    kinds = [f["type"] for f in frames]
    for kind in ("agent", "structure", "spend", "status", "result"):
        assert kind in kinds, kind
    spend = [f["data"] for f in frames if f["type"] == "spend"][-1]
    assert spend["spend_usd"] == 16.5 and spend["ceiling_usd"] == 20.0
    phases = [f["data"]["phase"] for f in frames if f["type"] == "status" and (f.get("data") or {}).get("phase")]
    assert phases == ["soft_limit", "outside_folder"]
    result = [f for f in frames if f["type"] == "result"][-1]["data"]
    assert result["end_reason"] == "wrapped_up_at_soft_limit" and result["resume_handle"] == "sess-9"
    assert result["outside_folder"] == ["Write wrote /etc/x"] and result["spend_usd"] == 16.5 and "left" not in result      # a run stopped at a limit is not verified, so nothing says what is left
    assert worker.calls[0]["resume_session_id"] is None
    orch.run("Continue", conv_id="conv-1", sink=StreamSink(frames.append), mode=Mode.LIVE)          # the person's Resume
    assert worker.calls[1]["resume_session_id"] == "sess-9"


def test_a_resume_in_another_conversation_or_with_other_words_starts_fresh():
    worker = ScriptedWorker()
    orch = orchestrator(worker)
    orch.run("grow the list", conv_id="conv-a")
    orch.run("Continue", conv_id="conv-b")
    orch.run("Continue with the budget section of the plan please, and then the summary", conv_id="conv-a")
    assert [c["resume_session_id"] for c in worker.calls[1:]] == [None, None]


def test_a_resume_after_a_restart_uses_the_handle_the_host_stored_with_the_reply():
    worker = ScriptedWorker()
    orch = orchestrator(worker)                     # a fresh process: nothing in the in-memory ledger for this conversation
    orch.run("Continue", conv_id="conv-restarted", context_meta={"available_resume_handle": "sess-stored"})
    assert worker.calls[0]["resume_session_id"] == "sess-stored"
    orch.run("What is the status?", conv_id="conv-restarted-2", context_meta={"available_resume_handle": "sess-stored"})
    assert worker.calls[1]["resume_session_id"] is None        # only the person's resume request uses it


class RecordingClient:
    def __init__(self) -> None:
        self.ticks: List[Dict[str, Any]] = []
        self.reports: List[tuple] = []

    def report_progress(self, task_id, kind, *, text=None, output=None, data=None):
        self.ticks.append({"kind": kind, "text": text, "data": data})

    def report_incomplete(self, task_id, result, session_id=None, result_data=None):
        self.reports.append(("incomplete", result, session_id, result_data))

    def report_done(self, task_id, result, session_id=None, result_data=None):
        self.reports.append(("done", result, session_id, result_data))

    def report_failed(self, task_id, message, session_id=None, result_data=None):
        self.reports.append(("failed", message, session_id, result_data))

    def __getattr__(self, name):
        return lambda *a, **k: {}


def test_a_task_posts_the_ticks_and_reports_the_end_facts_with_its_terminal_status():
    """The dry run: prints the ticks that would be posted (use ``pytest -s``); the client is a fake, nothing is written anywhere."""
    client = RecordingClient()
    outcome = TaskExecutor(client, orchestrator(ScriptedWorker())).execute({"task_id": "task_1", "text": "grow the list"})
    posted = [t for t in client.ticks if t["kind"] in ("agent", "structure", "spend", "tokens", "status", "stop_decision")]
    for tick in posted:
        print("tick", tick["kind"], json.dumps(tick["data"], sort_keys=True))
    assert {"agent", "structure", "spend", "tokens"} <= {t["kind"] for t in posted}
    assert next(t for t in posted if t["kind"] == "tokens" and "cost_usd" in (t["data"] or {}))["data"]["cost_usd"] == 16.5
    assert len([t for t in posted if t["kind"] == "tokens" and "cost_usd" in (t["data"] or {})]) == 1
    status, _, session_id, data = client.reports[-1]
    print("terminal", status, json.dumps(data, sort_keys=True))
    assert session_id == "sess-9"
    assert data["end_reason"] == "wrapped_up_at_soft_limit" and data["resume_handle"] == "sess-9" and data["spend_usd"] == 16.5
    assert data["ceiling_usd"] == 20.0 and data["outside_folder"] == ["Write wrote /etc/x"] and "left" not in data
    assert outcome.task_id == "task_1"


# --- Resume --------------------------------------------------------------------------------------------

def test_a_resume_is_recognised_from_the_persons_short_message_only():
    for text in ("Continue", "continue.", "Please continue from where you stopped.", "  Resume ", "keep going"):
        assert resume_request.is_resume_request(text), text
    for text in ("", "Continue with the budget section of the plan, please", "do not continue", "what is left?"):
        assert not resume_request.is_resume_request(text), text
    assert resume_request.is_resume_request("Weiter", extra=["weiter"])


def test_the_ledger_hands_a_handle_back_once_and_forgets_a_run_that_finished():
    ledger = resume_request.ResumeLedger(size=2)
    ledger.note("c1", {"resume_handle": "s1", "left": "x"})
    assert ledger.take("c1") == {"handle": "s1", "left": "x", "end_reason": ""} and ledger.take("c1") is None
    ledger.note("c1", {"resume_handle": "s1"})
    ledger.note("c1", {})
    assert ledger.take("c1") is None
    for key in ("a", "b", "c"):
        ledger.note(key, {"resume_handle": key})
    assert ledger.take("a") is None and ledger.take("c")["handle"] == "c"


class CapturingOrchestrator:
    def __init__(self) -> None:
        self.kwargs: Dict[str, Any] = {}
        self.brief = ""

    def run(self, brief, **kwargs):
        self.brief, self.kwargs = brief, kwargs
        return OrchestratorResult(kind="answer", text="ok")


def execute(task: Dict[str, Any], client: Optional[RecordingClient] = None):
    orch, client = CapturingOrchestrator(), client or RecordingClient()
    TaskExecutor(client, orch).execute({"task_id": "task_2", "text": "grow the list", **task})
    return orch, client


def test_a_task_resume_continues_the_handle_quest_hands_back_and_is_told_what_was_left():
    earlier = {"resume_handle": "sess-9", "left": "read the last two", "end_reason": "wrapped_up_at_soft_limit"}
    orch, client = execute({"text": "Please continue from where you stopped.", "resume_session_id": "sess-7", "result_data": earlier})
    assert orch.kwargs["resume_session_id"] == "sess-7"
    assert "continue this same session" in orch.brief and "read the last two" in orch.brief and "wrapped up at soft limit" in orch.brief
    assert any(t["kind"] == "status" and "Resuming" in (t["text"] or "") for t in client.ticks)


def test_a_task_resume_falls_back_to_the_handle_its_earlier_report_carried():
    orch, _ = execute({"text": "Continue", "result_data": {"resume_handle": "sess-9"}})
    assert orch.kwargs["resume_session_id"] == "sess-9"


def test_a_resume_with_no_handle_is_not_claimed_and_an_ordinary_task_is_untouched():
    orch, _ = execute({"text": "Continue"})
    assert orch.kwargs["resume_session_id"] is None and "RESUME" not in orch.brief
    orch, _ = execute({"text": "grow the list", "resume_session_id": "sess-7", "result_data": {"resume_handle": "sess-9"}})
    assert orch.kwargs["resume_session_id"] == "sess-7" and "RESUME" not in orch.brief
