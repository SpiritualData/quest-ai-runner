"""A turn whose goal is not met and never ran deep work runs ONE deep pass before giving up.

Found live 2026-09-30: asked about a specific bot issue, the quick answer was "I don't have specific
material... which file or quest should I look in?" even though a deep runner was configured. The
in-loop escalation only fires when the verifier sets ``need_more_context``; every other not-met
ending (attempts exhausted after a regeneration, a gap named without that flag) shipped the
non-answer. The net after the verification loop closes all of them.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubDeepRunner, StubProvider, StubRetrieval


class VerdictProvider(StubProvider):
    """Planner decisions come from the base queue; goal verdicts from their own script."""

    def __init__(self, decisions: List[Dict[str, Any]], verdicts: List[Dict[str, Any]]):
        super().__init__(decisions, answer_text="I have no material on that. Which file should I look in?")
        self.verdicts = list(verdicts)

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Any:
        if (tool_schema or {}).get("name") == "goal_verdict":
            return self.verdicts.pop(0) if self.verdicts else {"met": False, "reason": "still no"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


NOT_MET = {"met": False, "reason": "the answer asks the user where to look instead of answering",
           "next_action": "search the corpus for the issue and answer"}
ANSWER = {"action": "answer", "rationale": "quick answer", "model_tier": "sonnet"}


def orch(provider, runner, **cfg):
    config = OrchestratorConfig(max_steps=1, **cfg)
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=config, deep_runner=runner)


def test_not_met_answer_with_no_deep_run_runs_deep_before_giving_up():
    provider = VerdictProvider([ANSWER], [NOT_MET, NOT_MET])
    runner = StubDeepRunner(met=True, output="Found it: the tesmd bot issue is in notes/tesmd.md")
    res = orch(provider, runner).run("What is the status of the tesmd bot issue?")
    assert res.kind == "deep"
    assert res.exit_reason == "escalated_deep"
    assert "Found it" in "\n".join(d.output for d in res.deep_results)


def test_knob_off_keeps_the_old_behavior():
    provider = VerdictProvider([ANSWER], [NOT_MET, NOT_MET])
    runner = StubDeepRunner(met=True, output="should not run")
    res = orch(provider, runner, deep_before_giving_up=False).run("What is the status of the tesmd bot issue?")
    assert res.kind == "answer"


def test_a_met_answer_never_triggers_deep():
    provider = VerdictProvider([ANSWER], [{"met": True, "reason": "ok"}])
    runner = StubDeepRunner(met=True, output="should not run")
    res = orch(provider, runner).run("What is the status of the tesmd bot issue?")
    assert res.kind == "answer"


def test_no_deep_runner_means_no_escalation():
    provider = VerdictProvider([ANSWER], [NOT_MET, NOT_MET])
    res = orch(provider, None).run("What is the status of the tesmd bot issue?")
    assert res.kind == "answer"
