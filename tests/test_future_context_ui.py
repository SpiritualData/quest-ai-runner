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


# ---------------------------------------------------------------------------
# Narration beats render INTO the transcript feed (not a separate bottom bar)
# ---------------------------------------------------------------------------

def test_narration_beat_written_to_transcript_feed():
    """An EVENT_PARTIAL tagged data={narration:True} lands in the main transcript log,
    so the reasoning of what's happening reads inline above the answer."""
    app, log = _make_app()
    app._last_narration = ""
    event = {
        "type": "partial",
        "text": "Looking through the codebase for where this is handled…",
        "data": {"narration": True},
    }
    app._handle_event(event)
    body = "\n".join(log.lines)
    assert "Looking through the codebase" in body
    assert app._last_narration == "Looking through the codebase for where this is handled…"


def test_narration_beat_consecutive_duplicate_not_doubled():
    """A re-emitted identical beat is dropped so it doesn't stack twice in the feed."""
    app, log = _make_app()
    app._last_narration = ""
    event = {"type": "partial", "text": "Same beat", "data": {"narration": True}}
    app._handle_event(event)
    app._handle_event(event)
    hits = [ln for ln in log.lines if "Same beat" in ln]
    assert len(hits) == 1


# ---------------------------------------------------------------------------
# Context showcase: shallow turns keep their context cards accessible too
# (not just deep turns with a future_context bullet list from the orchestrator)
# ---------------------------------------------------------------------------

from quest_ai_runner.textual_ui import _build_shallow_context_bullets  # noqa: E402


def test_build_shallow_context_bullets_from_cards():
    cards = [
        {"id": "card-1", "title": "Pricing tiers", "adapter": "keyword"},
        {"id": "card-2", "title": "Refund policy", "adapter": "vector"},
    ]
    bullets = _build_shallow_context_bullets(cards)
    assert "keyword: Pricing tiers" in bullets
    assert "vector: Refund policy" in bullets


def test_build_shallow_context_bullets_empty_when_no_cards():
    assert _build_shallow_context_bullets([]) == ""
    assert _build_shallow_context_bullets(None) == ""


def test_build_shallow_context_bullets_falls_back_to_id_when_no_title():
    bullets = _build_shallow_context_bullets([{"id": "card-1", "adapter": "keyword"}])
    assert "card-1" in bullets


def test_build_shallow_context_bullets_no_em_dash():
    bullets = _build_shallow_context_bullets([{"id": "c1", "title": "x", "adapter": "keyword"}])
    assert "—" not in bullets


class _FakeFinalAnswer:
    """Minimal OrchestratorResult-like stand-in for a shallow (answer) turn."""
    kind = "answer"
    text = "Here is your answer."
    deep_results = []
    goals = []
    tokens_in = 10
    tokens_out = 20
    model = "claude-sonnet-4-6"
    steps = 1


class _FakeSessionForFinishTurn:
    """A session stand-in with just enough surface for _finish_turn (and the on_mount header
    it triggers, since QuestAITerminal is driven through a real app run_test() here) to work."""
    _rep_name = "Tester"
    _cfg = type("Cfg", (), {"corpus_root": None})()
    _goal_id = None
    _model_hint = None

    def __init__(self) -> None:
        self._last_user = ""
        self._last_assistant = ""
        self._session_history: list = []
        self._turn_count = 0
        self._turns: list = []
        self._orch = type("Orch", (), {"context_assembler": None})()

    def _write_session_file(self) -> None:
        pass


@pytest.mark.asyncio
async def test_shallow_turn_with_context_cards_shows_alt_c_hint():
    """A shallow (non-deep) turn that gathered context cards gets the same
    "[Alt+C] Context it used" footer hint that deep turns get, and the cards survive
    into the FutureContextPanel instead of being discarded when the turn ends."""
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        app._ctx.set_cards([
            {"id": "card-1", "title": "Pricing tiers", "adapter": "keyword"},
        ])
        app._future_context = ""  # shallow turns never populate this from the orchestrator
        app._answer_parts = []
        app._auto_pass = 1

        app._finish_turn("what are the pricing tiers?", _FakeFinalAnswer(), 1.2,
                         cancelled=False, error=None)

        # The panel picked up bullets built from the ContextPanel's cards (not discarded).
        assert "Pricing tiers" in app._future_ctx_panel._bullets


@pytest.mark.asyncio
async def test_shallow_turn_without_context_cards_no_panel_load():
    """A shallow turn that gathered no context cards leaves the panel empty (nothing to show)."""
    app = QuestAITerminal(_FakeSessionForFinishTurn())
    async with app.run_test():
        app._ctx.reset()
        app._future_context = ""
        app._answer_parts = []
        app._auto_pass = 1

        app._finish_turn("a plain question", _FakeFinalAnswer(), 0.8,
                         cancelled=False, error=None)

        assert app._future_ctx_panel._bullets == ""
