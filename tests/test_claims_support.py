"""The claims check also judges SUPPORT: facts and advice stated as certain with nothing behind them.

It rides the SAME goal verdict call as the completed-change honesty check (no extra call). A reply
the verifier flags ``claims_unsupported`` is rewritten ONCE to state its basis, hedge, or drop the
claim; the rewrite is bounded, never starts a deep run, and the check can be switched off.
"""
from __future__ import annotations

from typing import Any, Dict, List

from quest_ai_runner.world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
from quest_ai_runner.intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_guard import GuardProvider, SucceedingDeepRunner

UNSUPPORTED = {"met": False, "claims_unsupported": True,
               "reason": "states that 8 hours of sleep cures anxiety as certain, no source"}
ADVICE = "Sleeping eight hours will cure your anxiety, so go to bed early."
REWRITTEN = "Sleep is linked to anxiety in research, though I have no evidence it cures it for you."


class SupportProvider(GuardProvider):
    """GuardProvider that also recognizes the support rewrite steer."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.support_rewrites = 0

    def answer(self, messages, *, model, system=None) -> str:
        joined = "\n".join(m["content"] if isinstance(m["content"], str) else "" for m in messages)
        if "claims that nothing from this turn supports" in joined:
            self.support_rewrites += 1
            return REWRITTEN
        return super().answer(messages, model=model, system=system)


def run(provider, message="how can I get over my anxiety?", **cfg):
    runner = SucceedingDeepRunner()
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=OrchestratorConfig(**cfg),
                        deep_runner=runner)
    return orch.run(message), runner


def test_unsupported_claim_is_rewritten_once_without_a_deep_run():
    provider = SupportProvider(
        plan_decisions=[{"action": "answer", "rationale": "advice"}],
        goal_verdicts=[UNSUPPORTED, UNSUPPORTED, UNSUPPORTED],
        answer_text=ADVICE)
    res, runner = run(provider)
    assert res.kind == "answer"
    assert provider.support_rewrites == 1          # bounded: exactly one rewrite
    assert res.text == REWRITTEN
    assert runner.calls == 0                       # never escalates to deep over an unsupported claim
    assert provider.verify_calls == 1              # the same single verdict call, nothing added


def test_supported_reply_is_untouched():
    provider = SupportProvider(
        plan_decisions=[{"action": "answer", "rationale": "advice"}],
        goal_verdicts=[{"met": True}], answer_text="Your quest has three goals.")
    res, _ = run(provider)
    assert provider.support_rewrites == 0
    assert res.text == "Your quest has three goals."


def test_support_rules_ride_the_claims_prompt_and_can_be_switched_off():
    on = SupportProvider(plan_decisions=[{"action": "answer", "rationale": "x"}],
                         goal_verdicts=[{"met": True}])
    run(on)
    assert "SUPPORT FOR CLAIMS" in on.verify_prompts[0]

    off = SupportProvider(plan_decisions=[{"action": "answer", "rationale": "x"}],
                          goal_verdicts=[{"met": True, "claims_unsupported": True}], answer_text=ADVICE)
    res, _ = run(off, verify_support=False)
    assert "SUPPORT FOR CLAIMS" not in off.verify_prompts[0]
    assert off.support_rewrites == 0               # a verdict flag is ignored when the check is off
    assert res.text == ADVICE


def test_verdict_carries_the_flag_and_shows_what_was_read():
    provider = SupportProvider(plan_decisions=[])
    seen: List[str] = []

    def plan(prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        seen.append(prompt)
        return {"met": False, "claims_unsupported": True, "reason": "no source"}

    provider.plan = plan  # type: ignore[assignment]
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider))
    from hubofhubs.api import ExecutionRecord
    gathered = [{"kind": "read", "locator": "notes/sleep.md", "text": "Sleep log: 6 hours a night."}]
    verdict, error = orch._verify_goal(
        "answer", "how do I sleep?", "You sleep 6 hours.", exec_record=ExecutionRecord(),
        gathered=gathered, check_support=True)
    assert error is None and verdict["claims_unsupported"] is True
    assert "WHAT WAS READ THIS TURN" in seen[0] and "Sleep log: 6 hours a night." in seen[0]

    seen.clear()
    verdict, _ = orch._verify_goal(
        "answer", "how do I sleep?", "You sleep 6 hours.", exec_record=ExecutionRecord(),
        gathered=gathered, check_support=False)
    assert verdict["claims_unsupported"] is False
    assert "WHAT WAS READ THIS TURN" not in seen[0] and "SUPPORT FOR CLAIMS" not in seen[0]


def test_deep_goal_verification_prompt_is_unchanged():
    provider = SupportProvider(plan_decisions=[])
    seen: List[str] = []
    provider.plan = lambda prompt, *, model, tool_schema: (seen.append(prompt) or {"met": True})  # type: ignore
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider))
    orch._verify_goal("goal", "brief", "worker output", check_support=True)
    assert "SUPPORT FOR CLAIMS" not in seen[0]      # no execution record, so no claims rules at all


def test_unsupported_flag_alongside_a_real_gap_does_not_trigger_the_support_rewrite():
    """A real gap (more context needed) is the goal loop's job; the rewrite is for support only."""
    both = {"met": False, "claims_unsupported": True, "need_more_context": True,
            "context_query": "sleep and anxiety", "reason": "also missing the person's own data"}
    provider = SupportProvider(
        plan_decisions=[{"action": "answer", "rationale": "advice"}],
        goal_verdicts=[both, {"met": True}], answer_text=ADVICE)
    run(provider)
    assert provider.support_rewrites == 0


