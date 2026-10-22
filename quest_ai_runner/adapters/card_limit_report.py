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
from typing import Any, Dict, List, Tuple

from . import card_limit as limit_mod
from . import card_origin
from .card_repository import QUARANTINE_DIR_NAME, FilesystemCardRepository
from .context_card_ceilings import card_folder_key

log = logging.getLogger("quest-ai-runner.card-limit")

REPORT_NAME = "card_limit_report.md"
SCREEN_FOLDERS = 3          # folders named in the on-screen line
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


def notice_lines(b: Breakdown, *, admitted_over: int = 0) -> List[str]:
    """The three short lines the person sees. Each is one sentence, so the terminal shows it whole."""
    if b.total > b.limit:
        first = (f"Your context store holds {b.total:,} cards, over its limit of {b.limit:,}, "
                 "so new topic cards are not being added.")
    else:
        first = f"Your context store is full at {b.limit:,} cards, so new topic cards are not being added."
    biggest = ", ".join(f"{short(name)} {n:,}" for name, n in b.topic_by_folder[:SCREEN_FOLDERS])
    topic = f"{b.topic:,} topic cards" + (f" (the biggest folders: {biggest})" if biggest else "")
    second = (f"It holds {topic}, {b.imported:,} imported from other stores, {b.learned:,} learned from runs, "
              f"{b.managed:,} from quests.")
    if admitted_over:
        second = second.rstrip(".") + f", and {admitted_over:,} learned cards were kept above the limit."
    third = (f"To allow more, set {limit_mod.ENV_MAX_CARDS} to a bigger number, or ask the AI: "
             f"raise the QAR card limit to {max(2 * b.limit, 2000)}.")
    return [first, second, third]


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
        "A full store can also be tidied: python scripts/quarantine_cards.py --cards-dir "
        f"{cards_dir} shows what is provably redundant (a dry run; nothing moves without --apply).",
    ]


def full_text(b: Breakdown, state: limit_mod.LimitState, cards_dir: Path) -> str:
    lines = notice_lines(b, admitted_over=state.admitted_over)
    lines.append(f"Card store: {cards_dir}. Full list: {cards_dir / REPORT_NAME}, or run: "
                 f"quest-ai-runner cards report --cards-dir {cards_dir}")
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


def announce_full(guard: Any, *, at_open: bool = False) -> None:
    """Write the report file and say it on screen (three short lines) and in the log (all of it)."""
    b = breakdown(guard.snapshot(), guard.cards_dir, guard.limit)
    write_report(guard.cards_dir, render_report(b, guard.state, guard.cards_dir))
    for line in notice_lines(b, admitted_over=guard.state.admitted_over):
        log.warning("%s", line)
    # The whole text, for the log file and for an AI asked to change the limit; not repeated on screen.
    log.warning("%s", full_text(b, guard.state, guard.cards_dir), extra={"in_transcript": False})


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
