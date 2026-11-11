"""The runner owns the turn's ask, a final that needs the person files one, and a shared thread's reply is not written from private records.

Three real defects of the second real tier (2026-11-01), each pinned offline: a reply said "I've sent X a request" while the ask was filed
by the worker's own tool to someone else and never shown; a final that put its question in plain text left nothing to track; a final in a
shared thread quoted the asker's private record. Fully offline: scripted providers, in-memory sinks and clients.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

from hubofhubs.api import EVENT_DECISION, EVENT_RESULT
from quest_ai_runner.the_intelligence.mind.context_intake import shared_thread
from quest_ai_runner.the_intelligence.mind.response_writer import work_trace
from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
from quest_ai_runner.the_consciousness.judgment_calls.ask_policy import ask_state_block, record_turn_ask
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_turn_messages import records_text
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import create_decision_spec

from .conftest import StubRetrieval
from .test_done_evidence import LOOKUP, Provider

PEOPLE = [{"user_id": "u1", "name": "Ana"}, {"user_id": "u2", "name": "Bram"}]
META = {"asker_user_id": "u1", "user_id": "u1", "conv_id": "c1", "turn_id": "t1", "participants": PEOPLE}


class Client:
    """What the create_decision tool talks to: the backend's contract in memory (one open ask per dedupe key)."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def list_open_decisions_for_quest(self, quest_id):
        return []

    def list_assignable_members(self, quest_id):
        return [dict(p) for p in PEOPLE]

    def create_decision(self, summary, **kw):
        self.calls.append(dict(kw, summary=summary))
        return {"decision_id": "d-%d" % len(self.calls)}


class RecordingProvider(Provider):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.system: List[str] = []

    def answer(self, messages, *, model, system=None):  # noqa: ARG002
        self.system.append(str(system) + "\n" + "\n".join(str(m) for m in messages))
        return "I've sent Chidi Eze a request."


def build(provider, tools=None, **cfg):
    cfg.setdefault("deep_goal_max_iterations", 4)
    config = OrchestratorConfig(**cfg)
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), config=config,
                        tools=tools, escalation=Sink())


class Sink:
    def __init__(self):
        self.sent, self.rows = [], {}

    def escalate(self, escalation):
        self.sent.append(escalation)
        return self.rows.setdefault(escalation.dedupe_key, "s-%d" % (len(self.rows) + 1))


def run(orch, meta=META) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    # nobody reads this thread live: the question is filed as an ask (a live chat asks it in the reply, see test_inline_ask_and_landed_receipt)
    orch.run("book the photographer", sink=StreamSink(events.append), context_meta=dict(meta, live_user_turn=False))
    return events


# ---- (1) the runner owns the ask

def test_the_judge_input_accepts_a_list_of_gathered_reads():
    text = records_text("rendered context", [{"text": "a note"}, {"hits": "a hit"}, "plain read"])
    assert "rendered context" in text and "a note" in text and "a hit" in text and "plain read" in text


def test_the_tool_files_under_the_turns_key_and_notes_the_ask_in_the_turn_ledger():
    client, ledger = Client(), []
    spec = create_decision_spec({}, client_factory=lambda: client)
    result = spec.handler({"summary": "Please approve 350 USD", "assignee": "Chidi Eze"},
                          ToolContext(user_id="u1", meta=dict(META, turn_asks=ledger)))
    assert result.ok
    assert client.calls[0]["dedupe_key"] == "turn-ask:t1" and client.calls[0]["assignee_user_id"] == "u1"   # a stranger: the asker
    assert ledger == [{"decision_id": "d-1", "assignee_user_id": "u1", "summary": "Please approve 350 USD"}]


def test_a_worker_raised_ask_becomes_the_turns_one_shown_ask_and_the_reply_claims_nothing_else():
    client = Client()
    tools = ToolRegistry([create_decision_spec({}, client_factory=lambda: client)])
    provider = RecordingProvider([
        {"action": "tool", "rationale": "ask", "tool_calls": [{"name": "create_decision", "args": {"summary": "Please approve 350 USD", "assignee": "Chidi Eze"}}]},
        dict(LOOKUP)], verdicts=[{"met": True}])
    events = run(build(provider, tools, interim_replies=True, answer_goal_max_iterations=3))
    asks = [e for e in events if e["type"] == EVENT_DECISION]
    assert len(asks) == 1 and asks[0]["decision_id"] == "d-1"
    assert asks[0]["data"]["assigned_to"] == "u1" and asks[0]["data"]["asker_user_id"] == "u1"   # Chidi is no participant: the asker
    final = [e for e in events if e["type"] == EVENT_RESULT][-1]["text"]
    assert "sent Chidi Eze a request" not in final and "Please approve 350 USD" in final and "Chidi" not in final
    assert len(client.calls) == 1


def test_the_reply_is_written_from_the_fact_that_no_ask_exists_or_that_one_does():
    assert "No decision-request exists" in ask_state_block([], PEOPLE, "u1")
    ledger: List[Dict[str, Any]] = []
    record_turn_ask(ledger, decision_id="d-1", assignee_user_id="u2", summary="x")
    assert "addressed to Bram" in ask_state_block(ledger, PEOPLE, "u1")
    ledger[0]["assignee_user_id"] = "u9"
    assert "addressed to the person who asked" in ask_state_block(ledger, PEOPLE, "u1")
    provider = RecordingProvider([dict(LOOKUP)], verdicts=[{"met": True}])
    run(build(provider, None, interim_replies=True, answer_goal_max_iterations=3))
    assert any("No decision-request exists for this turn" in s for s in provider.system)
    off = RecordingProvider([dict(LOOKUP)], verdicts=[{"met": True}])
    run(build(off, None, interim_replies=False))
    assert not any("ASK STATE" in s for s in off.system)


