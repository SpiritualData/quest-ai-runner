"""Every user-facing answer carries the per-turn ``rep_preamble``, not only the single-answer path.

``rep_preamble`` is the system prompt the consumer builds per turn (persona overlay, plan-mode
addenda such as a "documentation mode" block that tells the model to refuse off-topic requests).
The sub-question fan-out (and its merge) and the read-budget wrap-up used to call the answerer
without it, so a multi-part question was answered with no persona and none of those rules.
"""
from typing import Any, Dict, List

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import REPLY_VOICE_SYSTEM, Orchestrator, OrchestratorConfig

from .conftest import StubProvider, StubRetrieval

PREAMBLE = "PERSONA-AND-PLAN-RULES-7731: refuse off-topic requests and end with the marker."


def _orch(provider: StubProvider, **cfg: Any) -> Orchestrator:
    config = OrchestratorConfig(**cfg)
    config.overseer = False
    return Orchestrator(retrieval=StubRetrieval({"a.md": "GROUNDING alpha"}), provider=provider,
                        registry=ModelRegistry(provider), config=config)


def _calls_without_preamble(provider: StubProvider) -> List[List[Dict[str, Any]]]:
    # Only calls made under the reply-voice system prompt are user-facing answers; the internal
    # done-standard restatement and similar helper calls use their own system prompt.
    return [m for m, system in zip(provider.all_answer_messages, provider.answer_systems)
            if system and REPLY_VOICE_SYSTEM in system and PREAMBLE not in "\n".join(str(x.get("content")) for x in m)]


def test_subquestion_answers_and_merge_carry_the_preamble():
    provider = StubProvider([{"action": "answer", "rationale": "two parts",
                              "subquestions": ["What is the product?", "Give me a workout plan"]}])
    res = _orch(provider, max_steps=1).run("What is it, and give me a workout plan",
                                           rep_preamble=PREAMBLE)
    assert res.kind == "answer"
    # two sub-answers + one merge, all of them user-facing
    assert sum(1 for x in provider.answer_systems if x and REPLY_VOICE_SYSTEM in x) >= 3
    assert _calls_without_preamble(provider) == []


def test_read_budget_wrapup_carries_the_preamble():
    read = {"action": "read", "reads": [{"rel_path": "a.md"}], "rationale": "look",
            "user_intent": "ask"}
    provider = StubProvider([read])
    res = _orch(provider, max_steps=1).run("why is alpha like that?", rep_preamble=PREAMBLE)
    assert res.exit_reason == "read_budget"
    assert any(x and REPLY_VOICE_SYSTEM in x for x in provider.answer_systems)
    assert _calls_without_preamble(provider) == []
