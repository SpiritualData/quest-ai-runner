"""A read-budget-capped turn that was asked for WORK does the work (found live 2026-09-27).

A reply on a finished bug-fix task reported two new bugs. The run diagnosed both, spent its read
budget, wrapped up with a "best-effort answer" and was marked done with nothing changed; only a
second reply ("yes please fix both") started a deep run. Three generic causes, each pinned here:

  * the planner had already said it was reading to ground a brief BEFORE escalating, and the
    wrap-up ignored that structured intent;
  * the brief is machine-composed and QUOTES earlier runs' output ("not yet released to
    production"), and the escalation nets' hold-off check read that as the human saying "not yet"
    (today: a planner "hold_off" verdict counts only for a typed message);
  * a turn with no usable intent verdict got no intent judgment on this path, unlike the answer
    path.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    clip_head_and_tail,
)

from .conftest import StubDeepRunner, StubProvider, StubRetrieval

# Shaped like the brief the backend composes for a reply on a finished thread: the standing
# request, an earlier run's result (the AI's own words), then the person's new reply.
FOLLOW_UP_BRIEF = (
    "The planning page does not scroll. Please fix it.\n\n"
    "What earlier runs on this same thread reported, newest first:\n"
    "Run 1, status done:\nFixed the scroll handler. Committed, not yet released to production.\n\n"
    "They have now replied in the app:\n"
    "Scrolling works now, thanks. But the arrow keys stop working on the other tab after I leave "
    "the planning page, the grid still grabs them. Can I drag the left column on the phone?"
)


class JudgeProvider(StubProvider):
    """StubProvider whose intent-directive judgment is answered separately from the planner's
    scripted decision queue, so the queue stays exactly what the planner and verifier see."""

    def __init__(self, decisions: List[Dict[str, Any]], *, directive: bool):
        super().__init__(decisions)
        self.directive = directive
        self.judge_calls: List[str] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Any:
        if (tool_schema or {}).get("name") == "execution_directive_verdict":
            self.judge_calls.append(prompt)
            return {"is_execution_directive": self.directive, "reason": "scripted"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def orch(provider, runner, **cfg):
    config = OrchestratorConfig(max_steps=1, **cfg)
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval({"src/grid.tsx": "GROUNDING grid code"}),
                        provider=provider, registry=ModelRegistry(provider), config=config,
                        deep_runner=runner)


READ = {"action": "read", "reads": [{"rel_path": "src/grid.tsx"}],
        "rationale": "read the handler to ground a precise brief before escalating"}


def test_a_hold_off_verdict_on_a_queued_brief_is_not_the_person_speaking():
    # On a machine-composed brief a "hold_off" verdict may come from quoted output ("not yet
    # released"), so it counts as no verdict: the intent judge decides instead of the nets
    # being switched off.
    provider = JudgeProvider([dict(READ, user_intent="hold_off"), {"met": True, "reason": "done"}],
                             directive=True)
    runner = StubDeepRunner(met=True, output="fixed and verified")
    res = orch(provider, runner).run(FOLLOW_UP_BRIEF, message_is_user_turn=False)
    assert provider.judge_calls, "a brief's hold_off verdict must fall back to the judge"
    assert runner.calls
    assert res.exit_reason != "read_budget"


def test_an_act_verdict_escalates_without_a_judge_call():
    provider = JudgeProvider([dict(READ, user_intent="act"), {"met": True, "reason": "done"}],
                             directive=False)
    runner = StubDeepRunner(met=True, output="fixed and verified")
    res = orch(provider, runner).run("the export drops the last row, fix it")
    assert runner.calls, "the planner said the user ordered work"
    assert provider.judge_calls == []
    assert res.exit_reason != "read_budget"


def test_an_ask_verdict_wraps_up_without_a_judge_call():
    provider = JudgeProvider([dict(READ, user_intent="ask")], directive=True)
    runner = StubDeepRunner(met=True, output="should not run")
    res = orch(provider, runner).run("why does the export drop the last row?")
    assert runner.calls == []
    assert provider.judge_calls == []
    assert res.exit_reason == "read_budget"


def test_planner_prepared_deep_work_escalates_when_the_budget_runs_out():
    provider = JudgeProvider([dict(READ, deep_brief="Guard the keydown listener on focus."),
                              {"met": True, "reason": "done"}], directive=False)
    runner = StubDeepRunner(met=True, output="fixed and verified")
    res = orch(provider, runner).run("Arrow keys still leak into the other tab.",
                                     message_is_user_turn=False)
    assert runner.calls, "the planner's own prepared deep work must run"
    assert "Guard the keydown listener" in runner.calls[0]["brief"]
    assert res.exit_reason != "read_budget"
    assert provider.judge_calls == []     # the planner's decision settled it, no extra call


def test_follow_up_bug_report_on_a_task_escalates_via_intent_judgment():
    provider = JudgeProvider([READ, {"met": True, "reason": "done"}], directive=True)
    runner = StubDeepRunner(met=True, output="fixed and verified")
    res = orch(provider, runner).run(FOLLOW_UP_BRIEF, message_is_user_turn=False)
    assert provider.judge_calls, "the ambiguous band must get the intent judgment"
    # The judge sees the NEWEST words (the reply at the end of the brief), not only its head.
    assert "grid still grabs them" in provider.judge_calls[0]
    assert runner.calls, "a reported bug on a fix thread must start the fix"
    assert res.exit_reason != "read_budget"


def test_a_genuine_question_still_wraps_up_with_an_answer():
    provider = JudgeProvider([READ], directive=False)
    runner = StubDeepRunner(met=True, output="should not run")
    res = orch(provider, runner).run(FOLLOW_UP_BRIEF, message_is_user_turn=False)
    assert runner.calls == []
    assert res.kind == "answer" and res.exit_reason == "read_budget"


def test_a_typed_hold_off_still_keeps_the_turn_an_answer():
    provider = JudgeProvider([dict(READ, user_intent="hold_off")], directive=True)
    runner = StubDeepRunner(met=True, output="should not run")
    res = orch(provider, runner).run(
        "The grid bug is back, but hold off on fixing it, not yet. What is causing it?")
    assert runner.calls == []
    assert provider.judge_calls == []
    assert res.exit_reason == "read_budget"


def test_clip_keeps_head_and_tail():
    text = "HEAD " + "x" * 5000 + " TAIL"
    clipped = clip_head_and_tail(text, 1000)
    assert clipped.startswith("HEAD") and clipped.endswith("TAIL")
    assert len(clipped) < 1100
    assert clip_head_and_tail("short", 1000) == "short"
