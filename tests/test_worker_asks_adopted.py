"""A deep worker that files its own decision-request is adopted by the runner, and a deep result that asks the person files one.

Two defects of the second real tier (2026-11-01), pinned offline. (1) A worker running out of process files its ask through the Quest
decisions endpoint with its own credentials: the row stayed unassigned, was not deduped and the asker was not told, and the backend has no
reassign route. The runner re-files it through its own ask path (turn dedupe key, assignee checked against the participants, asker told) and
then withdraws the worker's row. (2) The verifier's structured ``asks_person`` flag now also covers the deep path: a met worker final that
ends by asking the person, and a deep run whose verdict asks, file the ask the same way the chat path does.
Fully offline: scripted providers, an in-memory sink that keeps rows like the backend does.
"""
from __future__ import annotations

from typing import Any, Dict, FrozenSet, List

from hubofhubs.the_world.tools.deep import DeepResult
from hubofhubs.the_world.tools.events import EVENT_DECISION
from quest_ai_runner.core.adapters_sinks import StreamSink
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.core.worker_ask_adoption import adopt_worker_asks

from .conftest import StubRetrieval
from .test_done_evidence import ANALYSIS, Provider

PEOPLE = [{"user_id": "u1", "name": "Ana"}, {"user_id": "u2", "name": "Bram"}]
META = {"asker_user_id": "u1", "user_id": "u1", "conv_id": "c1", "turn_id": "t1", "participants": PEOPLE}
QUEST = "q1"


class RowSink:
    """The backend's contract in memory: rows with ids, one open row per dedupe key, withdraw leaves the history."""

    def __init__(self):
        self.rows: List[Dict[str, Any]] = []
        self.sent: List[Any] = []
        self.withdrawn: List[str] = []

    def escalate(self, escalation):
        self.sent.append(escalation)
        for row in self.rows:
            if row["status"] == "open" and escalation.dedupe_key and row.get("dedupe_key") == escalation.dedupe_key:
                return row["decision_id"]
        row = {"decision_id": "d-%d" % (len(self.rows) + 1), "status": "open", "summary": escalation.summary, "assigned_to": escalation.assignee,
               "dedupe_key": escalation.dedupe_key, "notify_user_id": escalation.notify_user_id}
        self.rows.append(row)
        return row["decision_id"]

    def worker_files(self, summary, assigned_to=None):
        """What the out-of-process worker does with its own credentials: no dedupe key, no notify, any assignee."""
        row = {"decision_id": "d-%d" % (len(self.rows) + 1), "status": "open", "summary": summary, "assigned_to": assigned_to,
               "dedupe_key": None, "notify_user_id": None}
        self.rows.append(row)
        return row["decision_id"]

    def open_decision_ids_for_quest(self, quest_id) -> FrozenSet[str]:
        return frozenset(r["decision_id"] for r in self.rows if r["status"] == "open")

    def open_decisions_for_quest(self, quest_id) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.rows if r["status"] == "open"]

    def withdraw_decision(self, decision_id):
        for row in self.rows:
            if row["decision_id"] == decision_id and row["status"] == "open":
                row["status"] = "withdrawn"
                self.withdrawn.append(decision_id)
                return True
        return False

    def open_rows(self):
        return [r for r in self.rows if r["status"] == "open"]


class FilingRunner:
    """A deep runner that files a decision-request itself while it works, then returns a scripted result."""

    def __init__(self, sink, result, summary="Please approve 350 USD", assigned_to="u2"):
        self.sink, self.result, self.summary, self.assigned_to, self.runs = sink, result, summary, assigned_to, 0

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None):  # noqa: ARG002
        self.runs += 1
        if self.summary:
            self.sink.worker_files(self.summary, self.assigned_to)
        return self.result


class ScriptedRunner:
    def __init__(self, results):
        self.results, self.runs = list(results), 0

    def run_goal(self, *, goal, brief, model=None, max_turns=None, context_preamble=None):  # noqa: ARG002
        self.runs += 1
        return self.results.pop(0) if self.results else DeepResult(met=True, output="done")


def build(provider, runner, sink, **cfg):
    cfg.setdefault("deep_goal_max_iterations", 3)
    cfg.setdefault("interim_replies", True)
    config = OrchestratorConfig(**cfg)
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=config,
                        deep_runner=runner, escalation=sink)


def run(orch, meta=META):
    events: List[Dict[str, Any]] = []
    result = orch.run("book the photographer", sink=StreamSink(events.append), quest_id=QUEST, context_meta=dict(meta))
    return result, events


# ---- (1) the worker's own row is adopted by the runner

def test_a_row_the_worker_filed_is_refiled_through_the_runner_path_and_the_workers_row_withdrawn():
    sink = RowSink()
    runner = FilingRunner(sink, DeepResult(met=True, output="Drafted the request."))
    result, events = run(build(Provider([dict(ANALYSIS)], verdicts=[{"met": True}]), runner, sink))
    mine = sink.open_rows()
    assert len(mine) == 1 and mine[0]["dedupe_key"] == "turn-ask:t1" and mine[0]["notify_user_id"] == "u1"
    assert mine[0]["assigned_to"] == "u2"                           # a participant other than the asker keeps the teammate
    assert sink.withdrawn == ["d-1"]
    asks = [e for e in events if e["type"] == EVENT_DECISION]
    assert len(asks) == 1 and asks[0]["decision_id"] == mine[0]["decision_id"] and asks[0]["data"]["asker_user_id"] == "u1"
    assert result.deep_results[0].decision_id == mine[0]["decision_id"]


