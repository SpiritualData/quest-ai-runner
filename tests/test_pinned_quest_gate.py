"""Offline tests: a chat pinned to one quest never ends in a which-quest question.

A planner clarify on a pinned turn is rewritten by ``answer_pinned_quest`` into an answer on that quest. When the quest's status snapshot
can be read, the reply is written from it; when it cannot, the planner answers from the quest's own records. Nothing is asked of the person.
"""
from types import SimpleNamespace

from quest_ai_runner.intelligence.critical_thinking.esoi_application import status_snapshot
from quest_ai_runner.intelligence.mind.context_intake.orchestrator_run_begin import RunBeginMixin


class _Emit:
    def status(self, *args, **kwargs):
        pass


class _Stub(RunBeginMixin):
    tools = None

    def cheap_decision(self, *args, **kwargs):
        return "status"


def _state():
    return SimpleNamespace(quest_id="quest_pinned", user_message="How is this quest going?", retrieval_constraints={}, gathered=[], trace=None,
                           plan=SimpleNamespace(action="clarify", reply_shape="clarify"))


def test_pinned_clarify_without_snapshot_is_answered_from_records(monkeypatch):
    monkeypatch.setattr(status_snapshot, "build", lambda *a, **k: None)
    state = _state()
    _Stub().answer_pinned_quest(state, {}, _Emit())
    assert state.plan.action == "answer"
    assert state.plan.needs_records is True
    assert getattr(state.plan, "reply_shape", None) != "clarify"
    assert not getattr(state, "status_snapshot", False)


def test_pinned_clarify_with_snapshot_is_answered_from_snapshot(monkeypatch):
    snapshot = {"lead": "Lead", "follow": "Follow", "stems": ["s"], "insight": "Insight", "next_step": False, "records": []}
    monkeypatch.setattr(status_snapshot, "build", lambda *a, **k: snapshot)
    state = _state()
    _Stub().answer_pinned_quest(state, {}, _Emit())
    assert state.plan.action == "answer"
    assert getattr(state.plan, "reply_shape", None) != "clarify"
    assert state.status_snapshot is True
    assert state.status_lead == "Lead"
    assert state.gathered == [snapshot]


def test_a_pinned_run_whose_planner_asks_which_quest_is_answered_on_the_quest(monkeypatch):
    """End to end through the planner loop: the scripted planner chooses a clarify on a chat pinned to one quest. The reply is written from that
    quest's snapshot, never with the clarify directive, so no which-quest question is returned."""
    from quest_ai_runner.consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.intelligence.mind.response_writer.reply_standards import CLARIFY_DIRECTIVE
    from .test_clarify_reply import RecordingProvider
    from .test_done_evidence import build

    snapshot = {"lead": "Lead", "follow": "", "stems": [], "insight": "", "next_step": False, "records": []}
    monkeypatch.setattr(status_snapshot, "reachable_quests", lambda tools: [{"quest_id": "quest_pinned", "title": "Half marathon"}])
    monkeypatch.setattr(status_snapshot, "build", lambda *a, **k: snapshot)
    provider = RecordingProvider([{"action": "clarify", "rationale": "which quest", "reply_shape": "clarify"}], ["Which quest do you mean?"])
    provider.message = "How is this quest going?"
    orch = build(provider)
    from unittest.mock import MagicMock
    tools = MagicMock()
    tools.names.return_value = []
    tools.render_planner_block.return_value = ""
    orch.tools = tools
    events = []
    orch.run("How is this quest going?", sink=StreamSink(events.append), quest_id="quest_pinned")
    replies = [e["text"] for e in events if e.get("type") == "result"]
    writer_calls = [c for c in provider.writer_inputs if c["messages"][-1]["content"] == "How is this quest going?"]
    assert all(CLARIFY_DIRECTIVE not in str(c) for c in writer_calls)
    assert not any(e.get("type") == "decision" for e in events)        # a which-quest clarify is raised as a decision that waits on the person
    assert not any("which quest" in str(r).lower() for r in replies)


def test_pinned_clarify_without_snapshot_still_names_the_quest_to_the_writer(monkeypatch):
    """When no status snapshot can be read, the records answer still gets the pinned quest's name, so the writer never has to ask which quest."""
    monkeypatch.setattr(status_snapshot, "reachable_quests", lambda tools: [{"quest_id": "quest_pinned", "name": "Run my first half marathon",
                                                                           "outcome": "I finish a half marathon in under 2:15"}])
    monkeypatch.setattr(status_snapshot, "build", lambda *a, **k: None)
    state = _state()
    state.gathered = []
    _Stub().answer_pinned_quest(state, {}, _Emit())
    named = [g for g in state.gathered if "Run my first half marathon" in str(g.get("text", ""))]
    assert named and "Never ask which quest" in named[0]["text"]
    assert state.plan.action == "answer" and state.plan.needs_records is True
    assert getattr(state.plan, "reply_shape", None) != "clarify"
