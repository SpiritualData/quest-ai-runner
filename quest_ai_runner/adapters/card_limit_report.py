"""QAR's voice for the hub's card limit notice and report: the commands, the setting name and the words for a quest-managed card.

The counting, the fit to a screen and the report layout are the hub's (``hubofhubs.recall.card_limit_report``). This module is
what only QAR knows: ``quest-ai-runner cards ...`` commands, ``QAR_MAX_CARDS``, how a lane reads its environment, and that a
card another system manages is "from a quest". The names the hub's report module defines (``breakdown``, ``REPORT_NAME``,
``write_report`` ...) are imported from it directly; the functions here are those that need the voice.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hubofhubs.recall import card_limit as limit_mod
from hubofhubs.recall import card_limit_report as hub_report
from hubofhubs.recall.card_limit_report import KEPT_OVER_WHY, Breakdown, Voice, can_set_aside
from hubofhubs.recall.card_repository import QUARANTINE_DIR_NAME, FilesystemCardRepository

from . import card_store_tidy as tidy
from .card_store_settings import ENV_MAX_CARDS

QUEST_COUNT_WORDS = (("topic", "imported", "learned", "quest"), ("topic", "imp", "learned", "quest"), ("top", "imp", "lrn", "qst"))


def how_to_change_line() -> str:
    """The second line: the tidy first, a higher limit second. Short enough for the same window, no full stop inside."""
    return f"Run quest-ai-runner cards tidy --apply, or set {ENV_MAX_CARDS} higher"


def lane_note(lines: List[str], guard: Any = None) -> str:
    """The notice for a lane's task feed, which has a task's text around it: it says what it is about, so a reader
    does not take it for a report on their task. The exact tidy command for this store comes first, a higher limit
    second, and (where the store cannot set a card aside) why learned cards still come in above the limit."""
    repo = getattr(guard, "repo", None)
    cards_dir = getattr(guard, "cards_dir", "")
    text = (f"About the context store, not this task: {lines[0]}. To bring it under the limit run: "
            f"{tidy.apply_command(repo, cards_dir)} (add --merge-topics to also join cards that pin the same files "
            f"under another name). {tidy.REVERSIBLE} Or set {ENV_MAX_CARDS} higher (0 = no limit).")
    if guard is not None and not can_set_aside(guard):
        text += " " + KEPT_OVER_WHY
    return text


def change_instructions(cards_dir: Path, repo: Any = None) -> List[str]:
    where = tidy.lane_args(repo if repo is not None else FilesystemCardRepository(str(cards_dir)), cards_dir)
    return [
        f"First, tidy it: quest-ai-runner cards tidy {where} lists what would be removed (copies, cards about "
        f"excluded folders, duplicates); add --apply to do it, and --merge-topics to also join cards that pin the same "
        f"files under another name onto their keeper. {tidy.REVERSIBLE} A card with notes, usage, quest tags or a "
        "hand-written origin is never removed. A lane tidies on its own at start with card_tidy = \"apply\" in its "
        "config (the default for a local folder; the Quest backend and Qdrant only plan until the owner sets it).",
        f"Or allow more: set {ENV_MAX_CARDS} to the number of cards to allow. 0 (or the word unlimited) means no limit. "
        f"The default is {limit_mod.DEFAULT_MAX_CARDS}.",
        "A lane reads its environment from three places, strongest first: the process environment (the "
        "service unit's Environment= or EnvironmentFile= lines), the .env files named in env_files in "
        "the lane's qar.toml, and the [env] table of that qar.toml (plain defaults). In a terminal chat "
        f"it is the shell environment. Put {ENV_MAX_CARDS}=<number> in whichever of those the "
        "lane uses, then restart the lane (for a systemd lane: systemctl --user restart "
        "<the lane's service>); the new limit applies from the next start.",
        "Or ask the AI that runs the lane: raise the QAR card limit to <number>. It should make that "
        "change where this lane reads its environment and restart it.",
        f"Cards the limit moved out are in {cards_dir / QUARANTINE_DIR_NAME / limit_mod.QUARANTINE_REASON} "
        f"(one line per card in {limit_mod.MANIFEST_NAME}). Move a file back into {cards_dir} to put it back.",
    ]


VOICE = Voice(
    how_to_change_line=how_to_change_line,
    change_instructions=change_instructions,
    lane_note=lane_note,
    report_command=lambda cards_dir: f"quest-ai-runner cards report --cards-dir {cards_dir}",
    count_words=QUEST_COUNT_WORDS,
    managed_row_label="From quests (managed)",
    learned_or_managed="learned or quest",
    managed_source="quests",
    limit_setting=ENV_MAX_CARDS,
)


def notice_lines(b: Breakdown, *, admitted_over: int = 0) -> List[str]:
    """What the person sees on screen: two lines, the counts and then how to change the limit."""
    return hub_report.notice_lines(b, VOICE, admitted_over=admitted_over)


def announce_full(guard: Any, at_open: bool = False, notify: Optional[Callable[[str], None]] = None) -> None:
    """Write the report file and say it (the hub's announcement, in QAR's voice). This is the card limit's ``announcer``."""
    hub_report.announce_full(guard, VOICE, at_open=at_open, notify=notify)


def report_for(cards_dir: str, limit: int, cards: Optional[Dict[str, Dict[str, Any]]] = None) -> str:
    """The report for a store, computed now (what ``quest-ai-runner cards report`` prints)."""
    return hub_report.report_for(cards_dir, limit, VOICE, cards=cards)
