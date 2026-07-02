"""Tests for drag-to-select + copy in the transcript (TranscriptLog).

A stock RichLog can't show or copy a selection (its render_line returns pre-baked
strips and never paints the screen--selection highlight). TranscriptLog implements
selection itself; these tests cover the pure pieces — text extraction and the
highlight painter — plus the no-selection default, all offline.
"""

from __future__ import annotations

from rich.segment import Segment
from rich.style import Style
from textual.geometry import Offset
from textual.selection import Selection
from textual.strip import Strip

from quest_ai_runner.textual_ui import TranscriptLog


def _cell_styles(strip: Strip) -> list:
    """Style per visible cell, expanded from the strip's segments."""
    out: list = []
    for seg in strip._segments:
        for _ in seg.text:
            out.append(seg.style)
    return out


# --- text extraction -------------------------------------------------------

def test_extract_single_line():
    lines = ["hello world", "second line"]
    sel = Selection.from_offsets(Offset(0, 0), Offset(5, 0))
    assert TranscriptLog._extract_selection(lines, sel) == "hello"


def test_extract_across_lines():
    lines = ["hello world", "second line", "third row"]
    sel = Selection.from_offsets(Offset(6, 0), Offset(6, 1))
    # from col 6 of line 0 ("world") through col 6 of line 1 ("second")
    assert TranscriptLog._extract_selection(lines, sel) == "world\nsecond"


def test_extract_reversed_drag_is_normalized():
    """Dragging bottom-to-top selects the same text as top-to-bottom."""
    lines = ["hello world", "second line", "third row"]
    down = Selection.from_offsets(Offset(6, 0), Offset(6, 1))
    up = Selection.from_offsets(Offset(6, 1), Offset(6, 0))
    assert TranscriptLog._extract_selection(lines, up) == TranscriptLog._extract_selection(lines, down)


def test_extract_trims_trailing_padding():
    lines = ["hello      ", "world"]
    sel = Selection.from_offsets(Offset(0, 0), Offset(0, 1))  # whole first line
    assert TranscriptLog._extract_selection(lines, sel) == "hello"


# --- highlight painter -----------------------------------------------------

def test_apply_highlight_styles_only_the_selected_cells():
    strip = Strip([Segment("hello world")])
    styled = TranscriptLog._apply_highlight(strip, 0, 5, Style(reverse=True))
    assert styled.text == "hello world"           # text unchanged
    assert styled.cell_length == strip.cell_length
    styles = _cell_styles(styled)
    assert all(s is not None and s.reverse for s in styles[0:5])   # "hello" highlighted
    assert not (styles[6] and styles[6].reverse)                   # "world" not highlighted


def test_apply_highlight_midspan():
    strip = Strip([Segment("hello world")])
    styled = TranscriptLog._apply_highlight(strip, 6, 11, Style(reverse=True))
    styles = _cell_styles(styled)
    assert not (styles[0] and styles[0].reverse)                   # "hello" untouched
    assert all(s is not None and s.reverse for s in styles[6:11])  # "world" highlighted


def test_apply_highlight_noop_when_empty_range():
    strip = Strip([Segment("hello world")])
    assert TranscriptLog._apply_highlight(strip, 4, 4, Style(reverse=True)) is strip


# --- widget-level default --------------------------------------------------

def test_no_selection_yields_empty_text():
    t = TranscriptLog()
    assert t.selected_text == ""


def test_selected_text_reads_from_lines():
    t = TranscriptLog()
    t.lines = [Strip([Segment("hello world")]), Strip([Segment("second line")])]
    t._sel_anchor = Offset(0, 0)
    t._sel_head = Offset(5, 0)
    assert t.selected_text == "hello"
