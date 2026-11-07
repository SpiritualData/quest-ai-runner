"""An unmet goal is pursued with the read-only deep search before the reply is sent (the hub's bounded replan), as the Mary case needed: the draft
asked the person to paste material the search could find. A long search sends the current reply with one plain line and continues as a queued
follow-up; a search that finds nothing ends with what was tried and what is missing; a remark never starts a follow-up. Offline, scripted."""
from __future__ import annotations

import json
from typing import Any, Dict, List

from hubofhubs.api import EVENT_MILESTONE, DeepResult
from quest_ai_runner.the_consciousness.feedback_learning.candidate_cases import record_candidate, turn_signals

from .test_incremental_replies import build, run
from .test_done_evidence import LOOKUP, Provider, Runner

ASKS_TO_PASTE = {"met": False, "reason": "the reply asks for a transcript the search can find", "next_action": "find it", "blocker": "needs_person",
                 "question": "Could you paste the transcript?"}


class QueueRunner(Runner):
    """A runner whose work outlives the turn."""
    starts_background_work = True


def plan(intent: str) -> Dict[str, Any]:
    return dict(LOOKUP, user_intent=intent)


def test_a_reply_that_asks_for_what_the_search_can_find_is_replaced_by_the_search():
    runner = Runner([DeepResult(met=True, output="Next step for Mary: send the two follow-ups agreed in the office hour.")])
    events = run(build(Provider([plan("ask")], verdicts=[dict(ASKS_TO_PASTE), {"met": True}]), runner, pursue_inline_seconds=1000.0))
    assert len(runner.briefs) == 1 and "read-only search" in runner.briefs[0]
    assert "office hour" in [e for e in events if e["type"] == "result"][-1]["text"]


def test_a_long_search_sends_the_current_reply_with_one_line_then_continues_as_a_follow_up():
    runner = QueueRunner([DeepResult(met=True, output="Queued as task #1", deferred=True)])
    events = run(build(Provider([plan("ask")], verdicts=[dict(ASKS_TO_PASTE), dict(ASKS_TO_PASTE)]), runner))
    still = [e for e in events if e["type"] == EVENT_MILESTONE and (e.get("data") or {}).get("still_working")]
    assert len(still) == 1 and "Still working on" in still[0]["text"] and len(runner.briefs) == 1
    assert "Still working on" in [e for e in events if e["type"] == "result"][-1]["text"]
    for text in (still[0]["text"], [e for e in events if e["type"] == "result"][-1]["text"]):
        assert "find it" not in text and "the reply asks for a transcript" not in text      # the verdict's next action and reason never reach the reply


def test_a_remark_never_starts_a_queued_follow_up():
    runner = QueueRunner([DeepResult(met=True, output="Queued as task #1", deferred=True)])
    run(build(Provider([plan("inform")], verdicts=[dict(ASKS_TO_PASTE), dict(ASKS_TO_PASTE)]), runner))
    assert runner.briefs == []


def test_a_search_that_finds_nothing_ends_with_what_was_tried_and_what_is_missing():
    runner = Runner([DeepResult(met=False, output="")])
    events = run(build(Provider([plan("ask")], verdicts=[dict(ASKS_TO_PASTE), dict(ASKS_TO_PASTE)]), runner, pursue_inline_seconds=1000.0))
    text = [e for e in events if e["type"] == "result"][-1]["text"]
    assert "could not confirm the rest" in text or "nothing further to try" in text
    assert "the reply asks for a transcript" not in text      # the checker's reason is its working note, never part of a reply


def test_a_turn_that_ended_unmet_becomes_a_candidate_case_only_when_the_folder_is_named(tmp_path):
    signals = turn_signals({"met": False, "blocker": "needs_person"}, "could you paste it?", {"status": "unmet_reported"})
    assert signals == {"unmet": True, "empty_reply": False, "pushed_back": True}
    assert record_candidate("what should Mary do next?", "could you paste it?", signals, "unmet", environ={}) is None
    path = record_candidate("what should Mary do next?", "could you paste it?", signals, "unmet", environ={"QAR_CANDIDATE_CASES_DIR": str(tmp_path)})
    line = json.loads(path.read_text().splitlines()[0])
    assert line["dataset"] == "poorly_answered_inputs" and line["input"] == "what should Mary do next?" and line["passed"] is False
    assert turn_signals({"met": True}, "fine", None) == {"unmet": False, "empty_reply": False, "pushed_back": False}


def test_a_reply_that_is_the_one_clarifying_question_is_terminal_one_message_no_pursuit_no_follow_up():
    """A cut-off or ambiguous message: the planner's own ``asks_first`` says the reply is the question, so the goal condition (the person supplies the missing
    piece) is satisfied by asking. No search runs, no 'still working' or 'tried' line is added, no follow-up is queued, and the same text is not sent twice."""
    runner = QueueRunner([DeepResult(met=True, output="Queued as task #1", deferred=True)])
    events = run(build(Provider([dict(plan("inform"), asks_first=True)], verdicts=[dict(ASKS_TO_PASTE), dict(ASKS_TO_PASTE)]), runner))
    results = [e for e in events if e["type"] == "result"]
    text = results[-1]["text"]
    assert runner.briefs == [] and not [e for e in events if (e.get("data") or {}).get("still_working")]
    assert "Still working on" not in text and "I tried" not in text and "could not confirm" not in text
    texts = [e["text"] for e in events if e["type"] in ("result", EVENT_MILESTONE) and e.get("text")]
    assert len(texts) == len(set(texts))


def test_the_verifiers_asks_person_flag_on_a_remark_is_terminal_but_an_order_is_still_pursued():
    from types import SimpleNamespace
    from quest_ai_runner.the_intelligence.critical_thinking.unmet_goal import turn_awaits_person
    asked = SimpleNamespace(plan=SimpleNamespace(asks_first=False, reply_shape=None), _last_verdict={"met": False, "asks_person": True}, turn_user_intent="inform")
    ordered = SimpleNamespace(plan=SimpleNamespace(asks_first=False, reply_shape=None), _last_verdict={"met": False, "asks_person": True}, turn_user_intent="act")
    unmet = SimpleNamespace(plan=SimpleNamespace(asks_first=False, reply_shape=None), _last_verdict=dict(ASKS_TO_PASTE), turn_user_intent="ask")
    assert turn_awaits_person(asked) and not turn_awaits_person(ordered) and not turn_awaits_person(unmet)
