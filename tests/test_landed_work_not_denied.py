"""A reply must never deny work the execution record proves landed.

Live finding (dev, 2026-10-18): a deferred run created a tracker (write receipt, code-written
account "Added the collection ..."), but the model that rewrote the reply took the pre-work
proposal ("nothing has been created yet") as the truth and told the person the change was not
made. The goal loop's steered regeneration could do the same to a correct reply. The guard
(``keep_landed_work_reported``) replaces a denying rewrite with the run's own account.

Offline, no network.
"""
from typing import Any, Dict

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubDeepRunner, StubProvider, StubRetrieval

ACCOUNT = "Added the collection. I created the tracker Weekly count and linked it to your quest."
DENIAL = "The tracker is not created yet. Reply go ahead and I will create it."


class DenyingProvider(StubProvider):
    """Writes a proposal that says nothing was made, then a post-run rewrite that keeps saying so
    (the bug). The denial judge answers from ``judge_says``; ``judge_calls`` counts its calls."""

    def __init__(self, judge_says, **kw):
        super().__init__(decisions=[{"action": "answer", "model_tier": "sonnet",
                                     "rationale": "describe", "user_intent": "act"}],
                         answer_text=DENIAL, **kw)
        self.judge_says = judge_says
        self.judge_calls = 0
        self.run_verified = True

    def answer(self, messages, *, model, system=None) -> str:
        super().answer(messages, model=model, system=system)
        return DENIAL

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any]) -> Dict[str, Any]:
        if tool_schema.get("name") == "landed_work_denial_verdict":
            self.judge_calls += 1
            if isinstance(self.judge_says, Exception):
                raise self.judge_says
            return {"denies_landed_work": self.judge_says(prompt)}
        if tool_schema.get("name") == "goal_verdict":
            # The deep run's own goal loop: the run is verified met, so the record shows success.
            return {"met": self.run_verified, "reason": "ok", "next_action": "", "claims_unexecuted": False}
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def run_turn(provider, runner_output=ACCOUNT, met=True):
    cfg = OrchestratorConfig()
    cfg.overseer = False
    cfg.verify_claims = False
    cfg.answer_goal_max_iterations = 1
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), config=cfg,
                        deep_runner=StubDeepRunner(met=met, output=runner_output))
    return orch.run("Create the tracker for me and tell me exactly what you did.")


def test_reply_denying_landed_work_is_replaced_by_the_runs_own_account():
    provider = DenyingProvider(judge_says=lambda prompt: DENIAL in prompt)
    res = run_turn(provider)
    assert res.execution_record.any_success
    assert res.text == ACCOUNT, res.text
    assert provider.judge_calls >= 1


def test_reply_that_reports_the_work_is_kept():
    provider = DenyingProvider(judge_says=lambda prompt: False)
    res = run_turn(provider)
    assert res.text != ACCOUNT
    assert provider.judge_calls >= 1


def test_judge_outage_keeps_the_reply():
    provider = DenyingProvider(judge_says=RuntimeError("provider outage"))
    res = run_turn(provider)
    assert res.text != ACCOUNT


def test_no_judgment_is_made_when_nothing_landed():
    provider = DenyingProvider(judge_says=lambda prompt: True)
    provider.run_verified = False
    res = run_turn(provider, runner_output="could not do it", met=False)
    assert provider.judge_calls == 0
    assert res.text != ACCOUNT
