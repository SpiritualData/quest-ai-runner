"""The "Sources:" header in the Textual terminal's EVENT_CONTEXT rendering must never print with
nothing under it.

Regression: the header was gated on the OUTER `sources` list being non-empty, but each source's
own line was separately gated on that source's `items` being non-empty. A source with no
file-level items (e.g. a recent/card-type context match) left a dangling "Sources:" header
followed immediately by whatever narration beat landed next -- visually indistinguishable from
broken/missing content.
"""
from __future__ import annotations

import pytest

from quest_ai_runner.core.adapters import EVENT_CONTEXT
from quest_ai_runner.textual_ui import QuestAITerminal


class _FakeSession:
    _rep_name = "Tester"
    _console = None
    _cfg = None
    _goal_id = None
    _model_hint = None


class _RecordingLog:
    """Captures everything written to the transcript as plain text (mirrors
    tests/test_deep_output_ui.py's helper of the same name)."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, x) -> None:  # noqa: ANN001 - mirrors RichLog.write
        if hasattr(x, "plain"):
            self.lines.append(x.plain)
        else:
            self.lines.append(str(x))


def _context_event(sources, card_metadata=None):
    return {
        "type": EVENT_CONTEXT,
        "text": "",
        "data": {"card_metadata": card_metadata or [], "sources": sources},
    }


@pytest.mark.asyncio
async def test_source_with_no_items_prints_no_dangling_header():
    app = QuestAITerminal(_FakeSession())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log

        app._handle_event(_context_event([{"label": "recent", "adapter": "recent", "items": []}]))

        body = "\n".join(log.lines)
        assert "Sources:" not in body


@pytest.mark.asyncio
async def test_source_with_items_still_prints_header_and_line():
    app = QuestAITerminal(_FakeSession())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log

        app._handle_event(_context_event(
            [{"label": "files", "adapter": "files", "items": ["a/b/c.py", "a/b/d.py"]}]
        ))

        body = "\n".join(log.lines)
        assert "Sources:" in body
        assert "files" in body
        assert "c.py" in body


@pytest.mark.asyncio
async def test_mixed_sources_only_the_one_with_items_is_listed():
    """A no-items source alongside a with-items one: the header appears once, and only the
    source that actually has content is listed under it."""
    app = QuestAITerminal(_FakeSession())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log

        app._handle_event(_context_event([
            {"label": "recent", "adapter": "recent", "items": []},
            {"label": "files", "adapter": "files", "items": ["x.py"]},
        ]))

        body = "\n".join(log.lines)
        assert body.count("Sources:") == 1
        assert "recent" not in body.split("Sources:")[1]
        assert "files" in body
        assert "x.py" in body


@pytest.mark.asyncio
async def test_no_sources_at_all_prints_no_header():
    app = QuestAITerminal(_FakeSession())
    async with app.run_test():
        log = _RecordingLog()
        app._tlog = log

        app._handle_event(_context_event([]))

        body = "\n".join(log.lines)
        assert "Sources:" not in body
