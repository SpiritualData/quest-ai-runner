"""Asking a question must not open a task, and asking for no task must be obeyed.

Regression cover for a failure that ran for months in a live deployment: plain informational
messages ("give me a report on the campaign", "give me the link to the leads sheet", "from the
database, tell me what you know about X") came back as "On it, running this as task #N".

The first two defects (a sayable noun read as a change verb; a scene-setting preamble hiding the
question from an anchored regex) belonged to the regex net over the user's words, which is retired:
the escalation nets now honor the planner's ``user_intent`` verdict (tests/test_escalation_threshold.py).
What stays here is the third:

  NO VETO -- every guard in the orchestrator could only ever ADD execution, so the one thing a
  user could not do was ask for less of it: "don't create a task, just answer me here" was
  itself escalated into a task.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    HOLD_OFF_NO_ACTION_ACK_NOTE,
    Orchestrator,
    OrchestratorConfig,
    message_forbids_new_task,
)

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# The user's own veto: "don't open a task" must beat a planner "deep".
# ---------------------------------------------------------------------------

FORBIDS_A_TASK = [
    "don't create a task, just answer me here",
    "dont open a task for this",
    "no new tasks please",
    "just tell me, don't go off and do it",
    "answer here in the chat",
    "hold off for now",
    "not yet",
    "i haven't given you an instruction yet",
]


def test_veto_phrases_forbid_a_new_task():
    for message in FORBIDS_A_TASK:
        assert message_forbids_new_task(message) is True, message


# Telling the runner to cancel something is an instruction to ACT on its own state. The planner's
# "hold_off" verdict gates the escalation nets for it, but the pre-planner veto must NOT suppress a
# planner "deep" -- otherwise the reply says "sure, cancelling that" and cancels nothing, which is
# the false-completion failure this codebase is full of fixes for.
ACTS_ON_RUNNER_STATE = [
    "cancel that run",
    "kill task #2699",
    "dismiss those tasks",
    "delete the queued job",
]


def test_cancel_instructions_are_still_executable():
    for message in ACTS_ON_RUNNER_STATE:
        assert message_forbids_new_task(message) is False, message


def test_veto_degrades_planner_deep_to_answer():
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Do the thing", "deep_brief": "do it",
         "rationale": "planner ignored the veto"},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "don't create a task, just answer me here: what account does the lead email go from?")
    assert res.kind == "answer"
    assert runner.calls == []                     # nothing executed


# The bare "hold on"/"hold off"/"stand by"/"not yet" phrasing names no topic of its own (no "task",
# no "answer"): it is ambiguous in a MIXED message that also carries a directive elsewhere. Without
# this, "hold off on the emails, but go ahead and update the leads sheet" and "the deploy is not yet
# done -- fix it" were answered instead of executed, because the bare phrase alone vetoed the whole
# turn even though the rest of the message was a live instruction.
MIXED_HOLD_AND_DIRECTIVE = [
    "hold off on the emails, but go ahead and update the leads sheet",
    "the deploy is not yet done -- fix it",
    "hold on, actually go ahead and fix the back button",
    "stand by on the campaign, but please update the draft subject line",
]


def test_mixed_hold_and_directive_does_not_forbid_a_task():
    for message in MIXED_HOLD_AND_DIRECTIVE:
        assert message_forbids_new_task(message) is False, message


# A bare hold phrase with no directive anywhere else in the message still vetoes, same as before.
def test_bare_hold_phrase_alone_still_forbids_a_task():
    for message in ["hold off for now", "not yet", "hold on a second", "stand by"]:
        assert message_forbids_new_task(message) is True, message


def test_veto_degrades_planner_confirm_to_answer():
    provider = StubProvider(decisions=[
        {"action": "confirm", "confirm_question": "Shall I start?", "rationale": "r"},
    ])
    escalation = StubEscalation()
    res = _orch(provider, StubRetrieval(), escalation=escalation).run(
        "just answer me here, no new tasks")
    assert res.kind == "answer"
    assert escalation.raised == []                # no decision-request parked either


def test_veto_reaches_the_planner_and_the_reply_contract():
    provider = StubProvider(decisions=[{"action": "answer", "rationale": "r"}])
    _orch(provider, StubRetrieval()).run("just answer in the chat, what is the lead count?")
    planner_prompt = provider.plan_prompts[0]
    assert "THE USER ASKED YOU NOT TO OPEN A TASK" in planner_prompt
    # ...and it is the veto wording, not the brainstorm wording: telling someone "brainstorm mode
    # is on" when you simply did as they asked reads as a system excuse for ignoring them.
    assert "BRAINSTORM MODE (active for this turn)" not in planner_prompt
    assert "--- NO TASK WAS OPENED THIS TURN" in HOLD_OFF_NO_ACTION_ACK_NOTE


def test_an_ordinary_message_is_untouched():
    # The whole veto path stays inert without the words that trigger it: a plain command still
    # reaches the deep runner exactly as before.
    provider = StubProvider(decisions=[
        {"action": "deep", "goal": "Fix it", "deep_brief": "fix the back button",
         "rationale": "r"},
    ])
    runner = StubDeepRunner(met=True)
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "fix the back button on the reflection page")
    assert res.kind == "deep"
    assert runner.calls != []
    assert "THE USER ASKED YOU NOT TO OPEN A TASK" not in provider.plan_prompts[0]
