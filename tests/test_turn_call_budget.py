"""What a "1 step" turn actually costs: the provider round trips hidden behind that one step.

A production turn that the UI reported as "1 step" took 83 seconds against fully prebuilt context.
"1 step" counts PLANNER LOOP iterations, and a turn makes several model calls that are not loop
iterations, so the number told the reader almost nothing about the wait. These tests pin the real
budget for the simplest possible turn (planner answers at step 0, nothing read), so the count
cannot grow silently again, and assert the turn does not pay for its context twice.

The breakdown they lock in, in call order, all SEQUENTIAL and all on the critical path:

  1. goal-condition derivation (``_derive_goal_condition``, cheap tier)  -- an ``answer`` call
  2. the planner                (``_plan``, planner tier)                -- a ``plan`` call
  3. the answer                 (``_grounded_answer``, answer tier)      -- an ``answer`` call
  4. goal verification          (``_verify_goal``, verify tier: the STRONGEST model, over the same
                                 context the answer saw)                 -- a ``plan`` call

So: four model round trips, the last one at the most expensive tier, for one reported step. None of
them is duplicated work and none of them re-sends the context view twice (asserted below); the
latency is four real generations, not a payload or retry problem.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.adapters import AssembledContext
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval

CONTEXT_MARKER = "ZZMARKERZZ the pricing page has no call to action"


class OneCardAssembler:
    """A ContextAssembler with prebuilt context: one card, no cost, nothing to discover."""

    def assemble(self, task_text: str, meta: Dict[str, Any] | None = None) -> AssembledContext:
        return AssembledContext(context_view=f"## Card: gaps\n  - (note) findings\n      {CONTEXT_MARKER}",
                                card_ids=["card_gaps"],
                                card_metadata=[{"id": "card_gaps", "title": "gaps", "items": []}])

    def record(self, *a: Any, **kw: Any) -> None:  # pragma: no cover - unused here
        return None


def _run_one_step_turn(provider: StubProvider, *, overseer: bool = False):
    # overseer=False pins the LOOP budget the docstring above describes. The overseer is ON by
    # default in the library; its own added cost is pinned separately by the last test in this file,
    # so the two numbers stay independently visible instead of one hiding inside the other.
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider),
                        config=OrchestratorConfig(overseer=overseer),
                        context_assembler=OneCardAssembler())
    return orch.run("what should I do next")


def test_a_one_step_answer_turn_costs_four_provider_round_trips():
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "the context has it"},
        {"met": True, "reason": "answered from the card"},   # goal verification
    ])
    res = _run_one_step_turn(provider)

    assert res.kind == "answer"
    assert res.steps == 1, "the UI's step count is planner-loop iterations, not model calls"
    # 1 planner + 1 verification.
    assert provider.plan_calls == 2
    # 1 goal-condition derivation + 1 real answer.
    assert provider.answer_calls == 2


def test_the_context_view_is_sent_once_per_call_not_twice():
    # ``_plan`` and ``_grounded_answer`` each build BOTH a flattened prompt and a layered form; only
    # one of the two may reach the provider, or every turn would pay for its context twice.
    provider = StubProvider(decisions=[
        {"action": "answer", "model_tier": "sonnet", "rationale": "have it"},
        {"met": True, "reason": "ok"},
    ])
    _run_one_step_turn(provider)

    assert provider.plan_prompts[0].count(CONTEXT_MARKER) == 1
    answer_payloads: List[str] = [
        "\n".join(str(m["content"]) for m in msgs) for msgs in provider.all_answer_messages]
    for payload in answer_payloads:
        assert payload.count(CONTEXT_MARKER) <= 1


def test_a_failed_verification_costs_one_extra_call_not_a_retry_storm():
    # The verifier's tier ladder (verify_tier, then planner_tier) is ONE fallback attempt, not a
    # loop: an unusable verdict must not multiply into repeated verification passes.
    provider = StubProvider(decisions=[
        {"action": "answer", "rationale": "have it"},
        {"garbage": True},          # verify attempt 1: no "met" key
        {"garbage": True},          # verify attempt 2 (planner-tier fallback)
    ])
    res = _run_one_step_turn(provider)
    assert res.kind == "answer"
    assert provider.plan_calls == 3   # 1 planner + 2 verify attempts, then it proceeds unverified


def test_the_on_by_default_overseer_costs_exactly_one_extra_call_on_a_simple_turn():
    """The overseer is ON by default, so its price belongs in this file's budget too.

    On the simplest possible turn that price is ONE call, not three: hook A (the in-loop watch) is
    gated by a free non-LLM pre-filter (consecutive reads, a repeated plan, or budget pressure) and
    none of those trip on a one-step answer, so only hook B (the answer checkpoint, which always
    consults) fires. Hook A's extra consults appear on the runs that actually drift, capped at
    ``overseer_max_signals``. If this number grows, the default got more expensive for every turn.
    """
    from .test_overseer import OverseerStubProvider

    def one_turn(overseer: bool):
        provider = OverseerStubProvider(decisions=[
            {"action": "answer", "model_tier": "sonnet", "rationale": "the context has it"},
            {"met": True, "reason": "answered from the card"},
        ])
        res = _run_one_step_turn(provider, overseer=overseer)
        assert res.kind == "answer"
        return provider

    off, on = one_turn(False), one_turn(True)

    assert off.overseer_calls == 0          # opted out: byte-for-byte the old loop
    assert on.overseer_calls == 1           # the default: one answer-checkpoint consult
    # and it costs nothing anywhere else: the loop's own budget is untouched.
    assert on.plan_calls == off.plan_calls == 2
    assert on.answer_calls == off.answer_calls == 2
