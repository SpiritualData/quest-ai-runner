"""Who raises the ask, and the hub's Judgment Calls on every ask.

Two things a real autopilot run got wrong together: its brief told it to raise one ask and keep working while the closing note of
every deep brief told it to stop and let the runner ask, and no ask on the runner's path was ever put to the hub's Judgment Calls.

  (1) the closing note depends on the kind of run: a thread turn leaves the ask to the runner, standing work raises its own
  (2) the executor marks a task the autopilot composed as standing work, from the task's kind
  (3) the decision sink consults Judgment Calls before it files, files on "ask", still files when the judgment fails, and does
      not file on "proceed" or "decline" (reported, never silent)
  (4) the create_decision tool passes the severity on and tells the worker what the ask's timing means for the rest of its run

All offline: the Quest transport is stubbed, and the hub's judgment runs for real except where a test replaces it.
"""
from __future__ import annotations

from typing import Any, Dict, List

import hubofhubs
import pytest

from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import create_decision_spec
from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient, QuestDecisionSink
from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import (
    ASK_BLOCKS_GUIDANCE, ASK_WAITS_GUIDANCE, Assignee, file_ask, timing_guidance)
from quest_ai_runner.the_consciousness.request_intake.executor import TaskExecutor
from quest_ai_runner.the_intelligence.mind.delegation.orchestrator_deep_run import ask_ownership_note
from quest_ai_runner.the_self.integrity.prompts.auxiliary_prompts import DEEP_ASK_OWNERSHIP_NOTE, STANDING_ASK_NOTE
from quest_ai_runner.the_world.tools.tools import ToolContext
from quest_ai_runner.the_world.tools.tools_status import ToolStatus


# ---- (1) the closing note of a deep brief --------------------------------------------------------------------------------------

def test_a_thread_turn_leaves_the_ask_to_the_runner():
    for meta in ({"asker_user_id": "u1"}, {"requester_user_id": "u1"}):
        note = ask_ownership_note(meta)
        assert note == DEEP_ASK_OWNERSHIP_NOTE and "do not raise a decision-request yourself" in note


def test_standing_work_raises_its_own_ask_and_keeps_working():
    note = ask_ownership_note({"requester_user_id": "u_ai", "standing_work": True})
    assert note == STANDING_ASK_NOTE and DEEP_ASK_OWNERSHIP_NOTE not in note
    assert "create_decision" in note and "does not depend on the answer" in note
    assert "do not raise" not in note and chr(8212) not in note


def test_a_run_for_nobody_gets_no_note():
    assert ask_ownership_note(None) == "" and ask_ownership_note({"task_id": "t1"}) == ""


# ---- (2) the executor marks standing work from the task's kind ------------------------------------------------------------------

def run_meta(task: Dict[str, Any]) -> Dict[str, Any]:
    from unittest.mock import MagicMock
    seen: Dict[str, Any] = {}

    class Orch:
        def run(self, brief, **kw):
            seen.update(kw.get("context_meta") or {})
            raise RuntimeError("stop after the meta is captured")

    client = MagicMock()
    client.get_work_model_config = None
    try:
        TaskExecutor(client, Orch()).execute(task)
    except Exception:  # noqa: BLE001 -- the stub stops the run on purpose
        pass
    return seen


def test_a_task_the_autopilot_composed_runs_as_standing_work_and_any_other_task_does_not():
    base = {"id": "t1", "text": "do it", "goal_id": "quest_1", "created_by": "u_ai", "user_id": "u_owner"}
    composed = run_meta(dict(base, task_kind="autopilot_work"))
    assert composed.get("standing_work") is True and composed.get("requester_user_id") == "u_ai"
    plain = run_meta(base)
    assert plain.get("requester_user_id") == "u_ai" and "standing_work" not in plain


# ---- (3) the sink consults Judgment Calls ---------------------------------------------------------------------------------------

def sink_and_calls(existing_open=None):
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1")
    calls: List[Dict[str, Any]] = []

    def fake_request(method, path, *, params=None, body=None):
        calls.append({"method": method, "path": path, "body": body})
        if path == "/api/teams/decisions/for-quest":
            return list(existing_open if existing_open is not None else [])
        if path == "/api/teams/team_1/decisions":
            return {"decision_id": "dec_new"}
        return {}

    client._request = fake_request  # type: ignore[assignment]
    return QuestDecisionSink(client, default_assignee_user_id="user_default"), calls


def filed(calls) -> List[Dict[str, Any]]:
    return [c for c in calls if c["method"] == "POST" and c["path"] == "/api/teams/team_1/decisions"]


def judging(monkeypatch, answer):
    """Replace the hub's run with one that records what it was asked and answers ``answer`` (an exception is raised)."""
    asked: List[Any] = []

    def run(target, given=None, **kw):
        asked.append((target, given))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(hubofhubs, "run", run)
    return asked


def test_the_sink_asks_the_hubs_judgment_calls_and_files_on_ask(monkeypatch):
    asked = judging(monkeypatch, {"decision": "ask", "to": "human", "timing": {"mode": "async", "batch": True}, "reason": "r", "severity": "medium"})
    sink, calls = sink_and_calls()
    assert sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1", assignee="user_1")) == "dec_new"
    assert asked == [("judgment-calls", {"signal": {"reason": "Approve X?"}, "person": "user_1", "context": {"needs_person": True}})]
    assert len(filed(calls)) == 1 and sink.last_judgment["timing"]["mode"] == "async"


