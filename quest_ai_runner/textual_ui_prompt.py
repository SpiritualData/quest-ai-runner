"""The prompt box: Alt+Enter key registration and the multi-line PromptTextArea.

Split out of ``textual_ui.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import enum
from textual._ansi_sequences import ANSI_SEQUENCES_KEYS
from textual.message import Message
from textual.widgets import TextArea


# ── Multi-line prompt input ────────────────────────────────────────────────────

# Textual 8 parses ESC CR by re-reading the CR on its own, and CR maps to plain "enter", so the
# Alt is lost and Alt+Enter submitted. Mapping the whole sequence makes the parser report it as
# "alt+enter" before that fallback runs.
_ALT_ENTER_SEQUENCE = "\x1b\r"


_QarKeys = enum.Enum("_QarKeys", {"AltEnter": "alt+enter"})


ANSI_SEQUENCES_KEYS.setdefault(_ALT_ENTER_SEQUENCE, (_QarKeys.AltEnter,))


class PromptTextArea(TextArea):
    """Auto-expanding multi-line input. Enter submits; Shift+Enter adds a newline.

    Most terminals send the same carriage return for Enter and Shift+Enter, so Shift+Enter is
    only seen as such where the terminal speaks the kitty keyboard protocol (kitty, WezTerm,
    Ghostty, foot, Alacritty, iTerm2 with CSI u on). GNOME Terminal and the other VTE terminals
    do not, and nothing on the terminal's input stream separates the two there. Everywhere a
    newline also comes from one of the sequences a terminal CAN tell apart from Enter:
      - Alt+Enter (ESC CR), which is also what Claude Code's /terminal-setup maps Shift+Enter to.
        Textual drops the Alt from ESC CR, so ``_ALT_ENTER_SEQUENCE`` registers it;
      - Ctrl+J (a bare LF);
      - a backslash typed right before Enter, the Claude Code convention. VS Code's
        /terminal-setup keybinding sends Shift+Enter as backslash, CR, LF, so the LF that
        trails a backslash-Enter is part of the same keypress and is dropped.
    """

    class Submitted(Message):
        def __init__(self, textarea: "PromptTextArea", value: str) -> None:
            super().__init__()
            self.textarea = textarea
            self.value = value

    MAX_LINES = 8
    # A trailing LF read this soon after a backslash-Enter belongs to the same keypress. Measured
    # on the events' own timestamps (set when the terminal input is parsed), so a busy UI thread
    # that handles the keys late does not split one keypress into two.
    PAIRED_LF_WINDOW_SECONDS = 0.05

    backslash_newline_at: float = 0.0

    def on_key(self, event) -> None:
        key = event.key
        menu_key = getattr(self.app, "command_menu_key", None)
        if menu_key is not None and menu_key(key):
            event.prevent_default()
            event.stop()
            return
        if key == "enter":
            event.prevent_default()
            event.stop()
            if self.consume_backslash_before_cursor():
                self.insert("\n")
                self.backslash_newline_at = event.time
                return
            self.post_message(self.Submitted(self, self.text))
        elif key in ("shift+enter", "alt+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            paired_lf = (
                key == "ctrl+j"
                and event.time - self.backslash_newline_at < self.PAIRED_LF_WINDOW_SECONDS
            )
            self.backslash_newline_at = 0.0
            if not paired_lf:
                self.insert("\n")
        else:
            self.backslash_newline_at = 0.0

    def consume_backslash_before_cursor(self) -> bool:
        """Delete a backslash sitting right before an empty-selection cursor; report whether one was."""
        if not self.selection.is_empty:
            return False
        row, col = self.cursor_location
        if col == 0 or self.document[row][col - 1] != "\\":
            return False
        self.delete((row, col - 1), (row, col))
        return True

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self.fit_height()

    def on_resize(self, event) -> None:
        # A new width re-wraps the text, which changes how many rows it needs. TextArea re-wraps
        # in its own resize handler, which runs after this one, so measure once that has happened.
        self.call_after_refresh(self.fit_height)

    def fit_height(self) -> None:
        """Grow (or shrink) to show every row the text occupies, wrapped rows included."""
        rows = min(max(self.wrapped_document.height, 1), self.MAX_LINES)
        # gutter = the border and padding rows (2 with the tall border this app styles it with).
        new_height = rows + self.styles.gutter.height
        if self.styles.height is None or self.styles.height.value != new_height:
            self.styles.height = new_height
        # Once the box is tall enough for everything, the rows scrolled off the top come back.
        self.call_after_refresh(self.scroll_cursor_visible)