def test_support_rewrite_keeps_a_standard_fact_that_answers_the_question():
    from quest_ai_runner.self.integrity.prompts.auxiliary_prompts import SUPPORT_REWRITE_STEER
    steer = SUPPORT_REWRITE_STEER.format(reason="a conversion stated as certain", text="350 F is about 175 C.")
    assert "unit conversion" in steer and "never removed" in steer


def test_support_rewrite_lost_answer_guard():
    from quest_ai_runner.intelligence.mind.response_writer.orchestrator_run_close import support_rewrite_lost_answer
    draft = "500 ml is about 16.9 US fluid ounces, or about 2.1 US cups."
    assert support_rewrite_lost_answer(draft, "General knowledge.")
    assert not support_rewrite_lost_answer(draft, "About 16.9 ounces, general knowledge.")
    assert not support_rewrite_lost_answer("Go to bed early.", "No evidence for that.")
    # A removed claim about the person's own data stays removed.
    assert not support_rewrite_lost_answer("You logged 12 hours of meditation this week.", "I can't see your logs.")
    # A rewrite that mentions general knowledge but also replaces the answer is not a bare label.
    logs = ("You logged 14 workouts this month and your average sleep was 6.2 hours, which is why you feel tired. "
            "Keep the pace for the next three weeks and you will see steady improvement across every category you track.")
    assert not support_rewrite_lost_answer(logs, "I can't see your logs. As general knowledge, sleep matters.")
    long_draft = draft + " A metric cup is 250 ml, so 500 ml is two metric cups, slightly more than two US cups."
    assert support_rewrite_lost_answer(long_draft, "General knowledge, not checked against your data.")
    assert support_rewrite_lost_answer(long_draft, "This is general knowledge.")


def test_support_rewrite_keeps_a_drafted_deliverable_whole():
    from quest_ai_runner.self.integrity.prompts.auxiliary_prompts import SUPPORT_REWRITE_STEER
    from quest_ai_runner.intelligence.mind.response_writer.orchestrator_run_close import support_rewrite_shrank_deliverable
    assert "deliverable" in SUPPORT_REWRITE_STEER.format(reason="x", text="y")
    bio = "Dana teaches gentle morning yoga. Her classes start slowly. Everyone is welcome to join her."
    assert support_rewrite_shrank_deliverable(bio, "Dana teaches gentle morning yoga.")
    assert not support_rewrite_shrank_deliverable(bio, bio.replace("Everyone is welcome to join her.", "All are welcome."))


BIO = ("Dana teaches gentle morning yoga and has a PhD from Oxford. Her classes start slowly and build over an hour. "
       "Everyone is welcome to join her, and no experience is needed.")
BIO_SHORT = "Dana teaches gentle morning yoga."
BIO_FIXED = ("Dana teaches gentle morning yoga. Her classes start slowly and build over an hour. "
             "Everyone is welcome to join her, and no experience is needed.")


class DraftProvider(SupportProvider):
    """A drafted bio, a support rewrite that shrinks it, and a keep-whole retry (never restores the flagged claim)."""

    def __init__(self, retry_text: str, **kw):
        super().__init__(**kw)
        self.retry_text = retry_text
        self.keep_whole_retries = 0

    def answer(self, messages, *, model, system=None) -> str:
        joined = "\n".join(m["content"] if isinstance(m["content"], str) else "" for m in messages)
        if "cut the requested text far shorter" in joined:
            self.keep_whole_retries += 1
            return self.retry_text
        if "claims that nothing from this turn supports" in joined:
            self.support_rewrites += 1
            return BIO_SHORT
        return super().answer(messages, model=model, system=system)


def _run_bio(retry_text):
    provider = DraftProvider(
        retry_text,
        plan_decisions=[{"action": "answer", "rationale": "write the bio", "deep_target": "draft_in_chat"}],
        goal_verdicts=[{"met": False, "claims_unsupported": True, "reason": "invents a PhD from Oxford"}] * 3,
        answer_text=BIO)
    res, _ = run(provider, message="write me a short bio for Dana")
    return provider, res


def test_shrunk_drafted_deliverable_is_retried_whole_without_the_flagged_claim():
    provider, res = _run_bio(BIO_FIXED)
    assert provider.support_rewrites == 1 and provider.keep_whole_retries == 1
    assert res.text == BIO_FIXED and "PhD" not in res.text


def test_shrunk_drafted_deliverable_still_short_after_retry_never_restores_the_claim():
    provider, res = _run_bio(BIO_SHORT)
    assert provider.keep_whole_retries == 1        # one retry only
    assert res.text == BIO_SHORT and "PhD" not in res.text


def test_an_empty_keep_whole_retry_never_replaces_the_first_rewrite():
    provider, res = _run_bio("")
    assert provider.keep_whole_retries == 1
    assert res.text == BIO_SHORT and "PhD" not in res.text
