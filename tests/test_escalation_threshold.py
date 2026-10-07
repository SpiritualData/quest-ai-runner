"""The bar a chat message must clear before an answer turn becomes a task.

The escalation nets (the fallbacks that turn an answer turn into work) used to read the user's
message with a regex net: change verbs, interrogative openers, polite-command openers, "I'll ..."
plans, hold-off phrases. Every misroute added one more pattern and each pattern leaked the next
phrasing. The nets now honor the PLANNER's structured verdict, ``user_intent`` ("act" | "ask" |
"inform" | "hold_off"), stated on the planning call it already makes; a missing verdict falls back
to the one-shot intent judge, never to a keyword list.

The message lists below are the real cockpit/chat messages that once opened a task nobody asked
for (NOT_DIRECTIVES) and the commands that must keep becoming work (DIRECTIVES). The planner's
reading of them is measured live (see the CHANGELOG entry); here they pin that the orchestrator
does exactly what the verdict says, whatever words the message uses.
"""
from typing import Any, Dict, List

import pytest

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    DECIDE_TOOL,
    PLANNER_PROMPT,
    PLANNER_PROMPT_COMPACT,
    USER_INTENTS,
    Orchestrator,
    OrchestratorConfig,
    decide_tool_for,
    normalize_decision,
    normalize_user_intent,
)

from .conftest import StubDeepRunner, StubProvider, StubRetrieval

# Messages that must NEVER open a task on their own.
NOT_DIRECTIVES = [
    "dont create task just drop asnewr here in chat",
    "don't create a task for this",
    "no new tasks please",
    "kill and delete those tasks.. i havent given you an instruction yet",
    "hold off on this for now",
    "just answer here in the chat",
    "give me an update on the product subscription work",
    "give me a status on the campaign",
    "where are we with the Quest facebook leads",
    "any update on the offer letter",
    "how are we doing on the landing page",
    "catch me up on the email campaign",
    "i have shared the feedback when he replies i will let you know",
    "I'll lean on the claim that effect sizes are stable across sites and move on to the next section.",
    "I'm going to move the launch to Friday and update the team myself",
    "so i plan to rewrite the intro tonight",
]

# Messages that must STILL become work.
DIRECTIVES = [
    "make sure the new draft is in the approval queue",
    "go ahead and create the draft and leave it in the approval queue",
    "fix the back button",
    "update my goal to be more ambitious",
    "please update the endpoint",
    "can you fix the date bug?",
    "the system incorrectly assigns dates to actions",
    "send me the report and update the sheet",
    "I'll need you to update the sheet",
    "I want you to rename the goal",
]


class JudgeCountingProvider(StubProvider):
    """Answers the intent judge separately from the planner's scripted queue and counts it."""

    def __init__(self, decisions: List[Dict[str, Any]], *, directive: bool = False, **kw):
        super().__init__(decisions, **kw)
        self.directive = directive
        self.judge_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Any:
        if (tool_schema or {}).get("name") == "execution_directive_verdict":
            self.judge_calls += 1
            return {"is_execution_directive": self.directive, "reason": "scripted"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def _run(message, decision, *, directive=False, user_turn=True):
    provider = JudgeCountingProvider([decision], directive=directive,
                                     answer_text="Here is how I would approach it.")
    runner = StubDeepRunner(met=True, output="done")
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=runner,
                        config=OrchestratorConfig(overseer=False))
    res = orch.run(message, message_is_user_turn=user_turn)
    return res, runner, provider


def _answer(intent):
    d = {"action": "answer", "model_tier": "sonnet", "rationale": "answering"}
    if intent is not None:
        d["user_intent"] = intent
    return d


@pytest.mark.parametrize("message", NOT_DIRECTIVES)
@pytest.mark.parametrize("intent", ["ask", "inform", "hold_off"])
def test_a_non_act_verdict_never_escalates_whatever_the_words(message, intent):
    _res, runner, provider = _run(message, _answer(intent), directive=True)
    assert runner.calls == [], f"{intent!r} must not open work: {message!r}"
    assert provider.judge_calls == 0, "a verdict settles it; no extra LLM call"


@pytest.mark.parametrize("message", DIRECTIVES)
def test_an_act_verdict_escalates_an_answered_order(message):
    _res, runner, provider = _run(message, _answer("act"))
    assert runner.calls, f"the planner said the user ordered work: {message!r}"
    assert provider.judge_calls == 0


def test_the_verdict_not_the_words_decides():
    # The same words go either way with the verdict: no keyword reading is left to disagree.
    _r, runner_ask, _p = _run("fix the back button", _answer("ask"))
    _r, runner_act, _p = _run("thanks, that is all", _answer("act"))
    assert runner_ask.calls == []
    assert runner_act.calls


def test_hold_off_also_turns_off_the_planner_work_flag():
    decision = dict(_answer("hold_off"), answer_contains_work_to_execute=True)
    _res, runner, _p = _run("kill those runs and answer me here", decision)
    assert runner.calls == []


def test_a_planner_deep_is_not_gated_by_the_verdict():
    # Asking for work still gets work: the verdict only gates the nets on an ANSWER turn.
    decision = {"action": "deep", "goal": "Cancel the run", "deep_brief": "cancel it",
                "rationale": "act on runner state", "user_intent": "hold_off"}
    _res, runner, _p = _run("cancel that run", decision)
    assert runner.calls


@pytest.mark.parametrize("directive", [True, False])
def test_a_missing_verdict_falls_back_to_the_intent_judge(directive):
    _res, runner, provider = _run("the export drops the last row", _answer(None),
                                  directive=directive)
    assert provider.judge_calls == 1
    assert bool(runner.calls) is directive


def test_an_unknown_verdict_is_treated_as_missing():
    _res, runner, provider = _run("the export drops the last row", _answer("maybe"))
    assert provider.judge_calls == 1
    assert runner.calls == []


def test_hold_off_counts_only_for_a_typed_message():
    # A queued task's brief is machine-composed: a hold_off verdict there is no verdict.
    _res, runner, provider = _run("Fix the export.\nEarlier run: not yet released.",
                                  _answer("hold_off"), directive=True, user_turn=False)
    assert provider.judge_calls == 1
    assert runner.calls


def test_normalize_user_intent():
    for value in USER_INTENTS:
        assert normalize_user_intent(value) == value
        assert normalize_user_intent(f"  {value.upper()} ") == value
    for bad in (None, "", "command", 3, ["act"]):
        assert normalize_user_intent(bad) is None


def test_parse_reads_the_field():
    cfg = OrchestratorConfig()
    assert normalize_decision({"action": "answer", "user_intent": "inform"}, cfg).user_intent == "inform"
    assert normalize_decision({"action": "answer"}, cfg).user_intent is None


def test_schema_requires_the_verdict_in_every_variant():
    for tool in (DECIDE_TOOL, decide_tool_for(True, True, card_thread=True, tools=True, web=True),
                 decide_tool_for(False, False, compact=True)):
        schema = tool["input_schema"]
        assert "user_intent" in schema["required"]
        assert schema["properties"]["user_intent"]["enum"] == list(USER_INTENTS)


def test_both_planner_profiles_define_the_verdict():
    for prompt in (PLANNER_PROMPT, PLANNER_PROMPT_COMPACT):
        assert "`user_intent`" in prompt
        for value in USER_INTENTS:
            assert f'"{value}"' in prompt