def test_the_real_hub_judgment_times_an_ask_by_its_severity():
    sink, calls = sink_and_calls()
    assert sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1")) == "dec_new"
    assert sink.last_judgment["decision"] == "ask" and sink.last_judgment["timing"]["mode"] == "async"
    assert sink.escalate(Escalation(summary="Sign in to the account", quest_id="quest_1", severity="high")) == "dec_new"
    assert sink.last_judgment["timing"]["mode"] == "sync" and len(filed(calls)) == 2


@pytest.mark.parametrize("answer", [RuntimeError("the hub is not reachable"), None, {"decision": "something else"}])
def test_a_judgment_that_fails_or_is_not_understood_still_files_the_ask(monkeypatch, answer):
    judging(monkeypatch, answer)
    sink, calls = sink_and_calls()
    assert sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1")) == "dec_new"
    assert len(filed(calls)) == 1 and sink.last_judgment == {}


@pytest.mark.parametrize("decision", ["proceed", "decline"])
def test_proceed_or_decline_files_nothing_and_the_caller_is_told_why(monkeypatch, decision):
    judging(monkeypatch, {"decision": decision, "to": "", "timing": {}, "reason": "their own policy", "severity": "low"})
    sink, calls = sink_and_calls()
    assert sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1")) == ""
    assert filed(calls) == [] and sink.last_judgment["decision"] == decision
    outcome = file_ask(sink, question="Approve X?", why_only_you="", done="", options=None, quest_id="quest_1", conv_id="c1",
                       turn_id="t1", asker_user_id="u1", assignee=Assignee("u1", "asker"), summary="Approve X?")
    assert not outcome.filed and filed(calls) == []
    assert decision in outcome.error and "their own policy" in outcome.error


def test_a_reused_open_ask_is_not_judged_again(monkeypatch):
    asked = judging(monkeypatch, {"decision": "ask", "to": "human", "timing": {"mode": "sync"}, "reason": "", "severity": "high"})
    sink, calls = sink_and_calls([{"decision_id": "dec_open", "summary": "Approve X?", "status": "open"}])
    assert sink.escalate(Escalation(summary="Approve X?", quest_id="quest_1")) == "dec_open"
    assert asked == [] and filed(calls) == [] and sink.last_judgment is None


# ---- (4) the worker's tool ------------------------------------------------------------------------------------------------------

class ToolClient:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def list_open_decisions_for_quest(self, quest_id):
        return []

    def create_decision(self, summary, **kw):
        self.calls.append(dict(kw, summary=summary))
        return {"decision_id": "d-%d" % len(self.calls)}


def call_tool(args: Dict[str, Any]):
    client = ToolClient()
    result = create_decision_spec({}, client_factory=lambda: client).handler(
        dict(args, quest_id="quest_1"), ToolContext(user_id="u1", meta={"asker_user_id": "u1", "quest_id": "quest_1"}))
    return result, client


def test_timing_guidance_is_read_from_the_timing():
    assert timing_guidance({"mode": "async", "batch": True}) == ASK_WAITS_GUIDANCE
    assert timing_guidance({"mode": "sync"}) == ASK_BLOCKS_GUIDANCE
    assert timing_guidance({}) == "" and timing_guidance(None) == ""


def test_an_ask_that_waits_tells_the_worker_to_continue():
    result, client = call_tool({"summary": "Please review the wording"})
    assert result.ok and len(client.calls) == 1
    assert "Raised ask d-1" in result.text and result.text.endswith(ASK_WAITS_GUIDANCE)


def test_a_high_severity_ask_tells_the_worker_it_blocks():
    result, client = call_tool({"summary": "Sign in to the account", "severity": "high"})
    assert result.ok and len(client.calls) == 1
    assert result.text.endswith(ASK_BLOCKS_GUIDANCE) and ASK_WAITS_GUIDANCE not in result.text


def test_the_tool_declares_severity():
    severity = create_decision_spec({}, client_factory=ToolClient).parameters["properties"]["severity"]
    assert severity["enum"] == ["medium", "high"] and "nothing in this run can proceed" in severity["description"]


@pytest.mark.parametrize("decision", ["proceed", "decline"])
def test_the_tool_raises_nothing_on_proceed_or_decline_and_says_so(monkeypatch, decision):
    judging(monkeypatch, {"decision": decision, "to": "", "timing": {}, "reason": "their own policy", "severity": "low"})
    result, client = call_tool({"summary": "Approve X?"})
    assert client.calls == [] and not result.ok and result.status is ToolStatus.REFUSED
    assert result.text.startswith("Not raised") and "their own policy" in result.text


def test_the_tool_still_raises_the_ask_when_the_judgment_fails(monkeypatch):
    judging(monkeypatch, RuntimeError("the hub is not reachable"))
    result, client = call_tool({"summary": "Approve X?"})
    assert result.ok and len(client.calls) == 1 and result.text == "Raised ask d-1 for the person on quest quest_1."
