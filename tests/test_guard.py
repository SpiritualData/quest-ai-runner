"""Broken-promise guard (workstream 5) — detection, remediation, honest correction, safety.

The guard runs at turn finalization. It detects when an ANSWER reply CLAIMS an action that did not
actually execute or finish, AUTO-REMEDIATES (one safe re-run) then re-verifies, and if still unmet
rewrites the reply to be honest and flags the result so a background task maps to needs_you (not
done). It NEVER re-runs an action that already executed (no double mutation), and on a turn with no
action claim it does ZERO verification (no model cost).
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import (
    EVENT_EXEC,
    DeepResult,
    MilestoneSink,
    Mode,
    ProgressEvent,
    StreamSink,
)
from quest_ai_runner.core.guard import (
    ExecutionFact,
    ExecutionRecord,
    classify_exec_phase,
    text_claims_action,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.runner.executor import TaskExecutor

from .conftest import StubRetrieval


# ---------------------------------------------------------------------------
# A provider that distinguishes the PLANNER call, the VERIFY call, and ANSWER.
# ---------------------------------------------------------------------------

class GuardProvider:
    """ModelProvider with separate scripted streams for plan-decisions and verify-verdicts.

    ``plan()`` is used for BOTH the planner step and the guard's verification call. We tell them
    apart by the prompt: the guard's verification prompt contains the marker "honesty checker".
    """

    def __init__(self, *, plan_decisions: List[Dict[str, Any]],
                 verify_verdicts: Optional[List[Dict[str, Any]]] = None,
                 answer_text: str = "Plain answer.",
                 rewrite_text: str = "I was not able to make that change."):
        self._plan = list(plan_decisions)
        self._verify = list(verify_verdicts or [])
        self._answer_text = answer_text
        self._rewrite_text = rewrite_text
        self.plan_calls = 0
        self.verify_calls = 0
        self.answer_calls = 0
        self.rewrite_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if "honesty checker" in prompt:
            self.verify_calls += 1
            if self._verify:
                return self._verify.pop(0)
            return {"verdict": "supported"}
        self.plan_calls += 1
        if self._plan:
            return self._plan.pop(0)
        return {"action": "answer", "rationale": "fallback"}

    def answer(self, messages, *, model, system=None) -> str:
        joined = "\n".join(
            m["content"] if isinstance(m["content"], str) else "" for m in messages)
        if "correcting an AI assistant" in joined or "HONEST" in joined:
            self.rewrite_calls += 1
            return self._rewrite_text
        self.answer_calls += 1
        return self._answer_text

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


class FailingDeepRunner:
    """A deep runner that ALWAYS fails (never meets the goal) — to drive remediation that stays unmet."""

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
        return DeepResult(met=True, output="goal met")


def _orch(provider, retrieval, **kw):
    return Orchestrator(retrieval=retrieval, provider=provider,
                        registry=ModelRegistry(provider), **kw)


# ---------------------------------------------------------------------------
# Unit-level: structural gate + phase classification.
# ---------------------------------------------------------------------------

def test_text_claims_action_detects_completed_and_future_claims():
    assert text_claims_action("I've added the goal for you.")
    assert text_claims_action("I created your new quest.")
    assert text_claims_action("Done. Your reflection is saved.")
    assert text_claims_action("I'll update that now.")
    assert text_claims_action("I am scheduling it for tomorrow.")


def test_text_claims_action_detects_adverb_separated_and_file_write_claims():
    # The real-world miss (2026-07-06): a reply claimed a file edit QAR cannot make itself,
    # phrased with adverbs between the auxiliary and the verb, so the guard never engaged.
    assert text_claims_action(
        "I have now directly updated and written the changes to "
        "start-dev-servers.sh to clean up port 3002.")
    assert text_claims_action("I have written the updated script to disk.")
    assert text_claims_action("I have now directly applied the fix.")
    assert text_claims_action("I just successfully staged and committed the change.")
    assert text_claims_action("We have updated the script accordingly.")


def test_text_claims_action_detects_passive_result_claims():
    assert text_claims_action("The file has been updated with the new tmux session.")
    assert text_claims_action("Your script is now updated and ready to run.")
    assert text_claims_action("The changes have already been written to the file.")


def test_text_claims_action_ignores_history_and_negation():
    # Simple past passive reports history, not this turn's work.
    assert not text_claims_action("The config was updated in version 2 last year.")
    # An honest not-done statement must not read as a completion claim.
    assert not text_claims_action(
        "The change has not been made yet; it still needs an execution run.")
    assert not text_claims_action("I have not updated the file yet.")


def test_text_claims_action_ignores_plain_answers():
    assert not text_claims_action("Your quest has three goals and looks on track.")
    assert not text_claims_action("Here is what the pricing page says: it is $9 a month.")
    assert not text_claims_action("Thanks, glad it helped!")
    assert not text_claims_action("")


def test_classify_exec_phase():
    assert classify_exec_phase("done") == "success"
    assert classify_exec_phase("error") == "failure"
    assert classify_exec_phase("retry") is None
    assert classify_exec_phase(None) is None


# ---------------------------------------------------------------------------
# 1) Claim NOT supported + remediation still fails -> honest reply, not done.
# ---------------------------------------------------------------------------

def test_unsupported_claim_remediates_then_corrects_honestly():
    # Planner ANSWERS (claiming it acted) without ever running a deep action -> nothing executed.
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "I will just say I did it"}],
        verify_verdicts=[{"verdict": "unsupported"}],  # the verify call: claim NOT backed
        answer_text="I've added the goal to your quest.",  # the (false) success claim
        rewrite_text="I was not able to add the goal, so I have not made the change.",
    )
    runner = FailingDeepRunner()  # remediation re-run FAILS -> claim stays unmet
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("add a goal")

    assert res.kind == "answer"
    # Guard engaged: it verified, attempted ONE remediation (the safe re-run, since nothing had run),
    # the re-run failed, so it rewrote the reply to be honest and flagged the result.
    assert provider.verify_calls == 1
    assert runner.calls == 1                       # exactly one remediation attempt
    assert res.claim_corrected is True
    assert res.partial is True
    assert "not able to add" in res.text           # honest, no false success
    assert "I've added the goal" not in res.text

    # The runner's executor maps a claim-corrected answer to needs_you, never done.
    outcome = _report_via_executor(res)
    assert outcome.status == "needs_you"


# ---------------------------------------------------------------------------
# 2) Claim supported by a real successful mutation -> pass-through, unchanged.
# ---------------------------------------------------------------------------

def test_supported_claim_passes_through_unchanged():
    # Planner runs a deep action that SUCCEEDS; the deep output legitimately reports success.
    provider = GuardProvider(
        plan_decisions=[{"action": "deep", "goal": "Add the goal", "deep_brief": "add it",
                         "rationale": "real work"}],
    )
    runner = SucceedingDeepRunner()
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("add a goal")
    # A deep result is mapped by DeepResult.met, not the answer guard — it stays met, no rewrite.
    assert res.kind == "deep"
    assert res.deep_results[0].met is True
    assert res.claim_corrected is False
    assert provider.verify_calls == 0              # guard only verifies ANSWER replies
    # And the execution record shows the real success.
    assert res.execution_record is not None
    assert res.execution_record.any_success is True


def test_answer_claim_supported_by_record_passes_through():
    # An ANSWER that claims success WHILE the record confirms a successful mutation: verify says
    # supported -> the reply and status are left exactly as-is.
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "report the success"}],
        verify_verdicts=[{"verdict": "supported"}],
        answer_text="I've updated your quest.",
    )
    orch = _orch(provider, StubRetrieval())
    # Seed a successful execution fact as if a prior step had executed (drive _guard_turn directly
    # for a focused check that a SUPPORTED verdict is a pure pass-through).
    res = orch.run("update my quest")
    record = res.execution_record
    record.facts.append(ExecutionFact(goal="update quest", succeeded=True))
    res.text = "I've updated your quest."
    res.partial = False
    res.claim_corrected = False
    orch._guard_turn(res, record, user_message="update my quest", plan=None,
                     model_hint=None, emit=None, rep_preamble=None)
    assert res.claim_corrected is False
    assert res.text == "I've updated your quest."


# ---------------------------------------------------------------------------
# 3) No action claim -> pass-through, NO verification model call.
# ---------------------------------------------------------------------------

def test_plain_answer_skips_verification_entirely():
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "chit-chat / info"}],
        answer_text="Your quest has three goals and is roughly half complete.",
    )
    res = _orch(provider, StubRetrieval()).run("how is my quest doing?")
    assert res.kind == "answer"
    assert provider.verify_calls == 0              # structural gate: no claim -> no verify call
    assert res.claim_corrected is False
    assert res.text == "Your quest has three goals and is roughly half complete."


# ---------------------------------------------------------------------------
# 4) SAFETY: an action that already executed is NEVER re-run (no double mutation).
# ---------------------------------------------------------------------------

def test_guard_never_reruns_an_action_that_already_executed():
    # A deep action runs once and FAILS (a real attempt happened). Then we drive the guard with an
    # ANSWER-shaped result whose record carries that failure. Because a real attempt was made, the
    # guard must NOT re-run (host actions are not idempotent) — it goes straight to honest correction.
    provider = GuardProvider(
        plan_decisions=[],  # not used; we call _guard_turn directly
        verify_verdicts=[{"verdict": "unsupported"}],
        rewrite_text="I tried to update it but it failed, so the change was not made.",
    )
    runner = SucceedingDeepRunner()   # if (wrongly) called, calls would increment
    orch = _orch(provider, StubRetrieval(), deep_runner=runner)

    record = ExecutionRecord()
    record.facts.append(ExecutionFact(goal="update quest", failed=True, error="boom"))
    from quest_ai_runner.core.orchestrator import OrchestratorResult
    res = OrchestratorResult(kind="answer", text="I've updated your quest.")

    # Provide a plan so remediation COULD run if the safety gate were wrong.
    from quest_ai_runner.core.adapters import PlanDecision
    plan = PlanDecision(action="deep", goal="Update quest", deep_brief="update")
    orch._guard_turn(res, record, user_message="update my quest", plan=plan,
                     model_hint=None, emit=None, rep_preamble=None)

    assert runner.calls == 0                       # NEVER re-ran the already-attempted action
    assert res.claim_corrected is True             # corrected honestly instead
    assert res.partial is True
    assert "I've updated your quest." not in res.text


def test_guard_does_not_rerun_on_already_successful_action():
    # Record shows a SUCCESS but verify (hypothetically) returns unsupported for some other claim.
    # The guard must not re-run a succeeded action (double-mutation risk) — it corrects honestly.
    provider = GuardProvider(
        plan_decisions=[],
        verify_verdicts=[{"verdict": "unsupported"}],
        rewrite_text="One part is done; I could not complete the rest.",
    )
    runner = SucceedingDeepRunner()
    orch = _orch(provider, StubRetrieval(), deep_runner=runner)
    from quest_ai_runner.core.orchestrator import OrchestratorResult
    from quest_ai_runner.core.adapters import PlanDecision
    record = ExecutionRecord()
    record.facts.append(ExecutionFact(goal="add goal", succeeded=True))
    res = OrchestratorResult(kind="answer", text="I've added two goals and emailed the team.")
    plan = PlanDecision(action="deep", goal="Add goals", deep_brief="add")
    orch._guard_turn(res, record, user_message="add goals and email", plan=plan,
                     model_hint=None, emit=None, rep_preamble=None)
    assert runner.calls == 0
    assert res.claim_corrected is True


# ---------------------------------------------------------------------------
# 5) Remediation SUCCEEDS -> claim becomes true, original reply kept, status done.
# ---------------------------------------------------------------------------

def test_remediation_success_keeps_original_reply():
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "claimed without doing"}],
        verify_verdicts=[{"verdict": "unsupported"}],
        answer_text="I've added the goal.",
    )
    runner = SucceedingDeepRunner()   # the safe re-run SUCCEEDS
    res = _orch(provider, StubRetrieval(), deep_runner=runner).run("add a goal")
    assert runner.calls == 1
    assert res.claim_corrected is False            # claim is now true; reply kept
    assert res.text == "I've added the goal."
    assert res.partial is False
    outcome = _report_via_executor(res)
    assert outcome.status == "done"


# ---------------------------------------------------------------------------
# Guard never raises: a verify call that explodes leaves the turn unchanged.
# ---------------------------------------------------------------------------

class ExplodingProvider(GuardProvider):
    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if "honesty checker" in prompt:
            raise RuntimeError("verify model down")
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def test_guard_degrades_gracefully_when_verifier_fails():
    provider = ExplodingProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        answer_text="I've added the goal.",
    )
    res = _orch(provider, StubRetrieval()).run("add a goal")
    # verify_supported swallows the error and returns supported -> reply unchanged, never crashes.
    assert res.kind == "answer"
    assert res.text == "I've added the goal."
    assert res.claim_corrected is False


# ---------------------------------------------------------------------------
# Config: verify_claims=False disables the guard entirely.
# ---------------------------------------------------------------------------

def test_verify_claims_disabled_skips_guard():
    provider = GuardProvider(
        plan_decisions=[{"action": "answer", "rationale": "x"}],
        answer_text="I've added the goal.",
    )
    cfg = OrchestratorConfig(verify_claims=False)
    res = _orch(provider, StubRetrieval(), config=cfg).run("add a goal")
    assert provider.verify_calls == 0
    assert res.claim_corrected is False
    assert res.text == "I've added the goal."


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