def test_a_worker_row_addressed_to_a_stranger_or_nobody_goes_to_the_asker():
    for assigned in ("u9", None):
        sink = RowSink()
        run(build(Provider([dict(ANALYSIS)], verdicts=[{"met": True}]), FilingRunner(sink, DeepResult(met=True, output="x"), assigned_to=assigned), sink))
        rows = sink.open_rows()
        assert len(rows) == 1 and rows[0]["assigned_to"] == "u1" and rows[0]["dedupe_key"] == "turn-ask:t1"


def test_two_rows_from_one_worker_collapse_into_the_turns_one_ask():
    sink = RowSink()
    sink.worker_files("an older ask on the quest", "u1")
    before = sink.open_decision_ids_for_quest(QUEST)
    sink.worker_files("first", "u1")
    sink.worker_files("second", "u2")
    ledger: List[Dict[str, Any]] = []
    adopted = adopt_worker_asks(sink, quest_id=QUEST, before=before, meta=dict(META, turn_asks=ledger))
    assert len(adopted) == 1 and len(ledger) == 1
    open_keys = [r["dedupe_key"] for r in sink.open_rows()]
    assert open_keys.count("turn-ask:t1") == 1 and sink.withdrawn == ["d-2", "d-3"]
    assert [r for r in sink.open_rows() if r["summary"] == "an older ask on the quest"]       # not this turn's, left alone


def test_a_row_that_cannot_be_refiled_is_left_for_the_person_and_nothing_is_withdrawn():
    sink = RowSink()
    before = sink.open_decision_ids_for_quest(QUEST)
    sink.worker_files("Please approve 350 USD", "u2")

    def down(escalation):
        raise RuntimeError("down")

    sink.escalate = down                                                              # type: ignore[assignment]
    ledger: List[Dict[str, Any]] = []
    assert adopt_worker_asks(sink, quest_id=QUEST, before=before, meta=dict(META, turn_asks=ledger)) == []
    assert sink.withdrawn == [] and len(sink.open_rows()) == 1 and ledger == []


def test_a_sink_without_the_row_or_withdraw_capability_adopts_nothing():
    class Plain:
        def escalate(self, escalation):
            return "d-x"

    assert adopt_worker_asks(Plain(), quest_id=QUEST, before=frozenset(), meta=dict(META, turn_asks=[])) == []
    assert adopt_worker_asks(RowSink(), quest_id=None, before=frozenset(), meta=dict(META, turn_asks=[])) == []


def test_the_quest_sink_lists_rows_and_withdraws_through_the_client():
    from quest_ai_runner.runner.quest_client import QuestDecisionSink

    class Client:
        def __init__(self):
            self.withdrawn = []

        def list_open_decisions_for_quest(self, quest_id):
            return [{"decision_id": "d-1", "summary": "s", "status": "open"}]

        def withdraw_decision(self, decision_id):
            self.withdrawn.append(decision_id)
            return {"ok": True}

    client = Client()
    sink = QuestDecisionSink(client)
    assert [r["decision_id"] for r in sink.open_decisions_for_quest(QUEST)] == ["d-1"]
    assert sink.withdraw_decision("d-1") is True and client.withdrawn == ["d-1"]


# ---- (2) the verifier's asks_person covers the deep path

def test_a_met_deep_final_that_asks_the_person_files_one_ask_and_stays_met():
    sink = RowSink()
    verdict = {"met": True, "asks_person": True, "question": "Which logo do we use?"}
    runner = ScriptedRunner([DeepResult(met=True, output="Draft ready. Which logo do we use?")])
    result, events = run(build(Provider([dict(ANALYSIS)], verdicts=[dict(verdict)]), runner, sink))
    final = result.deep_results[0]
    assert final.met and final.decision_id and final.needs_person == "Which logo do we use?"
    assert final.output == "Draft ready. Which logo do we use?"                     # the answer stays as the worker wrote it
    assert len(sink.open_rows()) == 1 and sink.open_rows()[0]["dedupe_key"] == "turn-ask:t1"
    assert len([e for e in events if e["type"] == EVENT_DECISION]) == 1


def test_a_deep_run_whose_verdict_asks_the_person_stops_and_files_one_ask():
    sink = RowSink()
    verdict = {"met": False, "blocker": "more_work", "asks_person": True, "question": "Which venue do you want?", "reason": "needs a choice"}
    runner = ScriptedRunner([DeepResult(met=False, output="Two venues fit. Which one?"), DeepResult(met=True, output="again")])
    result, events = run(build(Provider([dict(ANALYSIS)], verdicts=[dict(verdict)]), runner, sink))
    assert runner.runs == 1                                                           # another attempt cannot answer the question
    assert result.deep_results[0].decision_id and len(sink.open_rows()) == 1
    assert len([e for e in events if e["type"] == EVENT_DECISION]) == 1


def test_a_deep_final_that_only_offers_help_or_runs_with_the_gate_off_files_nothing():
    sink = RowSink()
    run(build(Provider([dict(ANALYSIS)], verdicts=[{"met": True, "asks_person": False, "question": ""}]), ScriptedRunner([DeepResult(met=True, output="done")]), sink))
    assert not sink.rows
    off = RowSink()
    run(build(Provider([dict(ANALYSIS)], verdicts=[{"met": True, "asks_person": True, "question": "Which logo?"}]),
              ScriptedRunner([DeepResult(met=True, output="done")]), off, interim_replies=False))
    assert not off.rows
