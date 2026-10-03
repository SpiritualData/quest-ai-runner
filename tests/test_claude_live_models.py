"""The CLI alias lags a release, so CLI lanes pass the newest live id, falling back to the alias."""
import pytest

from quest_ai_runner.adapters import claude_live_models as live
from quest_ai_runner.adapters.claude_cli_provider import cli_model

IDS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1", "claude-opus-5", "claude-sonnet-5",
       "claude-opus-4-8", "claude-haiku-4-5-20251001"]


@pytest.fixture
def live_on(monkeypatch):
    monkeypatch.setenv("QAR_CLAUDE_LIVE_MODELS", "1")
    live.cache.update(ids=[], at=0.0, ttl=0.0)
    yield
    live.cache.update(ids=[], at=0.0, ttl=0.0)


def test_family_resolves_to_newest_live_id(live_on, monkeypatch):
    monkeypatch.setattr(live, "fetch_ids", lambda: IDS)
    assert cli_model("opus") == "claude-opus-5-5"
    assert cli_model("claude-sonnet") == "claude-sonnet-5-5"
    assert cli_model("claude-opus-4-8") == "claude-opus-5-5"
    assert cli_model("haiku") == "claude-haiku-4-5-20251001"


def test_falls_back_to_alias_when_lookup_fails(live_on, monkeypatch):
    monkeypatch.setattr(live, "fetch_ids", lambda: [])
    assert cli_model("opus") == "opus"


def test_lookup_is_cached(live_on, monkeypatch):
    calls = []
    monkeypatch.setattr(live, "fetch_ids", lambda: calls.append(1) or IDS)
    cli_model("opus"); cli_model("sonnet")
    assert len(calls) == 1
