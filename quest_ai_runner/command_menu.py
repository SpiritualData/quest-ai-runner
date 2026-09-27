"""command_menu — what the chat's "/" menu offers for whatever is typed so far (UI-free).

Typing "/" in the prompt shows every command with its one-line description, narrowing as you type,
the way Claude Code's slash menu does. After "/quest " it offers the quest choices instead: turn
matching off, back on, or pick one of the quests this account can reach. The Textual app renders
these items and handles the keys; everything about WHAT to offer lives here, so it is testable
without a terminal.

Command descriptions are parsed from the session's own ``/help`` text, so the menu and the help can
never disagree about what a command does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence

HELP_LINE_RE = re.compile(r"^\s{4}(/\S+(?:, /\S+)*(?: [^\s].*?)?)\s{2,}(\S.*)$")

MAX_ITEMS = 40  # the menu scrolls; typing narrows it
MAX_QUEST_ITEMS = 60  # the menu scrolls; typing narrows it


@dataclass
class MenuItem:
    """One row of the menu.

    ``label`` is what is shown on the left, ``description`` on the right. ``completion`` is what
    the prompt holds after Tab; ``submit`` says whether Enter should run it at once (a complete
    command) or only complete it (a command that still needs an argument).
    """
    label: str
    description: str
    completion: str
    submit: bool


def parse_help(help_text: str) -> List[MenuItem]:
    """Every command row of the /help text, in order."""
    items: List[MenuItem] = []
    for line in help_text.splitlines():
        m = HELP_LINE_RE.match(line)
        if not m:
            continue
        usage, description = m.group(1).strip(), m.group(2).strip()
        first = usage.split(", ")[0]
        command, _, args = first.partition(" ")
        needs_arg = args.startswith("<")
        literal_arg = bool(args) and not args.startswith(("<", "["))
        if literal_arg:
            completion, submit = f"{command} {args}", True
        elif needs_arg:
            completion, submit = f"{command} ", False
        else:
            completion, submit = command, True
        items.append(MenuItem(label=usage, description=description, completion=completion,
                              submit=submit))
    return items


def quest_items(query: str, quests: Sequence, mode: str = "auto",
                pinned_id: Optional[str] = None) -> List[MenuItem]:
    """The choices after "/quest ": off, on, then each reachable quest matching the query."""
    query_words = query.lower().split()

    def wanted(*texts: str) -> bool:
        hay = " ".join(texts).lower()
        return all(w in hay for w in query_words)

    items: List[MenuItem] = []
    if wanted("none off no quest"):
        items.append(MenuItem("none", "Never add a quest to your messages"
                              + (" (current)" if mode == "none" and not pinned_id else ""),
                              "/quest none", True))
    if wanted("auto on match"):
        items.append(MenuItem("auto", "Match each message to the quest it is about"
                              + (" (current)" if mode == "auto" and not pinned_id else ""),
                              "/quest auto", True))
    for q in quests:
        title = getattr(q, "title", "") or q.quest_id
        folder = getattr(q, "folder", "") or ""
        if not wanted(title, q.quest_id, folder):
            continue
        where = folder if folder else "not synced to a local folder"
        current = " (pinned)" if pinned_id and q.quest_id == pinned_id else ""
        items.append(MenuItem(title, f"{where}{current}", f"/quest {q.quest_id}", True))
    return items


def menu_items(text: str, commands: Sequence[MenuItem], quests: Sequence = (),
               mode: str = "auto", pinned_id: Optional[str] = None) -> List[MenuItem]:
    """What to show for the prompt's current text; [] means hide the menu."""
    if not text.startswith("/") or "\n" in text:
        return []
    if text.startswith("/quest "):
        return quest_items(text[len("/quest "):], quests, mode, pinned_id)[:MAX_QUEST_ITEMS]
    typed = text.strip()
    if " " in text:
        return []  # past the command name and into its argument: nothing more to suggest
    return [c for c in commands if c.label.startswith(typed)
            or any(part.startswith(typed) for part in c.label.split(", "))][:MAX_ITEMS]
