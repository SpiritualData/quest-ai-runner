"""Vendor knowledge in core is data (patterns and tables), not branches on a vendor name."""
import pytest

from quest_ai_runner.core import model_registry
from quest_ai_runner.core.goal_runner_contract import _is_claude_model


@pytest.mark.parametrize("model,family", [
    ("claude-haiku-4-5", "claude-haiku"), ("claude-opus-4-1", "claude-opus"), ("claude-sonnet-4-6", "claude-sonnet"),
    ("claude-x", "claude-other"), ("gemini-1.5-pro", "gemini-1.5"), ("gemini-3.1-flash-lite", "gemini-3"),
    ("gemini-2.5-flash", "gemini-2.0"), ("gpt-4o-mini", "gpt-4o"), ("o3", "o-series"), ("llama-3", "other")])
def test_family_rules_name_the_family(model, family):
    low = model.lower()
    assert next((n for n, rx in model_registry.FAMILY_RULES if rx.search(low)), "other") == family


def test_deepseek_is_never_bucketed_into_a_tier():
    assert model_registry.EXCLUDED_FROM_TIERS.search("deepseek-chat")
    assert not model_registry.EXCLUDED_FROM_TIERS.search("claude-deepseek-x")


@pytest.mark.parametrize("model,ok", [("claude-sonnet-4-6", True), ("us.anthropic.claude-x", True), ("opus", True),
                                      ("fable", True), ("gemini-3.5-flash", False), ("gpt-4o", False), ("", False)])
def test_claude_code_model_pattern(model, ok):
    assert _is_claude_model(model) is ok
