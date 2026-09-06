"""`quest-ai-runner chat` has exactly one UI, and says so when it cannot start.

The ANSI/prompt_toolkit renderer that used to catch a failed Textual import was removed: the
project maintains one chat UI, not two. That makes the import check the only safety net left,
so it has to behave like one — a non-zero exit and a message naming the cause and the fix, never
a silent no-op and never a bare ImportError traceback.
"""
from __future__ import annotations

import logging

from quest_ai_runner import cli
from quest_ai_runner.config import RunnerConfig


def _valid_cfg(config_path=None) -> RunnerConfig:
    return RunnerConfig(
        quest_base_url="http://example.invalid",
        quest_api_key="qsk_test",
        retrieval=object(),
        model_provider=object(),
    )


def test_chat_starts_the_textual_ui(monkeypatch):
    """The happy path: chat goes straight to Textual, with no availability branch to fall off."""
    monkeypatch.setattr(cli, "_config_from_env", _valid_cfg)
    import quest_ai_runner.textual_session as textual_session
    monkeypatch.setattr(textual_session, "is_textual_available", lambda: True)

    started = []
    monkeypatch.setattr(
        textual_session, "start_textual_interactive",
        lambda cfg, **kwargs: started.append(kwargs),
    )

    assert cli.main(["chat"]) == 0
    assert len(started) == 1


def test_chat_without_textual_exits_nonzero_with_actionable_error(monkeypatch, caplog):
    """No fallback UI: an unimportable `textual` must fail loudly and tell the user how to fix it."""
    monkeypatch.setattr(cli, "_config_from_env", _valid_cfg)
    import quest_ai_runner.textual_session as textual_session
    monkeypatch.setattr(textual_session, "is_textual_available", lambda: False)

    def _must_not_run(cfg, **kwargs):
        raise AssertionError("the UI must not start when textual is unavailable")

    monkeypatch.setattr(textual_session, "start_textual_interactive", _must_not_run)

    with caplog.at_level(logging.ERROR):
        rc = cli.main(["chat"])

    assert rc == 1
    text = caplog.text
    assert "textual" in text.lower()
    assert "pip install" in text
