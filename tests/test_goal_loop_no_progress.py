"""The deep goal loop stops when the runner says it is OUT OF MOVES, instead of looping to its cap.

The live failure this covers: a data-op question whose answer was not in the data a code-writing
runner could reach. The runner's query came back empty, the verifier (rightly) said the goal was
not met, and the loop re-launched the runner with an augmented brief. The runner wrote new code,
got the same empty result, and the loop did it again, attempt after attempt, while the person
watched it "correcting its code" forever. The stop pinned here is ``DeepResult.exhausted``: the
runner says it is out of moves (its own budget is spent, or it reproduced a result it already
returned). A not-met exhausted run is terminal, and even a run the runner thought was met gets no
retry once the verifier rejects it.

Deliberately NOT done in the library: treating two identical output TEXTS as no progress. An
agentic worker can return the same summary after real progress on disk, so only the runner, which
knows what its result actually was, may declare it. A runner that never sets the flag keeps the
existing retry behaviour, which the last test pins. Fully offline.
"""
from __future__ import annotations

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import RecordingRunner, ScriptedProvider

PLAN = {"action": "deep", "goal": "Report the status",
        "deep_subtasks": [{"goal": "Report the job status", "brief": "look it up"}],
        "rationale": "deep"}

NOT_MET = {"met": False, "reason": "the status was not reported",
           "next_action": "look somewhere else"}


def build(provider, runner, **cfg) -> Orchestrator:
    cfg.setdefault("deep_goal_max_iterations", 8)
    return Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        deep_runner=runner, config=OrchestratorConfig(**cfg),
    )


def test_an_exhausted_not_met_run_is_terminal_without_a_verifier_call():
    provider = ScriptedProvider(plans=[PLAN], verdicts=[NOT_MET] * 8)
    runner = RecordingRunner([DeepResult(met=False, output="I tried 4 queries; all empty.",
                                         exhausted=True)])

    res = build(provider, runner).run("what is the job status?")

    assert len(runner.calls) == 1
    assert provider.verify_calls == 0
    assert res.deep_results[0].met is False
    assert "all empty" in res.deep_results[0].output


def test_an_exhausted_run_the_verifier_rejects_gets_no_retry():
    provider = ScriptedProvider(plans=[PLAN], verdicts=[NOT_MET] * 8)
    runner = RecordingRunner([DeepResult(met=True, output="an answer", exhausted=True),
                              DeepResult(met=True, output="another answer")])

    res = build(provider, runner).run("what is the job status?")

    assert len(runner.calls) == 1
    assert provider.verify_calls == 1
    assert res.deep_results[0].met is False


def test_an_exhausted_run_the_verifier_accepts_is_still_met():
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="an answer", exhausted=True)])

    res = build(provider, runner).run("what is the job status?")

    assert len(runner.calls) == 1
    assert res.deep_results[0].met is True


def test_a_runner_that_never_sets_the_flag_keeps_its_retries():
    provider = ScriptedProvider(plans=[PLAN], verdicts=[NOT_MET, NOT_MET, {"met": True}])
    runner = RecordingRunner([DeepResult(met=True, output="same text")] * 3)

    res = build(provider, runner).run("what is the job status?")

    assert len(runner.calls) == 3
    assert res.deep_results[0].met is True


def test_an_exhausted_first_rung_still_hands_off_to_the_next_rung():
    provider = ScriptedProvider(plans=[PLAN], verdicts=[{"met": True}])
    first = RecordingRunner([DeepResult(met=False, output="nothing I can do", exhausted=True)])
    second = RecordingRunner([DeepResult(met=True, output="done by the next rung")])
    ladder_orch = Orchestrator(
        retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider),
        deep_runner_ladder=[first, second],
        config=OrchestratorConfig(deep_goal_max_iterations=8),
    )
    res = ladder_orch.run("what is the job status?")

    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert res.deep_results[0].met is True
