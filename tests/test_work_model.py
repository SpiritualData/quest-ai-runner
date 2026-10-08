"""Work-model configuration applied to quest AI runs (resolution order and the exclusion rule).

The backend resolves the effective view (team override, else org, else the Claude default) and QAR
applies it. These tests pin:
  * the pure choice: a requested model is kept only when it is allowed; an excluded one falls back to
    the configured default with a note; an unconfigured run stays unpinned;
  * the source: a configured view (team or org) pins its default, the built-in default does not;
  * the client: the GET path for a team's environment and the heartbeat's available models;
  * the executor: a literal pin is restricted, a read failure restricts nothing, and an environment
    with no Claude model refuses the run.
"""

import pytest

from quest_ai_runner.core.work_model import (
    RUNNABLE_MODELS,
    WorkModelUnavailable,
    anthropic_options,
    apply_work_model,
    default_effective,
    is_tier_word,
)
from quest_ai_runner.runner.quest_client import QuestClient
from tests.test_runner import MockQuestClient, _brain, _ModelCapturingProvider


def _claude(model, tier):
    return {"provider": "anthropic", "model": model, "tier": tier}


def _view(allowed, default="sonnet", excluded=()):
    """An effective view shaped like the backend's ``effective`` block."""
    return {
        "default_model": _claude(default, "standard"),
        "allowed_models": allowed,
        "excluded_tiers": list(excluded),
        "reported_models": None,
    }


# --- the pure choice -------------------------------------------------------

def test_unconfigured_run_with_no_request_stays_unpinned():
    choice = apply_work_model(None, default_effective(), configured=False)
    assert choice.model is None
    assert choice.source == "unpinned"


def test_configured_default_is_pinned_when_nothing_requested():
    choice = apply_work_model(None, _view([_claude("haiku", "economy"), _claude("sonnet", "standard")],
                                          default="haiku"), configured=True)
    assert choice.model == "haiku"
    assert choice.source == "default"


def test_allowed_request_is_used_as_asked():
    view = default_effective()
    choice = apply_work_model("opus", view, configured=True)
    assert choice.model == "opus"
    assert choice.source == "requested"
    assert choice.note is None


def test_excluded_premium_request_falls_back_to_default_with_note():
    # Premium excluded: opus and fable are not in the effective allowed list, so the request is refused.
    view = _view([_claude("haiku", "economy"), _claude("sonnet", "standard")], excluded=["premium"])
    choice = apply_work_model("opus", view, configured=True)
    assert choice.model == "sonnet"
    assert choice.source == "default"
    assert "'opus' is not allowed" in choice.note


def test_request_outside_the_allowed_list_falls_back_even_when_not_excluded_by_tier():
    view = _view([_claude("haiku", "economy")], default="haiku")
    choice = apply_work_model("sonnet", view, configured=True)
    assert choice.model == "haiku"
    assert choice.note is not None


def test_default_removed_by_runner_report_falls_back_to_first_available_model():
    view = _view([_claude("haiku", "economy"), _claude("opus", "premium")], default="sonnet")
    choice = apply_work_model(None, view, configured=True)
    assert choice.model == "haiku"


def test_only_non_anthropic_models_refuses_the_run():
    view = _view([{"provider": "ollama", "model": "llama-3", "tier": "standard"}], default="llama-3")
    view["default_model"] = {"provider": "ollama", "model": "llama-3", "tier": "standard"}
    with pytest.raises(WorkModelUnavailable):
        apply_work_model("sonnet", view, configured=True)


def test_non_anthropic_entries_are_skipped_not_run():
    view = _view([{"provider": "ollama", "model": "sonnet", "tier": "standard"}, _claude("haiku", "economy")],
                 default="haiku")
    assert anthropic_options(view) == ["haiku"]


def test_tier_words_are_not_work_models():
    for word in ("fast", "balanced", "quality", "best", "science"):
        assert is_tier_word(word)
    assert not is_tier_word("opus")
    assert not is_tier_word(None)


def test_runnable_models_are_the_claude_family():
    assert RUNNABLE_MODELS == ("haiku", "sonnet", "opus", "fable")


# --- resolution order: which configured view counts as "configured" ---------

@pytest.mark.parametrize("source", ["team", "org"])
def test_team_or_org_configuration_counts_as_configured(source, monkeypatch):
    # The backend resolves team override then org; a result from either is an explicit configuration.
    from quest_ai_runner.runner import executor as ex_mod

    class _Client(MockQuestClient):
        def get_work_model_config(self, **kw):
            return {"source": source, "config": None, "effective": _view(
                [_claude("haiku", "economy")], default="haiku")}

    provider = _ModelCapturingProvider(decisions=[{"action": "answer", "model_tier": "haiku", "rationale": "ok"}])
    ex = ex_mod.TaskExecutor(_Client([]), _brain(provider))
    assert ex._work_model_hint({"id": "t1"}, None) == "haiku"


def test_built_in_default_source_pins_nothing(monkeypatch):
    from quest_ai_runner.runner import executor as ex_mod

    class _Client(MockQuestClient):
        def get_work_model_config(self, **kw):
            return {"source": "default", "config": {}, "effective": default_effective()}

    provider = _ModelCapturingProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    ex = ex_mod.TaskExecutor(_Client([]), _brain(provider))
    assert ex._work_model_hint({"id": "t2"}, None) is None