# ---- (2) a final that needs the person files the ask

def test_a_met_final_that_asks_the_person_for_something_files_one_ask_and_keeps_the_answer():
    """The ask is filed with streaming on or off: ``interim_replies`` controls streaming only (the sd-shared lane has it off)."""
    verdict = {"met": True, "asks_person": True, "question": "Do we accept the logo condition?"}
    for switched_on in (True, False):
        provider = RecordingProvider([dict(LOOKUP)], verdicts=[dict(verdict)])
        orch = build(provider, None, interim_replies=switched_on, answer_goal_max_iterations=3)
        events = run(orch)
        asks = [e for e in events if e["type"] == EVENT_DECISION]
        assert len(asks) == 1 and asks[0]["data"]["question"] == "Do we accept the logo condition?"
        assert asks[0]["data"]["assigned_to"] == "u1" and len(orch.escalation.sent) == 1
        assert [e for e in events if e["type"] == EVENT_RESULT][-1]["text"] == "I've sent Chidi Eze a request."


def test_a_met_final_that_only_offers_help_files_nothing():
    provider = RecordingProvider([dict(LOOKUP)], verdicts=[{"met": True, "asks_person": False, "question": ""}])
    orch = build(provider, None, interim_replies=True, answer_goal_max_iterations=3)
    assert not [e for e in run(orch) if e["type"] == EVENT_DECISION] and not orch.escalation.sent


# ---- (3) a shared thread's reply is not written from the asker's private records

def cards():
    return [{"id": "marathon", "title": "Run a marathon in April", "adapter": "cards", "rendered_section": "### Card: Run a marathon in April\nWeek 6 of 16, left knee sore after hills, physio on Thursday",
             "items": []},
            {"id": "club", "title": "Club banquet", "adapter": "cards", "visible_to_all": True, "rendered_section": "### Card: Club banquet\nHarbour Hall hold until Friday", "items": []},
            {"id": "turn-1", "title": "a turn", "adapter": "turn", "rendered_section": "earlier turn", "items": []}]


def test_only_records_visible_to_everyone_stay_in_a_shared_threads_context():
    meta = {"work_trace": {"audience": "shared"}}
    assert shared_thread.is_shared_thread(meta) and not shared_thread.is_shared_thread({"work_trace": {"audience": "owner"}})
    private = shared_thread.private_cards(cards(), meta)
    assert [c["title"] for c in private] == ["Run a marathon in April"]
    view = "\n".join(c["rendered_section"] for c in cards())
    stripped = shared_thread.strip_private_sections(view, private)
    assert "left knee" not in stripped and "Harbour Hall" in stripped
    assert [c["title"] for c in shared_thread.private_cards(cards(), dict(meta, visible_card_ids=["marathon"]))] == []


def test_a_sentence_that_draws_on_a_private_record_is_dropped_by_overlap_with_its_stored_text():
    work_trace.set_word_rarity(lambda word: word in {"marathon", "april", "knee", "sore", "physio", "hills"})
    try:
        private = shared_thread.private_cards(cards(), {"work_trace": {"audience": "shared"}})
        text = "A shared Sunday session fits your April marathon build, and your sore left knee will shape the distance.\nMeet at the hall at nine."
        assert shared_thread.scrub_private_sentences(text, private) == "Meet at the hall at nine."
        clean = "Which start time and meeting point should the club use?"
        assert shared_thread.scrub_private_sentences(clean, private) == clean
    finally:
        work_trace.set_word_rarity(lambda word: False)


# ---- the metric

def real_case(**over):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation" / "components"))
    from interim_reply import live_turns
    case = dict(id="X", name="x", arm="new", surface="group", audience="shared", message="m", expect_ask=True, expect_interim="maybe",
                messages=[{"kind": "final", "text": "ok"}], log=[], asks_filed=[], callers=[], private_titles=[], events=[])
    case.update(over)
    return live_turns, case


def test_the_metric_requires_an_ask_when_one_is_expected_and_reads_the_final_for_leaks():
    live, case = real_case()
    assert "no ask on a turn that needed the person" in live.check(case)
    live, case = real_case(expect_ask="maybe", private_titles=["Run a marathon in April"],
                           messages=[{"kind": "final", "text": "Your Run a marathon in April plan fits."}])
    assert "a private card's text reached an interim, an ask or the final in the shared thread" in live.check(case)
    live, case = real_case(expect_ask="maybe", private_titles=["Run a marathon in April"])
    assert not live.check(case)


def test_the_metric_accepts_an_ask_from_a_person_block_an_asking_final_or_an_adopted_worker_ask_and_nothing_else():
    row = {"id": "d-1", "assigned_to": "user-alice", "notify_user_id": "user-alice", "dedupe_key": "k", "summary": "s"}
    live, base = real_case(expect_ask="maybe", asks_filed=[row], messages=[{"kind": "ask", "text": "q", "data": {}}, {"kind": "final", "text": "ok"}])
    unexplained = [p for p in live.check(dict(base, log=[{"step": "verdict", "blocker": "more_work", "question": "q"}])) if "verdict was" in p]
    assert unexplained                                                       # an ask with no structural source is still a problem
    for log, callers in (([{"step": "verdict", "blocker": "needs_person", "question": "q"}], []),
                         ([{"step": "verdict", "blocker": "more_work", "asks_person": True, "question": "q"}], []),
                         ([], [["create_decision", "handler"]])):
        assert not [p for p in live.check(dict(base, log=log, callers=callers)) if "verdict was" in p]
