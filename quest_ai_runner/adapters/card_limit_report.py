"""What a store holds and what its card limit has done: the notice, the report file, ``cards report``.

The limit (``card_limit``) acts only together with this: whoever hits it is told what fills the store
and how to change the limit, in words an AI asked to change it can act on without anyone explaining.
Plain text, stdlib only; no real paths or names in it (the store's own path is filled in at run time).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import card_limit as limit_mod
from . import card_origin
from .card_repository import QUARANTINE_DIR_NAME, FilesystemCardRepository
from .context_card_ceilings import card_folder_key

log = logging.getLogger("quest-ai-runner.card-limit")

REPORT_NAME = "card_limit_report.md"
SCREEN_FOLDERS = 3          # folders named in the log text
SCREEN_CHARS = 72           # an 80 column terminal shows 72 characters of a warning line
REPORT_FOLDERS = 15
REPORT_COLDEST = 8
NAME_CHARS = 18
TOP_LEVEL = "(top level)"


@dataclass
class Breakdown:
    limit: int
    total: int
    topic: int = 0
    imported: int = 0
    learned: int = 0
    managed: int = 0
    topic_by_folder: List[Tuple[str, int]] = field(default_factory=list)
    imported_by_source: List[Tuple[str, int]] = field(default_factory=list)
    quarantined: Dict[str, int] = field(default_factory=dict)
    coldest: List[Dict[str, Any]] = field(default_factory=list)


def top_folder(card: Dict[str, Any]) -> str:
    """The top-level folder a topic card is about (the first part of the deepest folder its files share)."""
    key = card_folder_key(card)
    first = key.split("/", 1)[0] if key else ""
    return first if first and first != "." else TOP_LEVEL


def source_of(card: Dict[str, Any]) -> str:
    prov = card_origin.provenance_of(card)
    return str(prov.get(card_origin.ORIGIN_ROOT) or prov.get("imported_from") or "(unknown source)")


def count_quarantined(cards_dir: Path) -> Dict[str, int]:
    """Cards moved out of the index, by the folder (reason) they were moved to. Names only."""
    out: Dict[str, int] = {}
    base = cards_dir / QUARANTINE_DIR_NAME
    try:
        for reason in sorted(os.listdir(base)):
            folder = base / reason
            if folder.is_dir():
                n = sum(1 for name in os.listdir(folder) if name.endswith(".json"))
                if n:
                    out[reason] = n
    except OSError:
        pass
    return out


def breakdown(cards: Dict[str, Dict[str, Any]], cards_dir: Path, limit: int) -> Breakdown:
    result = Breakdown(limit=limit, total=len(cards), quarantined=count_quarantined(cards_dir))
    folders: Dict[str, int] = {}
    sources: Dict[str, int] = {}
    derived: List[Tuple[Any, str, str]] = []
    for cid, card in cards.items():
        kind = limit_mod.card_kind(card)
        if kind == limit_mod.KIND_MANAGED:
            result.managed += 1
        elif kind == limit_mod.KIND_LEARNED:
            result.learned += 1
        elif kind == limit_mod.KIND_IMPORTED:
            result.imported += 1
            name = source_of(card)
            sources[name] = sources.get(name, 0) + 1
        else:
            result.topic += 1
            name = top_folder(card)
            folders[name] = folders.get(name, 0) + 1
        if kind in limit_mod.DERIVED_KINDS:
            derived.append((limit_mod.coldness(cid, card), cid, kind))
    result.topic_by_folder = sorted(folders.items(), key=lambda kv: (-kv[1], kv[0]))
    result.imported_by_source = sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))
    for key, cid, kind in sorted(derived)[:REPORT_COLDEST]:
        card = cards[cid]
        touched = key[1]
        result.coldest.append({
            "id": cid, "kind": kind, "used": key[0],
            "touched": datetime.fromtimestamp(touched, timezone.utc).strftime("%Y-%m-%d") if touched else "never",
            "created": str(card_origin.provenance_of(card).get("created_at") or "")[:10] or "unknown",
        })
    return result


def short(name: str) -> str:
    return name if len(name) <= NAME_CHARS else name[: NAME_CHARS - 1] + "…"


def grouped(n: int) -> str:
    return f"{n:,}"


def plain(n: int) -> str:
    return str(n)


def thousands(n: int) -> str:
    return f"{round(n / 1000)}k" if n >= 10_000 else str(n)


def compact(n: int) -> str:
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"


def count_line(b: Breakdown) -> str:
    """The one line that says the store is full and what fills it, at most ``SCREEN_CHARS`` long when it can be.

    ``Context store full at 1,000: 812 topic, 150 imported, 30 learned, 8 quest``. The terminal shows the first
    sentence of a warning cut to the width of the window (80 columns leave 72 characters), so this is one line
    with no full stop inside it, and when the numbers are large it shortens its first words, drops the thousands
    separators, then writes thousands as ``12k``, then every count as ``1.4k``. Nothing is ever left out of it: all four counts are
    always there, and the longest form that fits is the one used.
    """
    over = b.total > b.limit
    for fmt in (grouped, plain, thousands, compact):
        for head in (("Context store over its limit of {limit} ({total})", "Store over limit {limit} ({total})",
                      "Over limit {limit} ({total})") if over
                     else ("Context store full at {limit}", "Store full at {limit}", "Full at {limit}")):
            line = (head.format(limit=fmt(b.limit), total=fmt(b.total))
                    + f": {fmt(b.topic)} topic, {fmt(b.imported)} imported, {fmt(b.learned)} learned, "
                      f"{fmt(b.managed)} quest")
            if len(line) <= SCREEN_CHARS:
                return line
    return line


def how_to_change_line() -> str:
    """The second line: how to allow more. Short enough for the same window, and no full stop inside it."""
    return f"Set {limit_mod.ENV_MAX_CARDS} higher (0 = no limit), or ask the AI to raise it."


def notice_lines(b: Breakdown, *, admitted_over: int = 0) -> List[str]:
    """What the person sees on screen: two lines, the counts and then how to change the limit.

    The whole notice is two lines because the terminal shows a few lines per turn, each cut to one
    sentence; the full text (where the report is, the exact steps) goes to the log (``full_text``).
    """
    return [count_line(b), how_to_change_line()]


def change_instructions(cards_dir: Path) -> List[str]:
    return [
        f"Set {limit_mod.ENV_MAX_CARDS} to the number of cards to allow. 0 (or the word unlimited) means no limit. "
        f"The default is {limit_mod.DEFAULT_MAX_CARDS}.",
        "A lane reads its environment from three places, strongest first: the process environment (the "
        "service unit's Environment= or EnvironmentFile= lines), the .env files named in env_files in "
        "the lane's qar.toml, and the [env] table of that qar.toml (plain defaults). In a terminal chat "
        f"it is the shell environment. Put {limit_mod.ENV_MAX_CARDS}=<number> in whichever of those the "
        "lane uses, then restart the lane (for a systemd lane: systemctl --user restart "
        "<the lane's service>); the new limit applies from the next start.",
        "Or ask the AI that runs the lane: raise the QAR card limit to <number>. It should make that "
        "change where this lane reads its environment and restart it.",
        f"Cards the limit moved out are in {cards_dir / QUARANTINE_DIR_NAME / limit_mod.QUARANTINE_REASON} "
        f"(one line per card in {limit_mod.MANIFEST_NAME}). Move a file back into {cards_dir} to put it back.",
        f"A full store can also be tidied: quest-ai-runner cards tidy --cards-dir {cards_dir} lists what is "
        "provably a copy of another card (a dry run; nothing moves without --apply, and a card with notes, "
        "usage, quest tags or a hand-written origin never moves).",
    ]


def full_text(b: Breakdown, state: limit_mod.LimitState, cards_dir: Path) -> str:
    """The notice in full, for the log and for an AI asked to change the limit. Not shown on screen."""
    biggest = ", ".join(f"{short(name)} {n:,}" for name, n in b.topic_by_folder[:SCREEN_FOLDERS])
    holds = (f"It holds {b.topic:,} topic cards" + (f" (the biggest folders: {biggest})" if biggest else "")
             + f", {b.imported:,} imported from other stores, {b.learned:,} learned from runs and "
               f"{b.managed:,} from quests. New topic and imported cards are not being added.")
    if state.admitted_over:
        holds += f" {state.admitted_over:,} learned or quest cards were kept above the limit."
    lines = [count_line(b), holds, how_to_change_line(),
             f"Card store: {cards_dir}. Full list: {cards_dir / REPORT_NAME}, or run: "
             f"quest-ai-runner cards report --cards-dir {cards_dir}"]
    lines.extend(change_instructions(cards_dir))
    return "\n".join(lines)


def render_report(b: Breakdown, state: limit_mod.LimitState, cards_dir: Path) -> str:
    out: List[str] = [
        "# Card limit report", "",
        f"Store: {cards_dir}",
        f"Written: {card_origin.now_iso()}", "",
        "## Where the store stands", "",
        f"- Limit: {b.limit:,} cards ({limit_mod.ENV_MAX_CARDS})" if b.limit else
        f"- Limit: none ({limit_mod.ENV_MAX_CARDS} is 0 or unlimited)",
        f"- Cards in the index: {b.total:,}" + (" (over the limit; nothing is removed to fit it)"
                                                 if b.limit and b.total > b.limit else ""),
        f"- Moved out of the index (quarantine, not counted): {sum(b.quarantined.values()):,}", "",
        "## What is in it", "",
        "| Kind | Cards |", "| --- | ---: |",
        f"| Topic cards (written from the corpus) | {b.topic:,} |",
        f"| Imported from other stores | {b.imported:,} |",
        f"| Learned from runs | {b.learned:,} |",
        f"| From quests (managed) | {b.managed:,} |", "",
        "### Topic cards by top-level folder", "",
    ]
    out += ["| Folder | Cards |", "| --- | ---: |"] + [
        f"| {name} | {n:,} |" for name, n in b.topic_by_folder[:REPORT_FOLDERS]] or ["none"]
    if len(b.topic_by_folder) > REPORT_FOLDERS:
        out.append(f"| ... {len(b.topic_by_folder) - REPORT_FOLDERS} more folders | |")
    out += ["", "### Imported cards by source store (its root, relative to this store's)", ""]
    out += (["| Source | Cards |", "| --- | ---: |"]
            + [f"| {name} | {n:,} |" for name, n in b.imported_by_source[:REPORT_FOLDERS]]
            if b.imported_by_source else ["none"])
    out += ["", "### Moved out of the index, by reason", ""]
    out += (["| Reason | Cards |", "| --- | ---: |"] + [f"| {r} | {n:,} |" for r, n in sorted(b.quarantined.items())]
            if b.quarantined else ["none"])
    out += ["", "## Since the limit was reached", ""]
    out += [
        f"- First reached: {state.reached_at or 'not yet'}",
        f"- New topic or imported cards waiting for room (latest pass): {state.waiting:,}",
        f"- Files left without a card because there was no room (latest pass): {state.unindexed_files:,}",
        f"- Derived cards moved out to make room for a learned or quest card: {state.evicted:,}",
        f"- Learned or quest cards kept above the limit (nothing derived could be moved): {state.admitted_over:,}",
        "", "## Coldest derived cards (the first to move out if a learned card needs room)", ""]
    out += (["| Card | Kind | Times used | Last touched | Created |", "| --- | --- | ---: | --- | --- |"]
            + [f"| {c['id']} | {c['kind']} | {c['used']} | {c['touched']} | {c['created']} |" for c in b.coldest]
            if b.coldest else ["none"])
    out += ["", "## Change the limit", ""] + [f"{i}. {text}" for i, text in enumerate(change_instructions(cards_dir), 1)]
    return "\n".join(out) + "\n"


def write_report(cards_dir: Path, text: str) -> bool:
    try:
        cards_dir.mkdir(parents=True, exist_ok=True)
        (cards_dir / REPORT_NAME).write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


def announce_full(guard: Any, *, at_open: bool = False, notify: Optional[Callable[[str], None]] = None) -> None:
    """Write the report file and say it: two lines on screen, everything in the log, and on the next task's feed.

    On screen (a terminal chat): two warning lines, the counts and how to change the limit. In the log: the
    whole text with the report's path and the exact steps. Where there is a startup callback (``notify``) it
    is given the same two lines. A lane has no screen, so the same two lines are also held for the next task's
    progress feed (``take_task_notes``), which is where a person watching that lane will read them.
    """
    b = breakdown(guard.snapshot(), guard.cards_dir, guard.limit)
    write_report(guard.cards_dir, render_report(b, guard.state, guard.cards_dir))
    lines = notice_lines(b, admitted_over=guard.state.admitted_over)
    for line in lines:
        log.warning("%s", line)
        if notify is not None:
            try:
                notify(line)
            except Exception:  # noqa: BLE001 -- a broken callback must not stop the notice
                log.debug("card limit notice callback failed", exc_info=True)
    # The whole text, for the log file and for an AI asked to change the limit; not repeated on screen.
    log.warning("%s", full_text(b, guard.state, guard.cards_dir), extra={"in_transcript": False})
    limit_mod.hold_task_note(str(guard.cards_dir), " ".join(lines))


def report_for(cards_dir: str, limit: int) -> str:
    """The report for a store on disk, computed now (what ``quest-ai-runner cards report`` prints)."""
    base = Path(cards_dir)
    cards = FilesystemCardRepository(str(base)).load_all()
    b = breakdown(cards, base, limit)
    guard_state = limit_mod.LimitState()
    try:
        import json
        data = json.loads((base / limit_mod.STATE_FILE).read_text(encoding="utf-8"))
        guard_state = limit_mod.LimitState(**{k: v for k, v in data.items()
                                              if k in limit_mod.LimitState.__dataclass_fields__})
    except Exception:  # noqa: BLE001
        pass
    return render_report(b, guard_state, base)