# --- client -----------------------------------------------------------------

def test_work_model_config_gets_the_team_environment_path():
    client = QuestClient("https://quest.example", "k", team_id="team_1", env_id="sd prod")
    seen = {}

    def fake_request(method, path, **kw):
        seen["method"], seen["path"] = method, path
        return {"effective": {}}

    client._request = fake_request  # type: ignore[assignment]
    client.get_work_model_config()
    assert seen == {"method": "GET", "path": "/api/teams/team_1/environments/sd%20prod/work-model"}


def test_work_model_config_defaults_to_the_default_environment():
    client = QuestClient("https://quest.example", "k", team_id="team_1")
    seen = {}
    client._request = lambda method, path, **kw: seen.setdefault("path", path) or {}  # type: ignore[assignment]
    client.get_work_model_config(env_id="")
    assert seen["path"] == "/api/teams/team_1/environments/default/work-model"


def test_heartbeat_reports_the_runnable_claude_models():
    client = QuestClient("https://quest.example", "k", team_id="team_1")
    seen = {}

    def fake_request(method, path, body=None, **kw):
        seen["body"] = body
        return {}

    client._request = fake_request  # type: ignore[assignment]
    client.post_environment_heartbeat({"web": True, "corpus": False, "code": True})
    assert seen["body"]["available_models"] == [
        {"provider": "anthropic", "model": m} for m in RUNNABLE_MODELS
    ]


# --- executor: the exclusion rule applied to a real run ---------------------

class _ConfigClient(MockQuestClient):
    def __init__(self, payload=None, error=None):
        super().__init__([])
        self._payload = payload
        self._error = error

    def get_work_model_config(self, **kw):
        if self._error is not None:
            raise self._error
        return self._payload


def _premium_excluded_payload():
    return {"source": "org", "config": {}, "effective": _view(
        [_claude("haiku", "economy"), _claude("sonnet", "standard")], default="sonnet", excluded=["premium"])}


def test_executor_excluded_opus_pin_runs_on_the_default_instead():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.runner.executor import TaskExecutor
    provider = _ModelCapturingProvider(decisions=[{"action": "answer", "model_tier": "haiku", "rationale": "ok"}])
    ex = TaskExecutor(_ConfigClient(_premium_excluded_payload()), _brain(provider))
    out = ex.execute({"id": "wm1", "text": "say hi", "deep_run_model": "opus"})
    assert out.status == "done"
    registry = ModelRegistry(provider)
    assert provider.answer_models == [registry.resolve_tier("fast"), registry.resolve_tier("sonnet")]


def test_executor_allowed_pin_is_kept():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.runner.executor import TaskExecutor
    provider = _ModelCapturingProvider(decisions=[{"action": "answer", "model_tier": "haiku", "rationale": "ok"}])
    ex = TaskExecutor(_ConfigClient(_premium_excluded_payload()), _brain(provider))
    out = ex.execute({"id": "wm2", "text": "say hi", "deep_run_model": "haiku"})
    assert out.status == "done"
    registry = ModelRegistry(provider)
    assert provider.answer_models == [registry.resolve_tier("fast"), registry.resolve_tier("haiku")]


def test_executor_read_failure_restricts_nothing():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.runner.executor import TaskExecutor
    provider = _ModelCapturingProvider(decisions=[{"action": "answer", "model_tier": "haiku", "rationale": "ok"}])
    ex = TaskExecutor(_ConfigClient(error=RuntimeError("backend down")), _brain(provider))
    out = ex.execute({"id": "wm3", "text": "say hi", "deep_run_model": "opus"})
    assert out.status == "done"
    registry = ModelRegistry(provider)
    assert provider.answer_models == [registry.resolve_tier("fast"), registry.resolve_tier("opus")]


def test_executor_tier_word_leaves_work_model_alone():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.runner.executor import TaskExecutor
    provider = _ModelCapturingProvider(decisions=[{"action": "answer", "model_tier": "haiku", "rationale": "ok"}])
    ex = TaskExecutor(_ConfigClient(_premium_excluded_payload()), _brain(provider))
    out = ex.execute({"id": "wm4", "text": "say hi", "model": "best"})
    assert out.status == "done"
    registry = ModelRegistry(provider)
    assert provider.answer_models[-1] == registry.resolve_tier("best")


def test_executor_refuses_run_when_no_claude_model_is_allowed():
    from quest_ai_runner.runner.executor import TaskExecutor
    provider = _ModelCapturingProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    payload = {"source": "org", "config": {}, "effective": _view(
        [{"provider": "ollama", "model": "llama-3", "tier": "standard"}], default="llama-3")}
    payload["effective"]["default_model"] = {"provider": "ollama", "model": "llama-3", "tier": "standard"}
    client = _ConfigClient(payload)
    ex = TaskExecutor(client, _brain(provider))
    out = ex.execute({"id": "wm5", "text": "say hi", "deep_run_model": "sonnet"})
    assert out.status == "failed"
    assert "no Claude model" in out.result
    assert provider.answer_models == []
