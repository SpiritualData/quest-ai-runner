"""A task whose deep run leaves the ask clearly unfinished gets another deep run, on its own.

Before this, such a task ended with "what is left" text and a person had to reply to get the rest
done. Now ``TaskExecutor.execute`` starts a follow-up run for the remaining items when ALL hold: the
result is incomplete and names what remains, nothing is waiting on a person, the run was not
cancelled or paused, and the chain is under its run cap and token budget. The decision is logged and
put on the task's feed either way.

The executor is origin-agnostic (chat, the task modal, an autopilot pass and an API call all arrive
as the same queued task), so one case below uses a task document shaped like a modal-created task.

Offline: a scripted brain, a mock client, no model calls.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.core import usage_limit
from quest_ai_runner.core.adapters_values import DeepResult
from quest_ai_runner.core.orchestrator import OrchestratorConfig, OrchestratorResult
from quest_ai_runner.core.orchestrator_constants import AUTO_FOLLOWUP_MAX_RUNS
from quest_ai_runner.runner.auto_followup import assess, followup_brief
from quest_ai_runner.runner.executor import TaskExecutor

from .test_runner import MockQuestClient

@pytest.fixture(autouse=True)
def fresh_usage_note():
    """A usage-limited result makes the executor record a process-wide note; do not leak it to other tests."""
    usage_limit.reset_for_tests()
    yield
    usage_limit.reset_for_tests()


ASK = "Update the docs for the export feature and add a changelog entry"
MODAL_TASK = {"task_id": "atask_modal", "text": ASK, "source": "task_modal",
              "task_kind": "ai_task", "conv_id": None}


class ScriptedBrain:
    """Returns the scripted results in order (the last one repeats) and records every call."""

    def __init__(self, results: List[OrchestratorResult], *, cfg: Optional[OrchestratorConfig] = None,
                 open_decisions: Optional[List[frozenset]] = None) -> None:
        self.results = list(results)
        self.cfg = cfg or OrchestratorConfig()
        self.calls: List[Dict[str, Any]] = []
        self._decisions = list(open_decisions or [])

    def run(self, text, *, deep_goal=None, resume_session_id=None, **kwargs):
        self.calls.append({"text": text, "deep_goal": deep_goal, "resume": resume_session_id,
                           "user_turn": kwargs.get("message_is_user_turn")})
        idx = min(len(self.calls) - 1, len(self.results) - 1)
        result = self.results[idx]
        if isinstance(result, Exception):
            raise result
        return result

    def quest_open_decision_ids(self, quest_id):
        if not self._decisions:
            return frozenset()
        return self._decisions.pop(0) if len(self._decisions) > 1 else self._decisions[0]

    def __getattr__(self, name):
        raise AttributeError(name)


class BrainWithoutDeepGoal(ScriptedBrain):
    def run(self, text, *, resume_session_id=None, **kwargs):      # no deep_goal parameter
        return super().run(text, resume_session_id=resume_session_id, **kwargs)


def deep(*deeps: DeepResult) -> OrchestratorResult:
    return OrchestratorResult(kind="deep", goals=["G"], deep_results=list(deeps))


def unmet(**fields) -> DeepResult:
    base = dict(met=False, output="Updated the docs page.", session_id="sess-1", tokens=1000,
                verdict_reason="The changelog entry is still missing.",
                verdict_next_action="Add the changelog entry.",
                error="Goal not yet met: The changelog entry is still missing.")
    base.update(fields)
    return DeepResult(**base)


def met(**fields) -> DeepResult:
    base = dict(met=True, output="Docs updated and changelog entry added.", session_id="sess-2",
                tokens=500)
    base.update(fields)
    return DeepResult(**base)


def run_task(brain, task: Optional[Dict[str, Any]] = None):
    client = MockQuestClient([])
    outcome = TaskExecutor(client, brain).execute(dict(task or {"task_id": "atask_1", "text": ASK}))
    return outcome, client


def feed(client: MockQuestClient) -> List[str]:
    return [text for _, kind, text, _ in client.progress if kind == "status" and text]


# --- incomplete and unblocked -> a follow-up run is spawned ----------------------------------------

def test_incomplete_and_unblocked_spawns_a_follow_up_that_finishes_the_ask():
    brain = ScriptedBrain([deep(unmet()), deep(met())])
    outcome, client = run_task(brain)
    assert len(brain.calls) == 2
    assert outcome.status == "done"
    assert [r[1] for r in client.reports] == ["done"]
    assert "changelog entry added" in outcome.result


def test_the_follow_up_brief_carries_the_ask_what_was_done_and_what_remains():
    brain = ScriptedBrain([deep(unmet()), deep(met())])
    run_task(brain)
    first, second = brain.calls
    assert first["deep_goal"] is None and first["text"] == ASK
    brief = second["text"]
    assert ASK in brief
    assert "Updated the docs page." in brief
    assert "The changelog entry is still missing." in brief and "Add the changelog entry." in brief
    assert chr(0x2014) not in brief and chr(0x2013) not in brief


def test_the_follow_up_is_a_deep_run_that_resumes_the_previous_session():
    brain = ScriptedBrain([deep(unmet(session_id="sess-1")), deep(met())])
    run_task(brain)
    second = brain.calls[1]
    assert second["deep_goal"] and second["deep_goal"].startswith("Finish what remains")
    assert second["resume"] == "sess-1"
    assert second["user_turn"] is False


def test_a_task_modal_task_is_followed_up_like_any_other():
    brain = ScriptedBrain([deep(unmet()), deep(met())])
    outcome, client = run_task(brain, MODAL_TASK)
    assert len(brain.calls) == 2
    assert outcome.status == "done"
    assert any(t.startswith("Continuing automatically (follow-up run 1 of 3)") for t in feed(client))


def test_a_turn_limit_stop_is_a_named_remainder():
    stopped = DeepResult(met=False, output="", limit_hit=True, session_id="sess-1",
                         error="The run reached its turn limit.")
    brain = ScriptedBrain([deep(stopped), deep(met())])
    outcome, _ = run_task(brain)
    assert len(brain.calls) == 2 and outcome.status == "done"
    assert "turn budget" in brain.calls[1]["text"]


def test_a_verified_not_met_answer_turn_is_followed_up_too():
    answer = OrchestratorResult(kind="answer", text="Partly done.", exit_reason="max_turns",
                                goal_verdict={"met": False, "reason": "Two pages are still old.",
                                              "next_action": "Update the two old pages."})
    brain = ScriptedBrain([answer, deep(met())])
    outcome, _ = run_task(brain)
    assert len(brain.calls) == 2 and outcome.status == "done"
    assert "Two pages are still old." in brain.calls[1]["text"]


def test_each_follow_up_chain_member_can_name_a_new_remainder():
    brain = ScriptedBrain([
        deep(unmet(verdict_reason="Part B is missing.", session_id="s1")),
        deep(unmet(verdict_reason="Part C is missing.", session_id="s2")),
        deep(met())])
    outcome, _ = run_task(brain)
    assert len(brain.calls) == 3 and outcome.status == "done"
    assert brain.calls[2]["resume"] == "s2" and "Part C is missing." in brain.calls[2]["text"]


def test_a_brain_whose_run_has_no_deep_goal_parameter_still_gets_the_follow_up():
    brain = BrainWithoutDeepGoal([deep(unmet()), deep(met())])
    outcome, _ = run_task(brain)
    assert len(brain.calls) == 2 and outcome.status == "done"
    assert brain.calls[1]["deep_goal"] is None


# --- incomplete but blocked on a person (or otherwise not worth repeating) -> not spawned ---------

@pytest.mark.parametrize("blocked", [
    unmet(decision_id="dec_1"),
    unmet(needs_person="Which account should the entry be filed under?"),
    unmet(tool_failure="The changelog service answers 500."),
    unmet(stalled="Two attempts ended at the same point."),
    unmet(exhausted=True),
    unmet(usage_limited=True),
    unmet(launch_failed=True),
    unmet(deferred=True),
    unmet(error="Unverified: goal verification did not run (outage). Not confirmed complete."),
], ids=["decision", "needs_person", "tool_failure", "stalled", "exhausted", "usage_limit",
        "launch_failed", "deferred", "unverified"])
def test_a_blocked_run_is_not_followed_up(blocked):
    brain = ScriptedBrain([deep(blocked), deep(met())])
    run_task(brain)
    assert len(brain.calls) == 1


def test_a_decision_raised_while_the_run_worked_blocks_the_follow_up():
    brain = ScriptedBrain([deep(unmet()), deep(met())],
                          open_decisions=[frozenset(), frozenset({"dec_new"})])
    outcome, client = run_task(brain)
    assert len(brain.calls) == 1
    assert any("Not continuing automatically: blocked: a new decision" in t for t in feed(client))


def test_a_confirm_or_cancelled_result_is_not_followed_up():
    for result in (OrchestratorResult(kind="confirm", question="Send it?", decision_id="dec_2"),
                   OrchestratorResult(kind="cancelled", exit_reason="cancelled")):
        brain = ScriptedBrain([result, deep(met())])
        run_task(brain)
        assert len(brain.calls) == 1


def test_an_unmet_run_that_names_nothing_remaining_is_not_followed_up():
    brain = ScriptedBrain([deep(unmet(verdict_reason="", verdict_next_action="")), deep(met())])
    outcome, client = run_task(brain)
    assert len(brain.calls) == 1
    assert any("no remaining items are named" in t for t in feed(client))


def test_a_task_cancelled_before_the_follow_up_is_not_continued():
    class CancelledClient(MockQuestClient):
        def is_task_cancelled(self, task_id):
            return True

    brain = ScriptedBrain([deep(unmet()), deep(met())])
    TaskExecutor(CancelledClient([]), brain).execute({"task_id": "atask_1", "text": ASK})
    assert len(brain.calls) == 1


# --- complete -> not spawned -----------------------------------------------------------------------

def test_a_complete_run_is_not_followed_up():
    brain = ScriptedBrain([deep(met()), deep(met())])
    outcome, client = run_task(brain)
    assert len(brain.calls) == 1 and outcome.status == "done"
    assert not any("automatically" in t for t in feed(client))


def test_a_plain_answer_is_not_followed_up():
    brain = ScriptedBrain([OrchestratorResult(kind="answer", text="Here you go.", exit_reason="verified")])
    run_task(brain)
    assert len(brain.calls) == 1


# --- the cap and the budget ------------------------------------------------------------------------

def test_the_chain_stops_at_the_cap_and_reports_the_remainder():
    brain = ScriptedBrain([deep(unmet())])          # every run falls short
    outcome, client = run_task(brain)
    assert len(brain.calls) == 1 + AUTO_FOLLOWUP_MAX_RUNS
    assert outcome.status == "incomplete"
    assert any("cap reached" in t for t in feed(client))
    assert "The changelog entry is still missing." in outcome.result


def test_a_configured_cap_is_honoured_and_zero_turns_the_behaviour_off():
    for limit in (1, 0):
        brain = ScriptedBrain([deep(unmet())], cfg=OrchestratorConfig(auto_followup_max_runs=limit))
        run_task(brain)
        assert len(brain.calls) == 1 + limit


def test_the_token_budget_stops_the_chain_before_the_cap():
    heavy = unmet(tokens=600)
    brain = ScriptedBrain([deep(heavy)], cfg=OrchestratorConfig(auto_followup_token_budget=1000))
    outcome, client = run_task(brain)
    assert len(brain.calls) == 2                     # 600 < 1000 -> one follow-up; 1200 >= 1000 -> stop
    assert any("token budget reached" in t for t in feed(client))


def test_a_follow_up_that_raises_keeps_the_earlier_result():
    brain = ScriptedBrain([deep(unmet()), RuntimeError("boom")])
    outcome, client = run_task(brain)
    assert len(brain.calls) == 2
    assert outcome.status == "incomplete"            # the earlier run's work is reported, not a failure
    assert any("automatic follow-up run failed" in t for t in feed(client))


# --- the audit trail -------------------------------------------------------------------------------

def test_the_decision_is_logged_with_its_reason(caplog):
    brain = ScriptedBrain([deep(unmet()), deep(met())])
    with caplog.at_level(logging.INFO):
        run_task(brain)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "auto follow-up (0 of 3 used): continue" in text
    assert "complete: every goal was verified met" in text


# --- the pure policy -------------------------------------------------------------------------------

def test_assess_reports_the_remaining_items_and_a_reason_either_way():
    decision = assess(deep(unmet()), followups_done=0, max_followups=3, tokens_used=0,
                      token_budget=None)
    assert decision.follow_up and len(decision.remaining) == 1
    capped = assess(deep(unmet()), followups_done=3, max_followups=3, tokens_used=0, token_budget=None)
    assert not capped.follow_up and capped.reason.startswith("cap reached") and capped.remaining


def test_assess_ignores_the_wording_of_the_output():
    # The model's own words never decide anything: output that says "nothing is left" on an unmet,
    # named-remainder result is still followed up, and output that says "remaining" on a met one is not.
    said_done = deep(unmet(output="Everything is finished, nothing remains."))
    assert assess(said_done, followups_done=0, max_followups=3, tokens_used=0, token_budget=None).follow_up
    said_left = deep(met(output="What remains is nothing important, items left: none."))
    assert not assess(said_left, followups_done=0, max_followups=3, tokens_used=0,
                      token_budget=None).follow_up


def test_the_brief_is_bounded():
    huge = "x" * 50_000
    brief = followup_brief(huge, deep(unmet(output=huge)), ["y" * 50_000], number=1, limit=3)
    assert len(brief) < 10_000
