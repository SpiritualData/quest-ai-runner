"""Tests for Ctrl+C in the Textual terminal UI: copy a selection, else stop the work, else ask to quit.

The terminal runs with mouse reporting on (so the wheel scrolls and plain
click-drag produces a Textual in-app selection). Ctrl+C is wired to
``action_copy_or_quit``: copy the current selection if there is one. With nothing
selected one press never ends the app: while the AI works it stops the work (the
same as Esc), when idle it says to press again, and a second press within a few
seconds quits. These tests exercise that branch logic offline, without a running
event loop, mirroring the fake-session pattern in test_future_context_ui.py.
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


def arm_clock(app, monkeypatch, start: float = 1000.0) -> dict:
    """Move the clock the quit window reads, so the tests do not wait."""
    from quest_ai_runner import textual_ui_actions
    clock = {"now": start}
    monkeypatch.setattr(textual_ui_actions.time, "monotonic", lambda: clock["now"])
    return clock


def watch_cancel(app) -> dict:
    seen: dict = {"cancelled": 0}
    app.action_cancel = lambda: seen.__setitem__("cancelled", seen["cancelled"] + 1)  # type: ignore[assignment]
    return seen


def test_one_press_with_nothing_selected_never_quits_when_idle(monkeypatch):
    """The first Ctrl+C only says how to quit: a stray press must not end the app."""
    app, log, calls = _make_app(None, monkeypatch)
    arm_clock(app, monkeypatch)
    cancel = watch_cancel(app)
    app.action_copy_or_quit()
    assert calls["exited"] is False
    assert calls["copied"] is None
    assert cancel["cancelled"] == 0
    assert log.lines == ["  Press Ctrl+C again to quit."]


def test_a_second_press_within_the_window_quits(monkeypatch):
    app, log, calls = _make_app(None, monkeypatch)
    clock = arm_clock(app, monkeypatch)
    app.action_copy_or_quit()
    clock["now"] += 2.9
    app.action_copy_or_quit()
    assert calls["exited"] is True


def test_a_second_press_after_the_window_only_asks_again(monkeypatch):
    app, log, calls = _make_app(None, monkeypatch)
    clock = arm_clock(app, monkeypatch)
    app.action_copy_or_quit()
    clock["now"] += 3.1
    app.action_copy_or_quit()
    assert calls["exited"] is False
    assert log.lines == ["  Press Ctrl+C again to quit."] * 2


def test_while_the_ai_works_ctrl_c_stops_the_work_like_esc_and_does_not_quit(monkeypatch):
    app, log, calls = _make_app(None, monkeypatch)
    arm_clock(app, monkeypatch)
    cancel = watch_cancel(app)
    app._turn_active = True
    app.action_copy_or_quit()
    assert cancel["cancelled"] == 1, "the same action Esc runs"
    assert calls["exited"] is False
    assert log.lines == ["  Stopped. Press Ctrl+C again to quit."]


def test_a_second_press_after_stopping_the_work_quits(monkeypatch):
    app, log, calls = _make_app(None, monkeypatch)
    clock = arm_clock(app, monkeypatch)
    cancel = watch_cancel(app)
    app._turn_active = True
    app.action_copy_or_quit()
    clock["now"] += 1.0
    app.action_copy_or_quit()
    assert calls["exited"] is True
    assert cancel["cancelled"] == 1, "the second press quits, it does not stop a second time"


def test_blank_selection_is_treated_as_no_selection(monkeypatch):
    """A whitespace-only selection copies nothing: it is the no-selection case (one press asks, never quits)."""
    app, log, calls = _make_app("   \n  ", monkeypatch)
    arm_clock(app, monkeypatch)
    app.action_copy_or_quit()
    assert calls["exited"] is False
    assert calls["copied"] is None
    assert log.lines == ["  Press Ctrl+C again to quit."]


def test_the_key_line_names_quit_only_when_idle_and_esc_only_when_busy(monkeypatch):
    app, log, calls = _make_app(None, monkeypatch)
    app._pending_select = None
    app._available_deep_runs = lambda: {}   # type: ignore[assignment]
    from types import SimpleNamespace
    app._future_ctx_panel = SimpleNamespace(has_bullets=False)
    idle = app.key_hints()
    assert ("ctrl+c", "Quit") in idle and not any(key == "esc" for key, _ in idle)
    assert not any("Copy" in label for key, label in idle), "copying is Alt+Y or a mouse selection"
    app._turn_active = True
    busy = app.key_hints()
    assert ("esc", "Cancel") in busy and not any(key == "ctrl+c" for key, _ in busy)


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
