"""The overseer's ANSWER CHECKPOINT (hook B) escalates a drafted answer to deep execution when the
draft only describes work instead of doing it (``_bsig.signal == "escalate_deep"`` at the answer
checkpoint in ``Orchestrator.run``). This is the exact shape of a real failure: the planner reads
the right data, drafts an answer that states a correct number worked out from it, and the overseer
then decides the request actually needs a write ("if over the cap, add a goal ..."), so it hands
the turn to the deep code runner instead of shipping the draft.

That hand-off must carry forward what the brain already read (``gathered``) as the deep runner's
``context_preamble`` -- the same generic mechanism hook A and a direct ``action="deep"`` plan
already use (see test_deep_gathered.py) -- or the deep runner re-derives everything from scratch
and can get it wrong (the wrong collection, a zero total from a bad field lookup). This test proves
the HOOK-B escalation path specifically, since no existing test drives an answer-then-escalate
turn end to end.
"""
from typing import Any, Dict, List, Optional

from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval

OVERSEER_MARK = "OVERSEER"


class OverseerStubProvider(StubProvider):
    """Same split as test_overseer.py's own helper (not imported from there to keep this file
    self-contained): overseer-prompt plan() calls draw from ``overseer_signals``, everything else
    (planner steps, goal verification) from the ordinary ``decisions`` queue."""

    def __init__(self, decisions: List[Dict[str, Any]], *,
                 overseer_signals: Optional[List[Dict[str, Any]]] = None):
        super().__init__(decisions)
        self.overseer_signals = list(overseer_signals or [])

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if OVERSEER_MARK in prompt and "minimal-intervention" in prompt.lower():
            if self.overseer_signals:
                return self.overseer_signals.pop(0)
            return {"signal": "proceed"}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


class PreambleCapturingRunner:
    """A DeepRunner that accepts context_preamble and records what it received."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None,
                 context_preamble: Optional[str] = None) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "context_preamble": context_preamble})
        return DeepResult(met=True, output="added the goal")


def test_answer_checkpoint_escalation_forwards_gathered_facts_as_context_preamble():
    # Step 1: the planner reads the relevant collection (this is what populates ``gathered``).
    # Step 2: the planner drafts an "answer" from it (a correct number, but no write happened).
    # Step 3: the goal-verification call for the deep run the overseer escalates to.
    decisions = [
        {"action": "read", "reads": [{"rel_path": "expenses.md"}], "rationale": "read the log"},
        {"action": "answer", "rationale": "report the total against the cap"},
        {"met": True, "reason": "added the goal"},
    ]
    provider = OverseerStubProvider(
        decisions,
        overseer_signals=[{"signal": "escalate_deep", "reason": "this needs a real write"}],
    )
    retrieval = StubRetrieval({
        "expenses.md": ("GROUNDING Launch expenses collection id: coll_launch_expenses_42. "
                         "Spend cap this quarter: $5,000. Spent so far: $5,430."),
    })
    runner = PreambleCapturingRunner()

    orch = Orchestrator(
        retrieval=retrieval,
        provider=provider,
        registry=ModelRegistry(provider),
        deep_runner=runner,
        # overseer_min_step=99 keeps hook A (the in-loop poll) from ever firing, so the single
        # overseer_signal above is unambiguously consumed by hook B (the answer checkpoint) --
        # the exact site this test is about.
        config=OrchestratorConfig(overseer=True, overseer_min_step=99, max_steps=10),
    )
    res = orch.run("Add up my launch expenses against the cap; if I'm over, add a goal.")

    assert res.kind == "deep"
    assert res.exit_reason == "overseer_escalated_deep"
    assert runner.calls, "the deep runner was never called"
    preamble = runner.calls[0]["context_preamble"]
    assert preamble is not None, "gathered facts never reached the deep runner's context_preamble"
    assert "coll_launch_expenses_42" in preamble
    assert "Spent so far: $5,430" in preamble
