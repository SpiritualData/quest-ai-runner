"""Tests for click-anywhere-to-type focus behavior in the Textual terminal UI.

The message input should keep keyboard focus so you can just start typing after
clicking anywhere in the terminal. Two mechanisms cooperate:
1. The transcript is non-focusable, so clicking it never steals focus.
2. An app-level on_click sends focus back to the prompt for any other click
   (e.g. the scrollable side panels, which remain focusable).

These are exercised offline, without a running event loop, mirroring the
fake-session pattern in test_future_context_ui.py.
"""

from __future__ import annotations

from quest_ai_runner.textual_ui import QuestAITerminal, TranscriptLog, PromptTextArea


class _FakeSession:
    _rep_name = "Tester"
    _console = None


class _FakePrompt:
    def __init__(self) -> None:
        self.focus_calls = 0

    def focus(self) -> None:
        self.focus_calls += 1


def test_transcript_is_not_focusable():
    """The transcript must not take keyboard focus (else clicks steal it from the input)."""
    assert TranscriptLog.can_focus is False


def _make_app(focused, prompt) -> QuestAITerminal:
    app = QuestAITerminal(_FakeSession())
    # query_one is a normal method — an instance attribute shadows it.
    app.query_one = lambda *a, **k: prompt  # type: ignore[assignment]
    return app


def test_click_focuses_prompt_when_focus_is_elsewhere(monkeypatch):
    prompt = _FakePrompt()
    app = _make_app(focused=object(), prompt=prompt)
    # `focused` is a read-only property; patch it on the class.
    monkeypatch.setattr(type(app), "focused", property(lambda self: object()))
    app.on_click(None)
    assert prompt.focus_calls == 1


def test_click_is_noop_when_prompt_already_focused(monkeypatch):
    prompt = _FakePrompt()
    app = _make_app(focused=prompt, prompt=prompt)
    monkeypatch.setattr(type(app), "focused", property(lambda self: prompt))
    app.on_click(None)
    assert prompt.focus_calls == 0
