"""A run that was NOT verified and changed nothing must not read like completed work.

A consumer's chat eval (2026-10-07) produced the worst possible pairing twice in one pass: the
runner's own output text announced a change ("I updated the goal ... with a deadline of October
30"), the verifier returned not met with exactly the right reason (no execution record for the
claimed write), and the turn surfaced that text verbatim as its result. The goal loop already had
both facts in structured form and threw them away at the one place that writes the reader's text.

The gate is structural, per hard rule #3: it reads ``DeepResult.met`` (written by the goal loop
from the verifier's own verdict, false also when verification could not run) and
``DeepResult.changed_nothing`` (set by the runner from its own write receipts). It never inspects
the wording of the output.
"""

from __future__ import annotations

from typing import Any, Dict, List

from quest_ai_runner.core.adapters import EVENT_RESULT, DeepResult, Mode, StreamSink
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import (
    UNCONFIRMED_NO_CHANGE_LEAD,
    Orchestrator,
    OrchestratorConfig,
    unconfirmed_no_change_text,
)
from tests.conftest import StubEscalation, StubRetrieval

CLAIM = "I updated the goal with a deadline of 30 October."


class _OneDeepThenVerdict:
    """A ModelProvider that plans one deep goal and answers the verifier with a fixed verdict.

    The two calls are told apart by the tool schema the orchestrator passes, never by the prompt
    text: the verifier is the call carrying the ``goal_verdict`` schema.
    """

    def __init__(self, verdict: Dict[str, Any]):
        self._verdict = verdict
        self._planned = False
        self.verify_calls = 0

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if (tool_schema or {}).get("name") == "goal_verdict":
            self.verify_calls += 1
            return dict(self._verdict)
        if not self._planned:
            self._planned = True
            return {"action": "deep", "goal": "move the goal to 30 October",
                    "deep_brief": "set the deadline", "rationale": "the user asked for a change"}
        return {"action": "answer", "rationale": "done"}

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"


class _FixedRunner:
    """A deep runner that returns a prepared result and is out of moves after one attempt."""

    uses_deep_model = False

    def __init__(self, result: DeepResult):
        self._result = result

    def run_goal(self, *, goal, brief, model=None, max_turns=None, **kwargs) -> DeepResult:
        return self._result


class _Sink(StreamSink):
    def __init__(self) -> None:
        self.events: List[Dict[str, Any]] = []
        super().__init__(self.events.append)

    def result_texts(self) -> List[str]:
        return [(e.get("text") or "") for e in self.events if e.get("type") == EVENT_RESULT]


def _run(result: DeepResult, verdict: Dict[str, Any]):
    provider = _OneDeepThenVerdict(verdict)
    orch = Orchestrator(
        retrieval=StubRetrieval({}),
        provider=provider,
        registry=ModelRegistry(provider),
        escalation=StubEscalation(),
        deep_runner=_FixedRunner(result),
        config=OrchestratorConfig(max_steps=3),
    )
    sink = _Sink()
    res = orch.run("move the goal to 30 October", mode=Mode.LIVE, sink=sink)
    return res, sink


# ---------------------------------------------------------------------------
# The helper itself: both facts are required, and neither is read from the words.
# ---------------------------------------------------------------------------

def test_the_text_of_an_unconfirmed_no_change_result_leads_with_the_honest_line():
    text = unconfirmed_no_change_text(
        DeepResult(met=False, output=CLAIM, exhausted=True, changed_nothing=True))

    assert text.startswith(UNCONFIRMED_NO_CHANGE_LEAD)
    assert CLAIM in text                       # what the runner said is shown, not hidden
    assert "—" not in text


def test_a_met_result_or_a_real_change_is_passed_through_unchanged():
    met = DeepResult(met=True, output=CLAIM, changed_nothing=True)
    changed = DeepResult(met=False, output=CLAIM, exhausted=True, changed_nothing=False)

    assert unconfirmed_no_change_text(met) == CLAIM
    assert unconfirmed_no_change_text(changed) == CLAIM


# ---------------------------------------------------------------------------
# End to end through the goal loop: a verified not-met plus no change.
# ---------------------------------------------------------------------------

def test_a_not_met_verdict_with_no_change_never_surfaces_the_claim_on_its_own():
    res, sink = _run(
        DeepResult(met=True, output=CLAIM, exhausted=True, changed_nothing=True),
        {"met": False, "reason": "no execution record backs the claimed update",
         "blocker": "needs_person", "question": "which goal did you mean?"})

    assert res.kind == "deep"
    assert res.deep_results and res.deep_results[0].met is False   # the verdict won
    surfaced = [t for t in sink.result_texts() if t]
    assert surfaced, "the turn surfaced no result text at all"
    assert all(not t.startswith(CLAIM) for t in surfaced), (
        f"the claim was surfaced as the turn's outcome: {surfaced!r}")
    assert any(UNCONFIRMED_NO_CHANGE_LEAD in t for t in surfaced)


def test_a_met_verdict_still_surfaces_the_runners_own_text():
    res, sink = _run(
        DeepResult(met=True, output=CLAIM, exhausted=True, changed_nothing=False),
        {"met": True, "reason": "the goal's deadline was changed"})

    assert res.deep_results[0].met is True
    assert any(t == CLAIM for t in sink.result_texts())
