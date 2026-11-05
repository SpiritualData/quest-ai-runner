"""Draw the hub's terminal blocks in the Textual transcript.

``hubofhubs.terminal`` decides what is shown (the work summary, the folded updates, the ask, the stopped reason); this module only
turns each ``Block`` into styled lines and says which key does each action. Nothing here decides wording or rules.
"""
from __future__ import annotations

from typing import List, Tuple

from rich.text import Text

from hubofhubs.the_consciousness.terminal import Block

from .textual_ui_helpers import indented_line

#: The semantic style of a block, as this terminal paints it.
STYLES = {"plain": "", "muted": "dim", "emphasis": "bold yellow", "warning": "yellow"}

#: The key (or command) behind each action id of a block. An action with no entry here is not offered in this terminal.
ACTION_KEYS = {
    "expand_trail": "Alt+T", "collapse_trail": "Alt+T", "show_all_trail": "Alt+T",
    "expand_updates": "Alt+U", "collapse_updates": "Alt+U",
    "continue": "Alt+K", "not_now": "/notnow", "answer_now": "/answer",
}


def hint_for(block: Block) -> str:
    """The short key hint closing a block: ``Alt+T show details``. Option and send actions are answered by typing, so they add none."""
    parts = [f"{ACTION_KEYS[a]} {label.lower()}" for a, label in block.actions if a in ACTION_KEYS]
    return ", ".join(parts)


def block_lines(block: Block) -> List[Text]:
    """The lines a block is written as (a removed block is none). The live trail line has no lines: it is shown on its own row."""
    if block.removed or block.kind == "trail_live":
        return []
    style = STYLES.get(block.style, "")
    hint = hint_for(block)
    if block.kind == "ask" and block.state == "open":
        bar = "bold yellow"
        lines = [indented_line(f"  ┃ {block.title}", bar, hang=4, carry=True), indented_line(f"  ┃ {block.text}", bar, hang=4, carry=True)]
        lines += [indented_line(f"  ┃ {row}", "yellow", hang=4, carry=True) for row in block.rows]
        lines.append(indented_line("  ┃ Type your answer, or a number to pick an option. /notnow keeps this open.", "dim", hang=4, carry=True))
        return lines
    if block.kind == "earlier_updates":
        marker = "▾" if block.state == "expanded" else "▸"
        head = f"  {marker} {block.title}" + (f" · {block.text}" if block.text else "")
        lines = [indented_line(head + (f" · {hint}" if hint else ""), style, hang=4)]
        lines += [indented_line(f"    {row}", style, hang=4) for row in block.rows]
        return lines
    if block.kind == "interim":
        lines = [indented_line(f"  ◐ {block.title}", "bold dim", hang=4)]
        lines += [indented_line(f"    {row}", "", hang=4) for row in block.text.splitlines() if row.strip()]
        lines += [indented_line(f"    {row}", "dim", hang=4) for row in block.rows]
        return lines
    joiner = ". " if block.kind == "ask" else ": "
    head = "  " + (f"{block.title}{joiner}" if block.title and block.text else block.title) + block.text
    inline = bool(hint) and not block.rows and len(head) + len(hint) < 70   # a long line would wrap the hint away from its key
    lines = [indented_line(head + (f" · {hint}" if inline else ""), style, hang=2)]
    lines += [indented_line(f"    {row}", style, hang=4) for row in block.rows]
    if hint and not inline:
        lines.append(indented_line(f"    {hint}", "dim", hang=4))
    return lines


def key_hints_for(actions: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """``(key, label)`` pairs for the key line, for the actions this terminal binds to a key (the slash commands are not keys)."""
    return [(ACTION_KEYS[a].lower(), label) for a, label in actions if a in ACTION_KEYS and ACTION_KEYS[a].startswith("Alt+")]
