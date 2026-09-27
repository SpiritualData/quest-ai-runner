"""Interactive picker for ``quest-ai-runner chat --resume`` (no id), modeled on Claude Code's.

Arrow keys move, Enter resumes the highlighted conversation, typing filters by the words of the
conversation, Esc (or Ctrl+C) cancels. Runs as its own small Textual app BEFORE the chat app
starts, so the chosen conversation is resolved exactly like ``--resume <id>`` would be.
"""
from __future__ import annotations

import time
from typing import List, Optional

from .chat_conversations import SavedConversation


def describe_age(mtime: float, now: Optional[float] = None) -> str:
    age = max(0.0, (now if now is not None else time.time()) - mtime)
    if age < 60:
        return "just now"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    if age < 86400:
        return f"{int(age // 3600)}h ago"
    return f"{int(age // 86400)}d ago"


def conversation_label(conv: SavedConversation, now: Optional[float] = None) -> str:
    turns = len(conv.history)
    return (f"{describe_age(conv.mtime, now):>9}  {turns:>3} turn{'s' if turns != 1 else ' '}  "
            f"{conv.first_message(90)}")


def conversation_matches(conv: SavedConversation, query: str) -> bool:
    """Every word of the query appears somewhere in the conversation (or its id), any case."""
    words = query.lower().split()
    if not words:
        return True
    haystack = " ".join([conv.short_id] + [u + " " + a for u, a in conv.history]).lower()
    return all(w in haystack for w in words)


def picker_app(convs: List[SavedConversation]):
    """Build the picker app; after it exits, ``app.chosen`` is the pick or None if cancelled."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Footer, Input, OptionList, Static
    from textual.widgets.option_list import Option

    class ConversationPicker(App):
        CSS = """
        #title { padding: 0 1; color: $accent; text-style: bold; }
        #filter { margin: 0 0 1 0; }
        #empty { padding: 0 1; color: $text-muted; }
        """
        BINDINGS = [
            Binding("escape", "cancel", "Cancel", priority=True),
            Binding("ctrl+c", "cancel", "Cancel", show=False, priority=True),
            Binding("up", "move(-1)", "Up", show=False, priority=True),
            Binding("down", "move(1)", "Down", show=False, priority=True),
            Binding("enter", "choose", "Resume", priority=True),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.chosen: Optional[SavedConversation] = None
            self.shown: List[SavedConversation] = list(convs)
            self.now = time.time()

        def compose(self) -> ComposeResult:
            yield Static("Resume a conversation", id="title")
            yield Input(placeholder="Type to search...", id="filter")
            yield OptionList(id="convs")
            yield Static("No conversations match.", id="empty")
            yield Footer()

        def on_mount(self) -> None:
            self.refill()
            self.query_one("#filter", Input).focus()

        def refill(self) -> None:
            options = self.query_one("#convs", OptionList)
            options.clear_options()
            options.add_options([Option(conversation_label(c, self.now)) for c in self.shown])
            if self.shown:
                options.highlighted = 0
            self.query_one("#empty", Static).display = not self.shown

        def on_input_changed(self, event: Input.Changed) -> None:
            self.shown = [c for c in convs if conversation_matches(c, event.value)]
            self.refill()

        def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
            self.finish(event.option_index)

        def action_move(self, step: int) -> None:
            options = self.query_one("#convs", OptionList)
            if not self.shown:
                return
            current = options.highlighted if options.highlighted is not None else 0
            options.highlighted = max(0, min(len(self.shown) - 1, current + step))

        def action_choose(self) -> None:
            self.finish(self.query_one("#convs", OptionList).highlighted)

        def finish(self, index: Optional[int]) -> None:
            if index is None or not (0 <= index < len(self.shown)):
                return
            self.chosen = self.shown[index]
            self.exit()

        def action_cancel(self) -> None:
            self.chosen = None
            self.exit()

    return ConversationPicker()


def pick_conversation(convs: List[SavedConversation]) -> Optional[SavedConversation]:
    """Show the picker and return the chosen conversation, or None if the user cancelled."""
    app = picker_app(convs)
    try:
        app.run()
    except KeyboardInterrupt:
        return None
    return app.chosen
