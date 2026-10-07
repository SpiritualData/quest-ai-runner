"""``OrchestratorConfig.planner_model``: the routing decision's own model.

Pinned here:

  * unset, the decide call resolves ``planner_tier`` exactly as before;
  * set, the decide call is sent that model id verbatim, and nothing else changes: the reach judge
    keeps its own tier, and a sibling call that shares ``planner_tier`` (request understanding,
    summaries) still resolves the tier;
  * ``QAR_PLANNER_MODEL`` reaches the config through the CLI's env loader.

Fully offline: the provider is a fake that records calls and returns scripted values.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval


class RecordingProvider:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def plan(self, prompt: str, *, model: str, tool_schema: Dict[str, Any],
             tier: Optional[str] = None) -> Any:
        self.calls.append({"model": model, "tool": tool_schema.get("name"), "tier": tier})
        if tool_schema.get("name") == "reach":
            return {"reach": "inside"}
        return {"action": "answer", "rationale": "ok"}

    def answer(self, messages, *, model, system=None) -> str:
        return "ANSWER"

    def list_models(self) -> List[str]:
        return ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"]


def build(provider: Any, **cfg: Any) -> Orchestrator:
    return Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), config=OrchestratorConfig(**cfg))


def decide_calls(provider: RecordingProvider) -> List[Dict[str, Any]]:
    return [c for c in provider.calls if c["tool"] == "decide"]


def test_unset_planner_model_resolves_the_planner_tier_as_before():
    provider = RecordingProvider()
    orch = build(provider)
    orch._plan("what is on my list", "", "", [])
    expected = ModelRegistry(provider).resolve_tier(OrchestratorConfig().planner_tier)
    assert decide_calls(provider)[-1]["model"] == expected


def test_planner_model_is_sent_verbatim_on_the_decide_call():
    provider = RecordingProvider()
    orch = build(provider, planner_model="  some-cheap-model-1  ")
    orch._plan("what is on my list", "", "", [])
    assert decide_calls(provider)[-1]["model"] == "some-cheap-model-1"


def test_planner_model_leaves_the_reach_judge_on_its_own_tier():
    provider = RecordingProvider()
    orch = build(provider, planner_model="some-cheap-model-1", planner_reach_judge=True,
                 planner_reach_judge_tier="best", read_reach_summary="Can read local notes.")
    orch._plan("what is on my list", "", "", [])
    judge = [c for c in provider.calls if c["tool"] == "reach"]
    assert judge and judge[0]["model"] != "some-cheap-model-1"
    assert judge[0]["tier"] == "best"
    assert decide_calls(provider)[-1]["model"] == "some-cheap-model-1"


def test_planner_model_does_not_move_the_shared_planner_tier():
    orch = build(RecordingProvider(), planner_model="some-cheap-model-1")
    # Siblings resolve their model from the tier, never from the pin.
    assert orch.registry.resolve_tier(orch.cfg.planner_tier) != "some-cheap-model-1"


def test_env_reaches_the_config(monkeypatch):
    from quest_ai_runner import cli
    monkeypatch.setenv("QAR_PLANNER_MODEL", "some-cheap-model-1")
    monkeypatch.setenv("QUEST_API_KEY", "test-key")
    monkeypatch.setenv("QUEST_TEAM_ID", "team-test")
    cfg = cli._config_from_env()
    assert cfg.orchestrator.planner_model == "some-cheap-model-1"


def test_reach_summary_from_env_or_file(monkeypatch, tmp_path):
    from quest_ai_runner import cli
    monkeypatch.setenv("QUEST_API_KEY", "test-key")
    monkeypatch.setenv("QUEST_TEAM_ID", "team-test")
    monkeypatch.setenv("QAR_READ_REACH_SUMMARY", "Reads reach the notes folder.")
    assert cli._config_from_env().orchestrator.read_reach_summary == "Reads reach the notes folder."
    f = tmp_path / "reach.md"
    f.write_text("Reads reach the corpus and the task list.\n")
    monkeypatch.setenv("QAR_READ_REACH_SUMMARY_FILE", str(f))
    assert (cli._config_from_env().orchestrator.read_reach_summary
            == "Reads reach the corpus and the task list.")
    monkeypatch.setenv("QAR_READ_REACH_SUMMARY_FILE", str(tmp_path / "missing.md"))
    # an unreadable file keeps the inline summary rather than silently disabling the judge
    assert cli._config_from_env().orchestrator.read_reach_summary == "Reads reach the notes folder."
    monkeypatch.delenv("QAR_READ_REACH_SUMMARY")
    assert cli._config_from_env().orchestrator.read_reach_summary == ""


class EmptyThenDecides(RecordingProvider):
    """A pinned model that answers nothing; the tier answers."""

    def plan(self, prompt, *, model, tool_schema, tier=None):
        if tool_schema.get("name") == "decide" and model == "some-broken-model":
            self.calls.append({"model": model, "tool": "decide", "tier": tier})
            return {}
        return super().plan(prompt, model=model, tool_schema=tool_schema, tier=tier)


def test_a_pinned_model_that_answers_nothing_falls_back_to_the_tier_once():
    provider = EmptyThenDecides()
    orch = build(provider, planner_model="some-broken-model")
    decision = orch._plan("what is on my list", "", "", [])
    models = [c["model"] for c in decide_calls(provider)]
    assert models[0] == "some-broken-model"
    assert models[1] == ModelRegistry(provider).resolve_tier(OrchestratorConfig().planner_tier)
    assert len(models) == 2
    assert decision.action == "answer"
