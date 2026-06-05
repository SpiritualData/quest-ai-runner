"""Offline tests for the keyless ClaudeCliProvider.

These never spawn the real ``claude`` binary — ``_invoke`` is monkeypatched — so they run with no
network, no API key, and no CLI installed. They lock down the two things that can silently break:
the lenient JSON extraction (the CLI can't force tool_choice) and the tier->CLI-alias mapping.
"""
from quest_ai_runner.adapters import ClaudeCliProvider
from quest_ai_runner.adapters.claude_cli_provider import cli_model, extract_json_object
from quest_ai_runner.core.orchestrator import DECIDE_TOOL


def test_cli_model_maps_family_ids_to_aliases():
    assert cli_model("claude-opus-4-8") == "opus"
    assert cli_model("claude-sonnet-4-6") == "sonnet"
    assert cli_model("claude-haiku-4-5-20251001") == "haiku"
    # already an alias -> unchanged family alias
    assert cli_model("sonnet") == "sonnet"
    # unknown id passes through; None stays None
    assert cli_model("some-other-model") == "some-other-model"
    assert cli_model(None) is None
    assert cli_model("") is None


def test_extract_json_object_bare():
    assert extract_json_object('{"action": "answer", "rationale": "ok"}') == {
        "action": "answer",
        "rationale": "ok",
    }


def test_extract_json_object_fenced():
    text = '```json\n{"action": "read", "rationale": "look"}\n```'
    assert extract_json_object(text) == {"action": "read", "rationale": "look"}


def test_extract_json_object_embedded_in_prose():
    text = 'Sure, here is my decision:\n{"action": "deep", "rationale": "work"}\nHope that helps!'
    assert extract_json_object(text) == {"action": "deep", "rationale": "work"}


def test_extract_json_object_handles_braces_in_strings():
    text = '{"action": "answer", "rationale": "use {curly} braces"}'
    assert extract_json_object(text) == {"action": "answer", "rationale": "use {curly} braces"}


def test_extract_json_object_empty_on_garbage():
    assert extract_json_object("no json here at all") == {}
    assert extract_json_object("") == {}


def test_plan_parses_invoke_output(monkeypatch):
    p = ClaudeCliProvider()
    captured = {}

    def fake_invoke(prompt, *, model, system=None):
        captured["prompt"] = prompt
        captured["model"] = model
        return '```json\n{"action": "read", "rationale": "grep first", "model_tier": "haiku"}\n```'

    monkeypatch.setattr(p, "_invoke", fake_invoke)
    decision = p.plan("PLAN PROMPT", model="claude-haiku-4-5", tool_schema=DECIDE_TOOL)
    assert decision["action"] == "read"
    assert decision["model_tier"] == "haiku"
    # The strict-output instruction (with the schema) is appended to the planner prompt.
    assert "OUTPUT FORMAT (STRICT)" in captured["prompt"]
    assert "PLAN PROMPT" in captured["prompt"]


def test_plan_degrades_to_empty_dict_on_invoke_failure(monkeypatch):
    p = ClaudeCliProvider()

    def boom(prompt, *, model, system=None):
        raise RuntimeError("cli not installed")

    monkeypatch.setattr(p, "_invoke", boom)
    # plan() must NEVER raise — an empty dict lets normalize_decision pick a safe default.
    assert p.plan("x", model="sonnet", tool_schema=DECIDE_TOOL) == {}


def test_answer_flattens_messages_and_returns_text(monkeypatch):
    p = ClaudeCliProvider()
    captured = {}

    def fake_invoke(prompt, *, model, system=None):
        captured["prompt"] = prompt
        captured["system"] = system
        return "the grounded answer"

    monkeypatch.setattr(p, "_invoke", fake_invoke)
    msgs = [
        {"role": "user", "content": "context block"},
        {"role": "user", "content": "the question"},
    ]
    out = p.answer(msgs, model="claude-sonnet-4-6", system="be terse")
    assert out == "the grounded answer"
    assert "context block" in captured["prompt"]
    assert "the question" in captured["prompt"]
    assert captured["system"] == "be terse"


def test_list_models_empty_so_registry_falls_back():
    assert ClaudeCliProvider().list_models() == []


def test_build_env_strips_billing_and_session_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = ClaudeCliProvider()._build_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDECODE" not in env
