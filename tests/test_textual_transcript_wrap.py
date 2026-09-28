"""On a narrow terminal (phone-width SSH session), transcript text must wrap, not crop.

`RichLog.write()` computes its wrap width as
``max(shrink_to_fit_width, self.min_width)`` -- so `min_width` is a FLOOR, not a
default only used when no better width is known. `RichLog`'s own default
(``min_width=78``) meant every write still wrapped at 78 columns even when the
widget's visible width was much narrower, and the extra width was then cropped
off by `render_line` rather than ever reaching a second visual line: on a
30-column-wide terminal the text was cut off at the edge of the screen exactly
as reported, never wrapping. Fixed by passing `min_width=1` to the
`TranscriptLog` in `QuestAITerminal.compose()`, so the wrap width is governed
purely by the widget's real available width.
"""
from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from rich.text import Text
from textual.app import App, ComposeResult

from quest_ai_runner.textual_ui import QuestAITerminal, TranscriptLog

LONG_TEXT = (
    "This is a fairly long sentence that should wrap onto multiple lines when the "
    "terminal is narrow instead of being cut off at the edge of the screen."
)


class _TranscriptOnlyApp(App):
    """Minimal harness mirroring how QuestAITerminal constructs its TranscriptLog."""

    def __init__(self, min_width: int) -> None:
        super().__init__()
        self._min_width = min_width

    def compose(self) -> ComposeResult:
        yield TranscriptLog(id="transcript", max_lines=20000, wrap=True,
                             min_width=self._min_width, highlight=True,
                             markup=True, auto_scroll=True)


@pytest.mark.asyncio
async def test_narrow_terminal_wraps_instead_of_cropping():
    app = _TranscriptOnlyApp(min_width=1)
    async with app.run_test(size=(30, 24)) as pilot:
        log = app.query_one(TranscriptLog)
        text = Text(LONG_TEXT)
        text.no_wrap = False
        log.write(text)
        await pilot.pause()

        visible_width = log.scrollable_content_region.width
        line_texts = [strip.text for strip in log.lines]

        # Every stored line must fit within what the widget can actually show --
        # otherwise the tail of the line is cropped, not wrapped onto the next one.
        assert all(len(line.rstrip()) <= visible_width for line in line_texts), (
            f"a line exceeds the visible width {visible_width}: {line_texts!r}"
        )
        # And the full message must still be recoverable across the wrapped lines.
        assert "".join(line_texts).replace(" ", "") == LONG_TEXT.replace(" ", "")


@pytest.mark.asyncio
async def test_default_min_width_would_have_cropped_on_a_narrow_terminal():
    """Guard the regression: RichLog's own default (78) reproduces the cut-off bug."""
    app = _TranscriptOnlyApp(min_width=78)
    async with app.run_test(size=(30, 24)) as pilot:
        log = app.query_one(TranscriptLog)
        text = Text(LONG_TEXT)
        text.no_wrap = False
        log.write(text)
        await pilot.pause()

        visible_width = log.scrollable_content_region.width
        line_texts = [strip.text for strip in log.lines]
        assert any(len(line.rstrip()) > visible_width for line in line_texts), (
            "expected the unfixed default to overflow the visible width; "
            "if this fails, RichLog's behavior changed and the fix may be obsolete"
        )


def test_quest_ai_terminal_transcript_uses_a_narrow_safe_min_width():
    """Static check on the real app: the transcript must not use RichLog's 78-column floor."""
    import inspect

    source = inspect.getsource(QuestAITerminal.compose)
    assert "min_width=1" in source, (
        "QuestAITerminal's TranscriptLog must pass min_width=1 (or another value "
        "safe for a narrow phone terminal), not RichLog's default of 78"
    )
