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


class QueueRunner(StubDeepRunner):
    """A runner whose work outlives the turn (a consumer's task-queue hand-off)."""
    starts_background_work = True


def test_own_escalation_never_starts_background_work():
    """Found 2026-10-07 in Quest's chat: a remark ("I'm buying the yellow paint tomorrow") got a
    not-met verdict and the last-resort escalation queued a background task to buy the paint. The
    orchestrator's own escalations may not start work that outlives the turn; the answer stands."""
    provider = VerdictProvider([ANSWER], [NOT_MET, NOT_MET])
    runner = QueueRunner(met=True, output="Queued as task #1")
    res = orch(provider, runner).run("Mia loves the yellow one, I'm buying it tomorrow.")
    assert res.kind == "answer"
    assert runner.calls == []


def test_need_more_context_escalation_never_starts_background_work():
    provider = VerdictProvider([ANSWER], [dict(NOT_MET, need_more_context=True), NOT_MET, NOT_MET])
    runner = QueueRunner(met=True, output="Queued as task #1")
    res = orch(provider, runner).run("What is the status of the tesmd bot issue?")
    assert res.kind == "answer"
    assert runner.calls == []


def test_overseer_prompt_keeps_impossible_asks_and_its_question_user_facing():
    """An overseer escalate_human replaced a correct decline ("I cannot move money") with its own
    third-person reason ("The user is requesting a direct financial transaction ...") as the
    question shown to the user (2026-10-07)."""
    from quest_ai_runner.core.overseer import OVERSEER_PROMPT
    assert "NOTHING here can" in OVERSEER_PROMPT
    assert "never about them" in OVERSEER_PROMPT


def test_an_empty_last_resort_run_keeps_the_answer():
    """A last-resort run that comes back with nothing must not replace the answer (2026-10-07:
    the turn ended with no reply and the person got a generic 'could you tell me more?')."""
    provider = VerdictProvider([ANSWER], [NOT_MET, NOT_MET])
    runner = StubDeepRunner(met=False, output="")
    res = orch(provider, runner).run("What is the status of the tesmd bot issue?")
    assert res.kind == "answer"
    assert "I have no material on that" in (res.text or "")
    assert len(runner.calls) >= 1


def test_a_users_own_request_may_still_reach_the_queue_through_an_escalation():
    """The decline is for work the user did not ask for. When the planner's verdict says their
    message ordered work (``user_intent`` "act", though it only answered), an escalation that
    resolves to the queue still runs. A missing verdict declines (the conservative side)."""
    provider = VerdictProvider([dict(ANSWER, user_intent="act")], [NOT_MET, NOT_MET])
    runner = QueueRunner(met=True, output="Queued as task #1")
    orch(provider, runner).run("add the wedding venues to the shared planning sheet")
    assert len(runner.calls) >= 1


def test_an_empty_escalation_that_filed_a_decision_still_reports_itself():
    from quest_ai_runner.core.adapters import DeepResult
    from quest_ai_runner.core.orchestrator import own_escalation_adds_nothing

    class Res:
        text = ""
        deep_results = [DeepResult(met=False, output="", decision_id="dec_1")]
    assert own_escalation_adds_nothing(Res()) is False
    Res.deep_results = [DeepResult(met=False, output="")]
    assert own_escalation_adds_nothing(Res()) is True

    from quest_ai_runner.core.guard import ExecutionFact

    class Record:
        facts = [ExecutionFact(goal="old")]
    done = ExecutionFact(goal="wrote it")
    done.succeeded = True
    Record.facts.append(done)
    assert own_escalation_adds_nothing(Res(), Record(), facts_before=1) is False
    assert own_escalation_adds_nothing(Res(), Record(), facts_before=2) is True
