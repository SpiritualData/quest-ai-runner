"""Tests for Ctrl+C copy-or-quit in the Textual terminal UI.

The terminal runs with mouse reporting on (so the wheel scrolls and plain
click-drag produces a Textual in-app selection). Ctrl+C is wired to
``action_copy_or_quit``: copy the current selection if there is one, otherwise
quit. These tests exercise that branch logic offline, without a running event
loop, mirroring the fake-session pattern in test_future_context_ui.py.
"""

from __future__ import annotations

import pytest

from quest_ai_runner.textual_ui import QuestAITerminal


class _FakeSession:
    _rep_name = "Tester"
    _console = None


class _RecordingLog:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        self.lines.append(getattr(x, "plain", str(x)))


class _FakeScreen:
    def __init__(self, selection: str | None) -> None:
        self._selection = selection

    def get_selected_text(self) -> str | None:
        return self._selection


def _make_app(selection: str | None, monkeypatch) -> tuple[QuestAITerminal, _RecordingLog, dict]:
    app = QuestAITerminal(_FakeSession())
    log = _RecordingLog()
    app._tlog = log
    calls: dict = {"copied": None, "cleared": False, "exited": False}

    # `screen` is a read-only property (a data descriptor), so it must be patched
    # on the class, not the instance.
    monkeypatch.setattr(type(app), "screen", property(lambda self: _FakeScreen(selection)))
    app.copy_to_clipboard = lambda text: calls.__setitem__("copied", text)  # type: ignore[assignment]
    app.clear_selection = lambda: calls.__setitem__("cleared", True)  # type: ignore[assignment]
    app.exit = lambda *a, **k: calls.__setitem__("exited", True)  # type: ignore[assignment]
    return app, log, calls


def test_copy_when_text_is_selected(monkeypatch):
    """With a selection, Ctrl+C copies it, clears the highlight, and does NOT quit."""
    app, log, calls = _make_app("hello world", monkeypatch)
    app.action_copy_or_quit()
    assert calls["copied"] == "hello world"
    assert calls["cleared"] is True
    assert calls["exited"] is False
    assert any("Copied" in ln for ln in log.lines)


def test_quit_when_nothing_selected(monkeypatch):
    """With no selection, Ctrl+C quits and copies nothing."""
    app, log, calls = _make_app(None, monkeypatch)
    app.action_copy_or_quit()
    assert calls["exited"] is True
    assert calls["copied"] is None


def test_blank_selection_is_treated_as_no_selection(monkeypatch):
    """A whitespace-only selection should quit, not copy an empty string."""
    app, log, calls = _make_app("   \n  ", monkeypatch)
    app.action_copy_or_quit()
    assert calls["exited"] is True
    assert calls["copied"] is None


def test_copy_preview_is_collapsed_and_truncated(monkeypatch):
    """The transcript preview collapses whitespace and truncates long selections."""
    long_sel = "line one\nline two " + "x" * 200
    app, log, calls = _make_app(long_sel, monkeypatch)
    app.action_copy_or_quit()
    # The full selection is copied verbatim…
    assert calls["copied"] == long_sel
    # …but the preview line is a single, truncated line.
    preview_line = next(ln for ln in log.lines if "Copied" in ln)
    assert "\n" not in preview_line
    assert "…" in preview_line
