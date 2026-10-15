"""The transcript log: a RichLog with mouse text selection, click and copy messages.

Split out of ``textual_ui.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import List, Optional
from textual.message import Message
from textual.geometry import Offset
from textual.widgets import RichLog
from rich.style import Style as RichStyle
from rich.segment import Segment
from textual.selection import Selection
from textual.strip import Strip


# ── Transcript widget ─────────────────────────────────────────────────────────
#
# Standard RichLog with auto_scroll=True yanks the view to the bottom on every
# write(), fighting any manual scroll the user makes during an active turn.
# TranscriptLog overrides write() to only auto-scroll when the view is already
# at the tail — so scrolling up actually sticks until the user returns to the bottom.

class TranscriptLog(RichLog):
    """RichLog that respects manual scroll position AND supports drag-to-select.

    Two additions over a plain RichLog:

    1. Manual-scroll-aware ``write`` — streaming only auto-follows the tail when the
       user is already at the bottom (so scrolling up sticks).

    2. Real drag-to-select + copy. A stock ``RichLog`` stores pre-rendered ``Strip``s
       and its ``render_line`` returns them verbatim — it never applies Textual's
       ``screen--selection`` highlight, so Textual's built-in text selection tracks a
       range over the log but NOTHING is ever drawn and no text is extracted (a drag
       looks like it does nothing). We implement selection ourselves: capture the
       mouse on press, track the selected range in content coordinates, paint the
       highlight in ``render_line``, and copy the selected text on release. This works
       regardless of terminal/tmux native-selection quirks because the app owns it.
    """

    # Non-focusable so clicking the transcript never steals keyboard focus from the
    # message input (see also the app-level on_click); selection below does not need
    # focus, it is driven by captured mouse events.
    can_focus = False

    class Copied(Message):
        """Posted when a drag-selection is copied to the clipboard."""

        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    class Clicked(Message):
        """Posted on a plain click (press+release, no drag) so the app can refocus the prompt."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # Selection endpoints in content coordinates: x = column, y = visual row
        # index into ``self.lines`` (post-wrap). None when there is no selection.
        self._sel_anchor: Optional[Offset] = None
        self._sel_head: Optional[Offset] = None
        self._selecting = False

    def write(self, content, width=None, expand=False, shrink=True,
              scroll_end=None, animate=False):
        if scroll_end is None and self.auto_scroll:
            # Checked BEFORE new content is added: if the user is at (or within 1
            # line of) the current tail, follow; otherwise leave them where they are.
            scroll_end = self.scroll_y >= self.max_scroll_y - 1
        return super().write(content, width=width, expand=expand, shrink=shrink,
                             scroll_end=scroll_end, animate=animate)

    # -- drag selection --------------------------------------------------------

    def _content_offset(self, event) -> Offset:
        """Map a mouse event to a (column, visual-row) offset in the log's content."""
        scroll_x, scroll_y = self.scroll_offset
        x = max(0, int(getattr(event, "x", 0)) + scroll_x)
        y = max(0, int(getattr(event, "y", 0)) + scroll_y)
        if self.lines:
            y = min(y, len(self.lines) - 1)
        return Offset(x, y)

    def on_mouse_down(self, event) -> None:
        # Only the left button starts a selection; leave right/middle alone.
        if getattr(event, "button", 1) not in (0, 1):
            return
        self.capture_mouse(True)
        self._selecting = True
        off = self._content_offset(event)
        self._sel_anchor = off
        self._sel_head = off
        self.refresh()
        event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._selecting:
            return
        self._sel_head = self._content_offset(event)
        # Auto-scroll when the drag runs past the top/bottom edge so you can select
        # more than one screenful.
        try:
            height = self.scrollable_content_region.height
        except Exception:  # noqa: BLE001
            height = self.size.height
        if getattr(event, "y", 0) < 0:
            self.scroll_up(animate=False)
        elif getattr(event, "y", 0) >= height:
            self.scroll_down(animate=False)
        self.refresh()
        event.stop()

    def on_mouse_up(self, event) -> None:
        if not self._selecting:
            return
        self._selecting = False
        self.release_mouse()
        text = self.selected_text
        if text:
            # The app owns the actual clipboard write + feedback (see on_transcript_log_copied);
            # the widget just reports what was selected.
            self.post_message(self.Copied(text))
        else:
            # No drag → treat as a plain click: drop any stale highlight and let the
            # app put the cursor back in the message input.
            self.clear_selection()
            self.post_message(self.Clicked())
        event.stop()

    def clear_selection(self) -> None:
        if self._sel_anchor is not None or self._sel_head is not None:
            self._sel_anchor = None
            self._sel_head = None
            self.refresh()

    def _selection(self) -> Optional[Selection]:
        if (
            self._sel_anchor is None
            or self._sel_head is None
            or self._sel_anchor == self._sel_head
        ):
            return None
        return Selection.from_offsets(self._sel_anchor, self._sel_head)

    @property
    def selected_text(self) -> str:
        sel = self._selection()
        if sel is None:
            return ""
        return self._extract_selection([s.text for s in self.lines], sel)

    @staticmethod
    def _extract_selection(line_texts: List[str], sel: Selection) -> str:
        """Extract the selected text from the log's per-line text (pure/testable)."""
        raw = sel.extract("\n".join(line_texts))
        # Trim per-line trailing padding a strip may carry, but keep line breaks.
        return "\n".join(part.rstrip() for part in raw.split("\n")).strip("\n")

    def _selection_style(self) -> RichStyle:
        """Style used to paint the selection.

        Use the theme's selection BACKGROUND only and keep each cell's own foreground,
        so highlighted text stays readable (the resolved ``screen--selection`` style
        can carry a foreground equal to its background, which would make text
        invisible). Fall back to reverse-video when no selection background is themed.
        """
        try:
            themed = self.selection_style  # resolves the screen--selection component
        except Exception:  # noqa: BLE001
            themed = None
        if themed is not None and themed.bgcolor is not None:
            return RichStyle(bgcolor=themed.bgcolor)
        return RichStyle(reverse=True)

    @staticmethod
    def _apply_highlight(strip: Strip, a: int, b: int, style: RichStyle) -> Strip:
        """Return ``strip`` with cells [a, b) restyled with ``style`` (pure/testable).

        ``style`` is overlaid ON TOP of each cell's existing style (``existing + style``)
        so the selection background wins while the cell keeps its own foreground.
        (``Strip.apply_style`` combines the other way and would let the transcript's own
        background override the highlight, showing nothing.)
        """
        length = strip.cell_length
        a = max(0, min(a, length))
        b = max(a, min(b, length))
        if a >= b:
            return strip
        parts = list(strip.divide([a, b, length]))
        if len(parts) < 2:
            return strip
        left, mid, *rest = parts
        mid_hl = Strip(
            [
                Segment(seg.text, (seg.style + style) if seg.style else style, seg.control)
                for seg in mid._segments
            ],
            mid.cell_length,
        )
        return Strip.join([left, mid_hl, *rest])

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        sel = self._selection()
        if sel is None:
            return strip
        scroll_x, scroll_y = self.scroll_offset
        span = sel.get_span(scroll_y + y)
        if span is None:
            return strip
        x0, x1 = span
        length = strip.cell_length
        a = x0 - scroll_x
        b = length if x1 == -1 else x1 - scroll_x
        return self._apply_highlight(strip, a, b, self._selection_style())
