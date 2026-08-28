"""Asking a question must not open a task, and asking for no task must be obeyed.

Regression cover for a failure that ran for months in a live deployment: plain informational
messages ("give me a report on the campaign", "give me the link to the leads sheet", "from the
database, tell me what you know about X") came back as "On it, running this as task #N". Three
distinct defects, one per section below.

  1. INTENT REGEX -- being asked for something SAYABLE (a report, a link, a list, a rundown) reads
     as an order to go and PRODUCE it, because the noun and the change verb are the same string.
  2. ANCHORING -- ``_INFO_QUESTION_RE`` is anchored at the start of the message, so any short
     scene-setting clause ("from the database, ...") hid the interrogative and the message fell
     through to the unconditional "this is a command".
  3. NO VETO -- every guard in the orchestrator could only ever ADD execution, so the one thing a
     user could not do was ask for less of it: "don't create a task, just answer me here" was
     itself escalated into a task.
"""
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    HOLD_OFF_NO_ACTION_ACK_NOTE,
    Orchestrator,
    OrchestratorConfig,
    _message_requests_change,
    _strip_question_preamble,
    message_forbids_new_task,
    message_holds_off_work,
)

from .conftest import StubDeepRunner, StubEscalation, StubProvider, StubRetrieval


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# 1. Being asked for something SAYABLE is a question, not an order to produce it.
# ---------------------------------------------------------------------------

# Real messages from the deployment, each of which opened a task.
ASKS_TO_BE_TOLD = [
    "give me a report on the email campaigns we have been running for the course",
    "give me the link to the spreadsheet containing over 800 Facebook leads",
    "give me a rundown of what we have been doing",
    "provide link to excel/ spreadsheet containing Facebook leads",
    "share the list of leads we contacted",
    "list out my tasks and i will be able to know where to start",
    "list all my tasks",
]


def test_asking_to_be_told_is_not_a_change_request():
    for message in ASKS_TO_BE_TOLD:
        assert _message_requests_change(message) is False, message


# The same nouns under a verb that really does mean "go and make it". These must keep escalating:
# the fix must not buy question-safety by making the assistant refuse to do work.
ORDERS_TO_PRODUCE = [
    "create a report of the last email campaign and CC Thadeus",
    "update the sheet with the corrected lead count",
    "fix the back button on the reflection page",
    "update my goal to reflect the new deadline",
]


def test_orders_to_produce_still_escalate():
    for message in ORDERS_TO_PRODUCE:
        assert _message_requests_change(message) is True, message


# ---------------------------------------------------------------------------
# 2. A scene-setting preamble must not hide the question behind it.
# ---------------------------------------------------------------------------

def test_preamble_does_not_hide_the_question():
    message = "from the data base, tell me what you know about the revenue challenge program"
    assert _message_requests_change(message) is False
    assert _strip_question_preamble(message).startswith("tell me")


def test_preamble_stripping_stops_at_one_clause():
    # Only ONE leading clause is removed, so a question buried after a whole sentence is not
    # manufactured out of a message that opens with something else entirely.
    assert _strip_question_preamble("first, second, what is X").startswith("second,")


def test_a_command_preamble_is_not_stripped():
    # "update the sheet" is the instruction, not scene-setting: stripping it would turn a real
    # command into a question and silently stop doing the work.
    message = "update the sheet, then tell me when done"
    assert _strip_question_preamble(message) == message
    assert _message_requests_change(message) is True


# ---------------------------------------------------------------------------
# 3. The user's own veto: "don't open a task" must beat a planner "deep".
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


# Telling the runner to cancel something is an instruction to ACT on its own state. It still gates
# the escalation nets (as it always did), but it must NOT suppress a planner "deep" -- otherwise the
# reply says "sure, cancelling that" and cancels nothing, which is the false-completion failure
# this codebase is full of fixes for.
ACTS_ON_RUNNER_STATE = [
    "cancel that run",
    "kill task #2699",
    "dismiss those tasks",
    "delete the queued job",
]


def test_cancel_instructions_are_still_executable():
    for message in ACTS_ON_RUNNER_STATE:
        assert message_forbids_new_task(message) is False, message
        assert message_holds_off_work(message) is True, message


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
