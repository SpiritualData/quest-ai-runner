"""Offline tests for the keyless ClaudeCliProvider.

These never spawn the real ``claude`` binary — ``_invoke`` is monkeypatched — so they run with no
network, no API key, and no CLI installed. They lock down the two things that can silently break:
the lenient JSON extraction (the CLI can't force tool_choice) and the tier->CLI-alias mapping.
"""
from quest_ai_runner.adapters import ClaudeCliProvider
from quest_ai_runner.adapters.claude_cli_provider import cli_model, extract_json_object
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import DECIDE_TOOL


def test_cli_model_maps_family_ids_to_aliases():
    assert cli_model("claude-opus-4-8") == "opus"
    assert cli_model("claude-sonnet-4-6") == "sonnet"
    assert cli_model("claude-haiku-4-5-20251001") == "haiku"
    # already an alias -> unchanged family alias
    assert cli_model("sonnet") == "sonnet"
    # fully-qualified Claude ids without a family word pass through
    assert cli_model("us.anthropic.claude-x-1") == "us.anthropic.claude-x-1"
    assert cli_model(None) is None
    assert cli_model("") is None


def test_cli_model_drops_non_claude_ids():
    # The CLI only runs Claude models. A tier map built for a Gemini/OpenAI deployment must not
    # leak a foreign id into --model (the CLI exits 1 having done nothing); fall back to the
    # CLI's default model instead — the same gate the deep runner applies (_is_claude_model).
    assert cli_model("gemini-3.1-flash-lite") is None
    assert cli_model("gemini-3.5-flash") is None
    assert cli_model("gpt-4o") is None
    assert cli_model("some-other-model") is None


def test_invoke_error_surfaces_stdout_envelope(monkeypatch):
    # In --output-format json mode the CLI reports errors in the stdout envelope with an EMPTY
    # stderr; the raised error must carry the envelope's result text, not "no stderr".
    import json
    import subprocess

    provider = ClaudeCliProvider()
    envelope = {"is_error": True, "result": "API Error: 404 model not found"}

    def fake_run(cmd, **kwargs):
        class P:
            returncode = 1
            stdout = json.dumps(envelope).encode()
            stderr = b""
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    try:
        provider._invoke("hi", model="haiku")
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "API Error: 404 model not found" in str(e)


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


def test_list_models_advertises_only_claude_families():
    """The CLI can run Claude and nothing else, so it must advertise Claude families.

    Regression guard: this used to return [], which made ModelRegistry fall through to its
    Gemini-flavoured DEFAULT_FALLBACK_TOP on a claude_cli-only deployment.
    """
    models = ClaudeCliProvider().list_models()
    assert models, "an empty list sends the registry to its Gemini defaults"
    assert all("claude" in m.lower() for m in models)
    assert {"haiku", "sonnet", "opus"} <= {cli_model(m) for m in models}


def test_every_tier_resolves_to_a_cli_runnable_model():
    """End to end of the bug: no tier may resolve to a model the CLI cannot run.

    A tier resolving to e.g. ``gemini-3.1-flash-lite`` is what killed every task on the SD dev
    lane at its first planner call, because no Gemini provider was registered to run it.
    """
    registry = ModelRegistry(ClaudeCliProvider())
    for tier in ("fast", "balanced", "quality", "best"):
        resolved = registry.resolve_tier(tier)
        assert "claude" in resolved.lower(), f"tier {tier} resolved to non-Claude {resolved!r}"
        assert cli_model(resolved) is not None, f"tier {tier} gave the CLI an unusable {resolved!r}"


def test_explicit_tier_override_still_wins():
    """Operators pinning QAR_MODEL_* keep full precedence over the advertised families."""
    registry = ModelRegistry(ClaudeCliProvider(), fallback={"balanced": "claude-haiku-4-5"})
    assert registry.resolve_tier("balanced") == "claude-haiku-4-5"


def test_build_env_strips_billing_and_session_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("CLAUDECODE", "1")
    env = ClaudeCliProvider()._build_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDECODE" not in env
