"""Tests for the zero-config first-run hang fix.

With no provider env vars set, quest-ai-runner chat falls back to ClaudeCliProvider,
which needs the `claude` CLI on PATH. Before this fix, if the CLI was missing the
Textual session build would hang forever on a blank alternate screen. Now
`_missing_model_provider_message()` detects the case up front so `_build_session_worker`
can fail fast with an actionable message via the existing `_startup_failed` path.

These tests exercise the pure detection helper directly (env/which injectable, no
Textual event loop needed) and confirm `_build_session_worker` routes through
`_startup_failed` instead of hanging when the helper reports a problem.
"""

from __future__ import annotations

from quest_ai_runner.textual_ui import QuestAITerminal, _missing_model_provider_message


# ---------------------------------------------------------------------------
# _missing_model_provider_message — pure helper, no Textual dependency
# ---------------------------------------------------------------------------

def test_no_key_and_no_claude_cli_reports_problem():
    """Zero-config: no API keys, and 'claude' is not on PATH -> a clear message."""
    env = {}
    msg = _missing_model_provider_message(env=env, which=lambda name: None)
    assert msg is not None
    assert "ANTHROPIC_API_KEY" in msg
    assert "claude" in msg.lower()


def test_no_key_but_claude_cli_present_is_fine():
    """Zero-config but the claude CLI IS installed: no problem (keyless subscription path)."""
    env = {}
    msg = _missing_model_provider_message(env=env, which=lambda name: "/usr/bin/claude")
    assert msg is None


def test_anthropic_key_set_is_fine():
    env = {"ANTHROPIC_API_KEY": "sk-ant-fake"}
    msg = _missing_model_provider_message(env=env, which=lambda name: None)
    assert msg is None


def test_openai_key_set_is_fine():
    env = {"OPENAI_API_KEY": "sk-fake"}
    msg = _missing_model_provider_message(env=env, which=lambda name: None)
    assert msg is None


def test_google_key_set_is_fine():
    env = {"GOOGLE_API_KEY": "fake"}
    msg = _missing_model_provider_message(env=env, which=lambda name: None)
    assert msg is None


def test_explicit_non_cli_backend_skips_the_check():
    """An explicit non-claude_cli backend is the user's choice; let it fail its own way."""
    env = {"QAR_MODEL_BACKEND": "anthropic"}
    msg = _missing_model_provider_message(env=env, which=lambda name: None)
    assert msg is None


def test_explicit_claude_cli_backend_still_checked():
    env = {"QAR_MODEL_BACKEND": "claude_cli"}
    msg = _missing_model_provider_message(env=env, which=lambda name: None)
    assert msg is not None


def test_message_has_no_em_dash():
    """Brand rule: no em dashes in user-facing copy."""
    msg = _missing_model_provider_message(env={}, which=lambda name: None)
    assert msg is not None
    assert "—" not in msg


# ---------------------------------------------------------------------------
# _build_session_worker fails fast via _startup_failed instead of hanging
# ---------------------------------------------------------------------------

class _FakeSession:
    _rep_name = "Tester"
    _console = None


def test_build_session_worker_fails_fast_when_no_provider(monkeypatch):
    """With no keys and no claude CLI, _build_session_worker must route to _startup_failed
    (never attempt to build the InteractiveSession, which is what would hang)."""
    app = QuestAITerminal(
        None,
        _config=object(),
        _rep_name="Assistant",
        _persona=None,
        _goal_id=None,
    )
    calls: dict = {"startup_failed_exc": None, "finish_startup_called": False}
    app.call_from_thread = lambda fn, *a, **k: fn(*a, **k)  # run inline, no event loop
    app._startup_failed = lambda exc: calls.__setitem__("startup_failed_exc", exc)
    app._finish_startup = lambda session: calls.__setitem__("finish_startup_called", True)

    monkeypatch.setattr(
        "quest_ai_runner.textual_ui._missing_model_provider_message",
        lambda: "No AI provider is configured. Set ANTHROPIC_API_KEY...",
    )

    app._build_session_worker()

    assert calls["finish_startup_called"] is False
    assert calls["startup_failed_exc"] is not None
    assert "ANTHROPIC_API_KEY" in str(calls["startup_failed_exc"])


def test_build_session_worker_proceeds_when_provider_available(monkeypatch):
    """When a provider IS available, the worker proceeds to build the session as before
    (does not short-circuit through _startup_failed)."""
    app = QuestAITerminal(
        None,
        _config=object(),
        _rep_name="Assistant",
        _persona=None,
        _goal_id=None,
    )
    calls: dict = {"startup_failed_called": False, "finish_startup_called": False}
    app.call_from_thread = lambda fn, *a, **k: fn(*a, **k)
    app._startup_failed = lambda exc: calls.__setitem__("startup_failed_called", True)
    app._finish_startup = lambda session: calls.__setitem__("finish_startup_called", True)

    monkeypatch.setattr(
        "quest_ai_runner.textual_ui._missing_model_provider_message",
        lambda: None,
    )
    # Stub out InteractiveSession construction so this stays offline/fast.
    monkeypatch.setattr(
        "quest_ai_runner.interactive.InteractiveSession",
        lambda *a, **k: _FakeSession(),
    )

    app._build_session_worker()

    assert calls["startup_failed_called"] is False
    assert calls["finish_startup_called"] is True
