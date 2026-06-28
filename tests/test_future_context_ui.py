"""Tests for the future-context expandable panel in the Textual terminal UI.

Covers:
1. _build_future_context_text() — pure helper, builds Rich Text from bullet lines.
   No Textual event loop needed; testable offline.
2. FutureContextPanel state management (load/hide) without calling _rerender().
3. Event parsing: a deep result event with future_context is captured into
   _future_context on the app instance.
4. Edge cases: empty/whitespace future_context, non-deep result events.
"""

from __future__ import annotations

import pytest

from quest_ai_runner.interactive import _DeepRunTracker
from quest_ai_runner.textual_ui import FutureContextPanel, QuestAITerminal, _build_future_context_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeSession:
    """Minimal stand-in so QuestAITerminal can be constructed without a brain."""
    _rep_name = "Tester"
    _console = None


class _RecordingLog:
    """Captures everything written to the transcript as plain text."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        self.lines.append(getattr(x, "plain", str(x)))


class _FakePanel:
    """Stand-in for FutureContextPanel that tracks calls without Textual."""
    display = False
    _bullets = ""

    def hide(self) -> None:
        self._bullets = ""
        self.display = False

    def load(self, bullets: str) -> None:
        self._bullets = bullets.strip()

    def _rerender(self) -> None:
        pass


def _make_app() -> tuple[QuestAITerminal, _RecordingLog]:
    """Build a minimal QuestAITerminal wired with a recording log and fake panels."""
    app = QuestAITerminal(_FakeSession())
    app._deep = _DeepRunTracker()
    app._deep_flushed = set()
    log = _RecordingLog()
    app._tlog = log
    # Swap in a fake FutureContextPanel so we can assert on it without Textual.
    app._future_ctx_panel = _FakePanel()
    app._future_context = ""
    # _ev is resolved lazily; leave it None so _types() initialises it.
    app._ev = None
    return app, log


# ---------------------------------------------------------------------------
# _build_future_context_text — pure helper (no event loop needed)
# ---------------------------------------------------------------------------

def test_build_text_includes_header_and_bullets():
    t = _build_future_context_text(
        "- collection: Pricing tiers (id: col-123)\n- user preference: metric units"
    )
    assert t is not None
    plain = t.plain
    assert "Context it used" in plain
    assert "col-123" in plain
    assert "metric units" in plain


def test_build_text_empty_returns_none():
    assert _build_future_context_text("") is None
    assert _build_future_context_text("   ") is None
    assert _build_future_context_text("\n\n") is None


def test_toggle_is_bound_to_alt_c_not_a_bare_letter():
    # The prompt Input consumes printable keys, so a bare 'c'/'f' would be typed into the message
    # instead of toggling. The toggle must use a modified key (alt+c) to fire reliably while typing.
    keys = {b.key: b.action for b in QuestAITerminal.BINDINGS
            if hasattr(b, "action") and b.action == "toggle_future_context"}
    assert "alt+c" in keys
    # No bare single-letter binding maps to the toggle.
    assert not any(len(k) == 1 for k in keys)


def test_deep_detail_toggle_is_alt_d():
    keys = {b.key for b in QuestAITerminal.BINDINGS
            if getattr(b, "action", None) == "toggle_deep_detail"}
    assert "alt+d" in keys
    assert not any(len(k) == 1 for k in keys)


def test_no_binding_uses_a_bare_printable_letter():
    # A bare printable-letter binding never fires while the prompt Input is focused (it gets typed),
    # so every action key must be a modified chord or a non-printable key. Guards against regressing
    # any toggle back to a bare letter.
    bare = [b.key for b in QuestAITerminal.BINDINGS
            if isinstance(getattr(b, "key", None), str)
            and len(b.key) == 1 and b.key.isprintable() and b.key.isalnum()]
    assert bare == [], f"bare printable-letter bindings will be swallowed by the input: {bare}"


def test_build_text_strips_blank_lines():
    """Blank lines within bullets are filtered; non-blank lines still appear."""
    t = _build_future_context_text("- first\n\n- second")
    assert t is not None
    plain = t.plain
    assert "first" in plain
    assert "second" in plain


def test_build_text_no_em_dash():
    """Brand rule: no em dashes in user-facing copy."""
    t = _build_future_context_text("- some bullet")
    assert t is not None
    assert "—" not in t.plain  # em dash


# ---------------------------------------------------------------------------
# FutureContextPanel state (load / hide) — no _rerender call
# ---------------------------------------------------------------------------

def test_panel_load_stores_bullets():
    panel = FutureContextPanel()
    panel.load("- col: abc\n- pref: metric")
    assert "col: abc" in panel._bullets
    assert "pref: metric" in panel._bullets


def test_panel_hide_clears_bullets_and_display():
    panel = FutureContextPanel()
    panel._bullets = "- something"
    panel.display = True
    # Patch _body.update so _rerender doesn't need a Textual app.
    panel._body = type("_FakeStatic", (), {"update": lambda self, x: None})()
    panel.hide()
    assert panel._bullets == ""
    assert not panel.display


# ---------------------------------------------------------------------------
# Event parsing — future_context captured from deep result events
# ---------------------------------------------------------------------------

def test_deep_result_event_captures_future_context():
    """A result event with result_kind='deep' and data.future_context is stored."""
    app, _log = _make_app()
    event = {
        "type": "result",
        "result_kind": "deep",
        "text": "",
        "data": {"future_context": "- key: value\n- another: bullet"},
    }
    app._partial_started = False
    app._answer_parts = []
    app._handle_event(event)
    assert "key: value" in app._future_context
    assert "another: bullet" in app._future_context


def test_deep_result_event_no_future_context_leaves_state_unchanged():
    """A deep result with no future_context does not overwrite existing context."""
    app, _log = _make_app()
    app._future_context = "- previous: context"
    event = {
        "type": "result",
        "result_kind": "deep",
        "text": "",
        "data": {},
    }
    app._partial_started = False
    app._answer_parts = []
    app._handle_event(event)
    # Should be unchanged, not wiped.
    assert app._future_context == "- previous: context"


def test_non_deep_result_event_does_not_affect_future_context():
    """A plain answer result must not set future_context."""
    app, _log = _make_app()
    app._future_context = ""
    event = {
        "type": "result",
        "result_kind": "answer",
        "text": "Here is your answer.",
        "data": {},
    }
    app._partial_started = False
    app._answer_parts = []
    app._handle_event(event)
    assert app._future_context == ""


def test_deep_result_whitespace_only_not_stored():
    """An explicit whitespace-only future_context is treated as absent."""
    app, _log = _make_app()
    app._future_context = ""
    event = {
        "type": "result",
        "result_kind": "deep",
        "text": "",
        "data": {"future_context": "   "},
    }
    app._partial_started = False
    app._answer_parts = []
    app._handle_event(event)
    assert app._future_context == ""
