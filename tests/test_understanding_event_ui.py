"""Tests for EVENT_UNDERSTANDING rendering in both terminal UIs.

The orchestrator emits EVENT_UNDERSTANDING right after Stage 1 (user input understanding)
resolves the goal condition, well before the planner loop or answer generation runs, so a
user needs to see "Understood as: ..." as its own clearly-distinct line the instant it
arrives, not blended into a plan/status line or mistaken for a blocking EVENT_DECISION
question. Covers:

1. textual_ui.QuestAITerminal._handle_event() renders it via a distinct glyph/style,
   separate from the yellow EVENT_DECISION marker.
2. interactive._TurnRenderer._types() and .render() do the same for the plain-terminal path
   (rich / color / plain-ANSI fallbacks all covered).
"""

from __future__ import annotations

from typing import List

from quest_ai_runner.core.adapters import EVENT_DECISION, EVENT_UNDERSTANDING
from quest_ai_runner.interactive import _DeepRunTracker
from quest_ai_runner.textual_ui import QuestAITerminal


# ---------------------------------------------------------------------------
# textual_ui.QuestAITerminal
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


def _make_app() -> tuple[QuestAITerminal, _RecordingLog]:
    app = QuestAITerminal(_FakeSession())
    app._deep = _DeepRunTracker()
    app._deep_flushed = set()
    log = _RecordingLog()
    app._tlog = log
    # _ev is resolved lazily; leave it None so _types() initialises it.
    app._ev = None
    return app, log


def test_understanding_event_renders_distinct_line():
    app, log = _make_app()
    event = {
        "type": EVENT_UNDERSTANDING,
        "text": "Understood as: add a --dry-run flag to poll and commit it",
    }
    app._handle_event(event)
    body = "\n".join(log.lines)
    assert "Understood as: add a --dry-run flag to poll and commit it" in body
    assert "◆" in body  # the chosen glyph (diamond)
    assert "┃" not in body  # not the decision marker's glyph


def test_understanding_event_does_not_set_awaiting_decision():
    """Understanding is informational, not blocking; it must not arm the decision-prompt state."""
    app, _log = _make_app()
    app._awaiting_decision = False
    event = {"type": EVENT_UNDERSTANDING, "text": "Understood as: something"}
    app._handle_event(event)
    assert app._awaiting_decision is False


def test_understanding_event_empty_text_writes_nothing():
    app, log = _make_app()
    event = {"type": EVENT_UNDERSTANDING, "text": ""}
    app._handle_event(event)
    assert log.lines == []


# ---------------------------------------------------------------------------
# interactive._TurnRenderer
# ---------------------------------------------------------------------------

def _make_renderer():
    from quest_ai_runner.interactive import _Console, _ContextPanel, _TurnRenderer
    console = _Console()
    console._rich = None
    console._color = False
    lines: List[str] = []
    console.line = lambda s="": lines.append(s)  # type: ignore[assignment]
    console.write = lambda s: lines.append(s)  # type: ignore[assignment]
    console.markdown = lambda s: lines.append(s)  # type: ignore[assignment]
    panel = _ContextPanel(console)
    renderer = _TurnRenderer(console, panel, "Tester")
    return renderer, lines


def test_turn_renderer_types_includes_understanding():
    renderer, _lines = _make_renderer()
    types = renderer._types()
    assert types.get("understanding") == EVENT_UNDERSTANDING


def test_turn_renderer_renders_understanding_event_plain():
    renderer, lines = _make_renderer()
    event = {"type": EVENT_UNDERSTANDING, "text": "Understood as: rename the helper"}
    renderer.render(event)
    assert any("Understood as: rename the helper" in ln for ln in lines), (
        f"understanding text should be rendered; got lines: {lines}"
    )
    assert any("◆" in ln for ln in lines), f"expected diamond glyph in lines: {lines}"


def test_turn_renderer_understanding_distinct_from_decision():
    renderer, lines = _make_renderer()
    renderer.render({"type": EVENT_UNDERSTANDING, "text": "Understood as: X"})
    renderer.render({"type": EVENT_DECISION, "text": "Proceed with X?"})
    body = "\n".join(lines)
    assert "◆ Understood as: X" in body
    assert "? Proceed with X?" in body
