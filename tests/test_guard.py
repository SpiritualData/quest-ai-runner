"""Claim honesty inside the goal verification — remediation, honest correction, safety.

The answering step can never change files, code, data, or configuration itself; only deep runs
can. Every answer turn's goal verification (``_verify_goal`` with ``verify_claims`` on) therefore
also judges whether completed-change claims in the reply are backed by the turn's
``ExecutionRecord`` (the ``claims_unexecuted`` verdict field; there is NO regex claim detector,
the verification LLM reads the reply and the record together). An unbacked claim remediates in the
answer goal loop: execute the work for real via a deep run when NOTHING ran this turn (safe),
otherwise regenerate the reply to be honest and flag the result partial + claim_corrected so a
background task maps to needs_you / failed, never a false done. NEVER re-runs an action that
already executed (no double mutation).
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import (
    EVENT_EXEC,
    DeepResult,
    ProgressEvent,
)
from quest_ai_runner.core.guard import (
    ExecutionFact,
    ExecutionRecord,
    classify_exec_phase,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.runner.executor import TaskExecutor

from .conftest import StubRetrieval


# ---------------------------------------------------------------------------
# A provider that distinguishes the PLANNER call from the GOAL-VERDICT call.
# ---------------------------------------------------------------------------

class GuardProvider:
    """ModelProvider with separate scripted streams for plan decisions and goal verdicts.

    ``plan()`` serves BOTH the planner step and the goal verification; they are told apart by the
    tool schema name (the verification uses the ``goal_verdict`` tool). ``answer()`` returns
    ``honest_text`` when steered to correct an unbacked claim, ``synth_text`` when reporting a
    finished deep run, else ``answer_text``.
    """

    def __init__(self, *, plan_decisions: List[Dict[str, Any]],
                 goal_verdicts: Optional[List[Dict[str, Any]]] = None,
                 answer_text: str = "Plain answer.",
                 synth_text: str = "The change was made; here is what was done.",
                 honest_text: str = "I was not able to make that change, so it has not been made."):
        self._plan = list(plan_decisions)
        self._verdicts = list(goal_verdicts or [])
        self._answer_text = answer_text
        self._synth_text = synth_text
        self._honest_text = honest_text
        self.plan_calls = 0
        self.verify_calls = 0
        self.verify_prompts: List[str] = []
        self.answer_calls = 0
        self.honest_rewrites = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "goal_verdict":
            # The deep runner's INTERNAL goal verification uses the same tool but never carries an
            # execution record; only the answer-path verification does. Script only the latter so a
            # deep run's own goal loop can't eat the answer-loop verdicts.
            if "EXECUTION RECORD" not in prompt:
                return {"met": True}
            self.verify_calls += 1
            self.verify_prompts.append(prompt)
            if self._verdicts:
                return self._verdicts.pop(0)
            return {"met": True}
        self.plan_calls += 1
        if self._plan:
            return self._plan.pop(0)
        return {"action": "answer", "rationale": "fallback"}

    def answer(self, messages, *, model, system=None) -> str:
        self.answer_calls += 1
        joined = "\n".join(
            m["content"] if isinstance(m["content"], str) else "" for m in messages)
        if "Rewrite your answer to be honest" in joined:
            self.honest_rewrites += 1
            return self._honest_text
        if "You already DID the work the user asked for" in joined:
            return self._synth_text
        return self._answer_text

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class FailingDeepRunner:
    """A deep runner that ALWAYS fails (never meets the goal) — a real attempt that went wrong."""

    def __init__(self):
        self.calls = 0

    def run_goal(self, *, goal, brief, model=None, max_turns=None, emit=None,
                 context_preamble=None) -> DeepResult:
        self.calls += 1
        if emit is not None:
            emit(ProgressEvent(type=EVENT_EXEC, text="failed", data={"phase": "error"}))
        return DeepResult(met=False, error="execution failed")


class SucceedingDeepRunner:
    def __init__(self):
        self.calls = 0

    def run_goal(self, *, goal, brief, model=None, max_turns=None, emit=None,
                 context_preamble=None) -> DeepResult:
        self.calls += 1
        if emit is not None:
            emit(ProgressEvent(type=EVENT_EXEC, text="done", data={"phase": "done"}))
        return DeepResult(met=True, output="goal met: the change was applied")


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# Unit-level: phase classification (the execution-record building block).
# ---------------------------------------------------------------------------

def test_classify_exec_phase():
    assert classify_exec_phase("done") == "success"
    assert classify_exec_phase("error") == "failure"
    assert classify_exec_phase("retry") is None
    assert classify_exec_phase(None) is None


# ---------------------------------------------------------------------------
# 1) The verification receives the EXECUTION RECORD (claims rules in the prompt).
# ---------------------------------------------------------------------------

def test_answer_verification_includes_execution_record():
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "info"}],
        goal_verdicts=[{"met": True}],
        answer_text="Your quest has three goals and is roughly half complete.",
    )
    res = _orch(provider, StubRetrieval()).run("how is my quest doing overall these days?")
    assert res.kind == "answer"
    assert provider.verify_calls >= 1
    # The claims rules + record ride in the SAME goal-verification call (no separate check).
    assert "EXECUTION RECORD" in provider.verify_prompts[0]
    assert "NO action/operation executed this turn" in provider.verify_prompts[0]
    assert res.claim_corrected is False
    assert res.text == "Your quest has three goals and is roughly half complete."


# ---------------------------------------------------------------------------
# 2) False claim + NOTHING executed -> execute for real via deep, then re-verify.
# ---------------------------------------------------------------------------

def test_unbacked_claim_with_nothing_executed_runs_the_work_for_real():
    # An informational question (no change-intent fallback fires), yet the answer falsely claims a
    # completed change. The verdict flags claims_unexecuted; nothing ran, so the loop executes the
    # work via the deep runner, folds the real output back in, and re-verifies.
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "hallucinated completion"}],
        goal_verdicts=[{"met": False, "claims_unexecuted": True,
                        "reason": "claims a file edit the record does not show"},
                       {"met": True}],
        answer_text="I have now directly updated and written the changes to the script.",
        synth_text="The script has now really been updated; the deep run applied the change.",
    )
    runner = SucceedingDeepRunner()
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run(
        "what is the status of the start script update?")
    assert res.kind == "answer"
    assert runner.calls == 1                       # the claimed work was executed for real
    assert res.claim_corrected is False            # claim became true, no honest correction needed
    assert res.partial is False
    assert "really been updated" in res.text       # the reply reports the actual executed work
    assert res.execution_record.any_success is True
    outcome = _report_via_executor(res)
    assert outcome.status == "done"


# ---------------------------------------------------------------------------
# 3) False claim + a REAL attempt already failed -> NO re-run, honest correction.
# ---------------------------------------------------------------------------

def test_unbacked_claim_after_failed_execution_corrects_honestly_without_rerun():
    # A change-requesting message: the message-intent fallback runs the deep work, which FAILS.
    # The answer still claims success, so the verdict flags claims_unexecuted; because a real
    # attempt was made (double-mutation risk), the loop must NOT re-run — it regenerates the reply
    # to be honest and flags the result.
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "claimed without doing"}],
        goal_verdicts=[{"met": False, "claims_unexecuted": True,
                        "reason": "record shows the action FAILED"},
                       {"met": True}],
        answer_text="I've added the goal to your quest.",
        honest_text="I was not able to add the goal, so I have not made the change.",
    )
    runner = FailingDeepRunner()
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("add a goal")
    assert res.kind == "answer"
    assert runner.calls == 1                       # the one real (failed) attempt; never re-run
    assert provider.honest_rewrites == 1
    assert res.claim_corrected is True
    assert res.partial is True
    assert "not able to add" in res.text
    assert "I've added the goal" not in res.text
    outcome = _report_via_executor(res)
    assert outcome.status == "needs_you"


# ---------------------------------------------------------------------------
# 4) SAFETY: an already-SUCCESSFUL action is never re-run either.
# ---------------------------------------------------------------------------

def test_unbacked_claim_after_successful_execution_never_reruns():
    # The deferred deep run SUCCEEDS, but the verdict (hypothetically) still flags an unbacked
    # claim (e.g. the reply also claims a second change that never ran). Re-running risks a double
    # mutation, so the loop corrects the reply honestly instead.
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "overstates"}],
        goal_verdicts=[{"met": False, "claims_unexecuted": True,
                        "reason": "the emailed-the-team claim is unbacked"},
                       {"met": True}],
        answer_text="I've added the goal and emailed the team.",
        honest_text="The goal was added. I did not email the team.",
    )
    runner = SucceedingDeepRunner()
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("add a goal")
    assert runner.calls == 1                       # only the original run; no remediation re-run
    assert res.claim_corrected is True
    assert res.partial is True
    assert "did not email" in res.text


# ---------------------------------------------------------------------------
# 5) A real successful deep turn passes through untouched (mapped by DeepResult.met).
# ---------------------------------------------------------------------------

def test_successful_deep_turn_passes_through_unchanged():
    provider = GuardProvider(
        plan_decisions=[{"action": "deep", "goal": "Add the goal", "deep_brief": "add it",
                         "rationale": "real work"}],
    )
    runner = SucceedingDeepRunner()
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("add a goal")
    assert res.kind == "deep"
    assert res.deep_results[0].met is True
    assert res.claim_corrected is False
    assert res.execution_record is not None
    assert res.execution_record.any_success is True


# ---------------------------------------------------------------------------
# Verification never breaks the turn: a verdict call that explodes degrades to accept.
# ---------------------------------------------------------------------------

class ExplodingProvider(GuardProvider):
    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "goal_verdict":
            raise RuntimeError("verify model down")
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def test_verification_degrades_gracefully_when_verifier_fails():
    provider = ExplodingProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        answer_text="Your quest has three goals in total right now.",
    )
    res = _orch(provider, StubRetrieval()).run("how many goals does my quest have right now?")
    assert res.kind == "answer"
    assert res.text == "Your quest has three goals in total right now."
    assert res.claim_corrected is False


# ---------------------------------------------------------------------------
# Config: verify_claims=False (with the goal loop off) means zero verification calls.
# ---------------------------------------------------------------------------

def test_verify_claims_disabled_skips_verification():
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        answer_text="Your quest has three goals in total right now.",
    )
    cfg = OrchestratorConfig(verify_claims=False, answer_goal_max_iterations=1)
    res = _orch(provider, StubRetrieval(), config=cfg).run(
        "how many goals does my quest have right now?")
    assert provider.verify_calls == 0
    assert res.claim_corrected is False
    assert res.text == "Your quest has three goals in total right now."


# ---------------------------------------------------------------------------
# helper: drive the result through the real executor's report mapping.
# ---------------------------------------------------------------------------

class _RecordingClient:
    def __init__(self):
        self.calls: List[str] = []

    def report_done(self, task_id, result):
        self.calls.append("done")

    def report_needs_you(self, task_id, summary, decision_id):
        self.calls.append("needs_you")

    def report_failed(self, task_id, msg):
        self.calls.append("failed")


def _report_via_executor(result):
    client = _RecordingClient()
    ex = TaskExecutor(client, orchestrator=None)  # _report does not touch the orchestrator
    return ex.report("task1", result)
