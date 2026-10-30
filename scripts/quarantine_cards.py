#!/usr/bin/env python3
"""Take inflated cards OUT of a context store, by moving them, never deleting them.

A card store is meant to hold hundreds of cards for a corpus of tens of thousands of files. Two
real stores reached 8,557 and 32,070. The bootstrap gates in
``quest_ai_runner/adapters/file_context_store.py`` stop that happening again; this tool cleans up
what already happened, for the ways it happened:

  item-id         a card stored under a vector point id (``card:<id>``), a second card next to the one the
                  edit was meant for. Its notes are folded into the card it names before it is moved; when
                  its name, description or summary differs from that card's it is left and counted.
  compounded      a card imported from another store through a chain of import hops, so the same
                  card exists several times over under ids like
                  ``a--root-xxxx--root-yyyy--topic`` or with a namespace segment repeated. Two
                  stores sitting inside one another re-imported each other's imports. An id with
                  two or more import-namespace prefixes (``<words>-<6 hex>`` segments before the
                  topic) is compounded; one such prefix is a normal single-hop import.
                  A lane in copy mode reads only its own store, so a compounded card is moved
                  only when a SURVIVING equivalent stays in the same store: another card, not
                  itself moved in this run and not a compounded candidate, whose file set equals
                  or contains this one's. The survivors are judged after every other reason is
                  planned, so a card whose equivalent is itself being moved (excluded, surplus,
                  thin) is kept too. With no survivor the compounded card is the only copy and
                  stays ("compounded card kept: its only copy in this store"). A candidate that
                  the excluded or thin rule moves anyway is still moved under that rule.
  excluded        a card with MORE THAN HALF of its files in folders the folder review now
                  excludes for good (a nested dependency repository, a duplicate tree, a
                  generated-data subtree). The cards are summaries of content that is not this
                  corpus's knowledge.
  surplus         a card covering the same FILES as another card. Identical path sets, or an
                  overlap at or above the dedup threshold. One card is kept, ranked by most used,
                  then best match between its name/keywords and its file and folder names, then
                  earliest created, then id. The manifest detail names the keeper.
  thin            a card whose single file is generated data (``.json``/``.yaml``/``*state.json``,
                  a lock file, ``package.json``, a local cache). One data file is not a topic.
  covered         a card whose file set is identical to, or inside, a card held by ANOTHER store
                  named with ``--covered-by DIR`` (e.g. a shared store the lane reads in place).
                  Not run by default: name it in ``--reasons`` and give at least one
                  ``--covered-by``. ``--covered-prefix-strip PATH`` drops a leading folder from
                  paths before comparing, when one store is rooted a level above the other.

The plan itself (every reason above) is ``plan_cards`` in ``hubofhubs/recall/card_store_tidy_plan.py``,
the one the runner makes for any card repository (a local folder, the Quest backend, Qdrant) and applies through the
repository port (``card_store_tidy``); which cards are protected is ``card_origin.protection``. This tool is that plan
over a local folder, by hand, and a way of looking at a store: a dry run starts with the store's health (cards by kind, ids that stack import prefixes,
cards pinning the same files, the corpus's folders) and shows, per reason, how many of the cards it would move
were created in the last 14 days.

NOTHING IS DELETED. Every card moves to ``<cards_dir>/_quarantine/<reason>/`` and the card store
ignores that directory, so the index shrinks and a wrong call is undone by moving files back. The
run is a DRY RUN unless ``--apply`` is passed, and it always writes a manifest naming every card
it moved or would move.

Cards it refuses to touch, because they are not inflation (one definition, ``card_origin.protection``,
shared with the store's own tidy and its card limit):
  * a card carrying ``managed_by`` (written and owned by another system, e.g. quest folder sync),
  * a card with ``usage_count`` above zero, notes or edits in ``content``, quest ``scope_tags``,
    conventions, or provenance other than the bootstrap (a card learned from a run, or written by a
    person with no provenance at all).
The one exception is a compounded copy (provable from the id alone) whose survivor stays in the store:
it moves only after everything it holds joins that survivor without loss, and if that is not possible it
stays and is counted under "left in place on purpose". A card stored under a vector point id is handled the
same way (its notes join the card it names, or it stays when they cannot).

Examples (paths are arguments; this script hardcodes none):

    python3 scripts/quarantine_cards.py --cards-dir /path/to/.quest-context
    python3 scripts/quarantine_cards.py --cards-dir /path/to/.quest-context --reasons compounded
    python3 scripts/quarantine_cards.py --cards-dir /path/to/.quest-context --apply
    python3 scripts/quarantine_cards.py --cards-dir /path/to/.quest-context \\
        --reasons covered --covered-by /path/to/shared/.quest-context
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from hubofhubs.recall.card_origin import card_id_of_item, compounding_evidence  # noqa: E402,F401
from hubofhubs.recall.card_repository import QUARANTINE_DIR_NAME  # noqa: E402
from hubofhubs.recall.card_store_hygiene import (  # noqa: E402,F401  (re-exported)
    card_paths,
    fold_card,
    jaccard,
    join_onto,
    keeper_rank,
    Move,
    move_to,
    name_match_score,
    name_tokens,
    plan_stray,
    protection_reason as is_protected,
    read_card,
    render_health_report,
    settle_move,
    store_id_of,
)
from hubofhubs.recall.card_store_lock import StoreLock  # noqa: E402
from quest_ai_runner.adapters.card_store_settings import max_cards_per_folder, skip_dirs_from_env  # noqa: E402
from hubofhubs.recall.card_store_hygiene import in_quarantine  # noqa: E402
from hubofhubs.recall.card_store_tidy_plan import (  # noqa: E402,F401  (re-exported)
    ALL_REASONS,
    DEFAULT_REASONS,
    excluded_prefixes_from_review,
    plan_cards,
    strip_prefix,
    under_any_prefix,
)
from hubofhubs.recall.context_card_ceilings import FILE_SET_DEDUP_JACCARD  # noqa: E402

SKIP_FILES = {"bootstrap_meta.json", "folder_review.json"}


# ---------------------------------------------------------------------------
# Reading the store
# ---------------------------------------------------------------------------
def load_cards(cards_dir: Path) -> List[Tuple[Path, Dict[str, Any]]]:
    """``[(path, card)]`` for every card file directly in ``cards_dir``. Never raises."""
    out: List[Tuple[Path, Dict[str, Any]]] = []
    for entry in sorted(cards_dir.iterdir()):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        if entry.name.startswith(".") or entry.name in SKIP_FILES:
            continue
        try:
            card = json.loads(entry.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- a corrupt card is left exactly where it is
            continue
        if isinstance(card, dict):
            out.append((entry, card))
    return out


# ---------------------------------------------------------------------------
# The plan: the library's (card_store_tidy_plan.plan_cards), over this folder's files
# ---------------------------------------------------------------------------
def plan(
    cards: List[Tuple[Path, Dict[str, Any]]],
    *,
    reasons: Set[str],
    excluded: List[str],
    threshold: float,
    covered_by: Optional[List[List[Tuple[Path, Dict[str, Any]]]]] = None,
    strip_prefixes: Optional[List[str]] = None,
    own_store_id: str = "",
    distinct_topics: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Decide what moves. Returns ``(entries, skipped_counts)``; mutates nothing on disk.

    The rules are ``plan_cards`` in the library, the same plan the runner makes for any repository; this keys
    the cards by file name and names each entry's file (and the file a protected copy joins) for the mover."""
    path_of = {path.stem: path for path, _ in cards}
    by_key = {path.stem: card for path, card in cards}
    cards_dir = cards[0][0].parent if cards else Path(".")
    entries, skipped = plan_cards(
        by_key, reasons=reasons, excluded=excluded, threshold=threshold,
        covered_by=[{p.stem: c for p, c in other} for other in (covered_by or [])],
        strip_prefixes=strip_prefixes, own_store_id=own_store_id,
        quarantined=lambda key: in_quarantine(cards_dir, key), distinct_topics=distinct_topics)
    for entry in entries:
        entry["file"] = path_of[entry["key"]].name
        if entry.get("joins"):
            entry["joins"] = path_of[entry["joins"]].name
    return entries, skipped


# ---------------------------------------------------------------------------
# Reporting and moving
# ---------------------------------------------------------------------------
def print_report(cards_dir: Path, total: int, entries: List[Dict[str, Any]],
                 skipped: Dict[str, int], excluded: List[str], applied: bool) -> None:
    by_reason: Dict[str, int] = {}
    for entry in entries:
        by_reason[entry["reason"]] = by_reason.get(entry["reason"], 0) + 1
    print(f"cards dir       : {cards_dir}")
    print(f"cards found     : {total}")
    print(f"folder review   : {len(excluded)} excluded folder(s) read from folder_review.json")
    print(f"mode            : {'APPLY (cards are moved)' if applied else 'DRY RUN (nothing moves)'}")
    print("")
    print("to quarantine:")
    for reason in ALL_REASONS:
        if by_reason.get(reason):
            print(f"  {reason:<12} {by_reason[reason]:>8}")
    print(f"  {'TOTAL':<12} {len(entries):>8}")
    print(f"  {'remaining':<12} {total - len(entries):>8}")
    if skipped:
        print("")
        print("left in place on purpose:")
        for why, count in sorted(skipped.items()):
            print(f"  {count:>8}  {why}")
    print("")
    print("sample (up to 3 per reason):")
    for reason in ALL_REASONS:
        shown = 0
        for entry in entries:
            if entry["reason"] != reason or shown >= 3:
                continue
            print(f"  [{reason}] {entry['card_id'][:90]}  ({entry['detail']})")
            shown += 1


def write_manifest(target: Path, cards_dir: Path, total: int, entries: List[Dict[str, Any]],
                   skipped: Dict[str, int], excluded: List[str], applied: bool) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cards_dir": str(cards_dir),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "applied": applied,
        "cards_found": total,
        "cards_quarantined": len(entries),
        "cards_remaining": total - len(entries),
        "skipped": skipped,
        "excluded_folders": excluded,
        "entries": entries,
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return target


def move_entries(cards_dir: Path, entries: List[Dict[str, Any]], own_store_id: str = "") -> Tuple[int, int]:
    """Move each planned card into ``_quarantine/<reason>/``. Returns ``(moved, failed)``.

    One card at a time under the folder's lock, and each card is read again first: a card that gained a
    note, usage or quest tags since the plan was made stays where it is (and says so in its entry), and a
    card another process already moved is not counted. What a protected compounded copy holds is joined
    onto its survivor before it moves.
    """
    moved = 0
    failed = 0
    lock = StoreLock.for_dir(str(cards_dir))
    for entry in entries:
        source = cards_dir / entry["file"]
        reason = entry["reason"]
        with lock:
            card = read_card(source)
            if card is None:
                entry["error"] = "gone: already moved or removed by somebody else"
                continue
            why = ""
            joined: Dict[str, Any] = {}
            keeper = ""
            if reason == "item-id":
                settled = settle_move(cards_dir, Move(source, source.stem, reason, entry["detail"]), own_store_id)
                destination, why, joined, keeper = settled.moved_to, settled.why, settled.joined, settled.keeper
            else:
                protection = is_protected(card)
                if protection and not (reason == "compounded" and entry.get("joins")):
                    why = f"left in place, it gained something since the plan: {protection}"
                elif protection:
                    why, joined = join_onto(cards_dir, card, cards_dir / entry["joins"])
                    keeper = Path(entry["joins"]).stem
                destination = None if why else move_to(cards_dir, source, reason)
                if destination is None and not why:
                    why = "could not be moved"
            if destination is None:
                entry["error"] = why
                failed += 1
                continue
            lock.bump()
            entry["moved_to"] = str(destination.relative_to(cards_dir))
            if keeper:
                entry["keeper"], entry["joined"] = keeper, joined
            moved += 1
    return moved, failed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Move inflated cards out of a context store (dry run by default).",
    )
    parser.add_argument("--cards-dir", required=True,
                        help="the card store directory (e.g. <corpus>/.quest-context)")
    parser.add_argument("--apply", action="store_true",
                        help="actually move the cards; without this nothing is touched")
    parser.add_argument("--reasons", default=",".join(DEFAULT_REASONS),
                        help=f"comma-separated subset of: {', '.join(ALL_REASONS)} "
                             f"(default: {', '.join(DEFAULT_REASONS)}; covered needs --covered-by)")
    parser.add_argument("--covered-by", action="append", default=[], metavar="DIR",
                        help="another card store directory whose cards may cover this store's "
                             "cards, for the covered reason (repeatable)")
    parser.add_argument("--covered-prefix-strip", action="append", default=[], metavar="PATH",
                        help="leading folder to strip from card paths before comparing for the "
                             "covered reason (repeatable)")
    parser.add_argument("--excluded-prefix", action="append", default=[], metavar="PATH",
                        help="extra corpus-relative folder to treat as excluded (repeatable)")
    parser.add_argument("--keep-prefix", action="append", default=[], metavar="PATH",
                        help="corpus-relative folder whose cards the folder review excludes but "
                             "that you want kept (repeatable); an --excluded-prefix beneath it still applies")
    parser.add_argument("--jaccard", type=float, default=FILE_SET_DEDUP_JACCARD,
                        help="file-set overlap at which two cards count as the same card")
    parser.add_argument("--manifest", default=None,
                        help="where to write the manifest (default: inside the quarantine dir)")
    parser.add_argument("--root", default=None, metavar="DIR",
                        help="the corpus root this store indexes, to count its folders in the health "
                             "table (default: the root the store recorded)")
    args = parser.parse_args(argv)

    cards_dir = Path(os.path.expanduser(args.cards_dir)).resolve()
    if not cards_dir.is_dir():
        print(f"error: {cards_dir} is not a directory", file=sys.stderr)
        return 2

    reasons = {r.strip() for r in args.reasons.split(",") if r.strip()}
    unknown = reasons - set(ALL_REASONS)
    if unknown:
        print(f"error: unknown reason(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    covered_stores: List[List[Tuple[Path, Dict[str, Any]]]] = []
    if "covered" in reasons:
        if not args.covered_by:
            print("error: the covered reason needs at least one --covered-by DIR", file=sys.stderr)
            return 2
        for raw in args.covered_by:
            other = Path(os.path.expanduser(raw)).resolve()
            if not other.is_dir() or other == cards_dir:
                print(f"error: --covered-by {other} must be another existing directory",
                      file=sys.stderr)
                return 2
            covered_stores.append(load_cards(other))

    cards = load_cards(cards_dir)
    keep = [k.rstrip("/") for k in args.keep_prefix]
    reviewed = [e for e in excluded_prefixes_from_review(cards_dir)
                if not under_any_prefix(e, keep)]
    excluded = reviewed + list(args.excluded_prefix)
    own_store_id = store_id_of(cards_dir)
    entries, skipped = plan(cards, reasons=reasons, excluded=excluded, threshold=args.jaccard,
                           covered_by=covered_stores, strip_prefixes=args.covered_prefix_strip,
                           own_store_id=own_store_id)

    print_report(cards_dir, len(cards), entries, skipped, excluded, args.apply)
    print("")
    print(render_health_report(cards_dir, entries, per_folder=max_cards_per_folder(), skip_extra=skip_dirs_from_env(),
                               root=Path(os.path.expanduser(args.root)).resolve() if args.root else None))

    if args.apply and entries:
        moved, failed = move_entries(cards_dir, entries, own_store_id)
        print("")
        print(f"moved {moved} card(s) into {cards_dir / QUARANTINE_DIR_NAME}"
              + (f"; {failed} could not be moved" if failed else ""))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    default_name = f"{stamp}-{os.getpid()}-{'apply' if args.apply else 'dryrun'}-manifest.json"
    manifest_path = (Path(os.path.expanduser(args.manifest)) if args.manifest
                     else cards_dir / QUARANTINE_DIR_NAME / default_name)
    try:
        written = write_manifest(manifest_path, cards_dir, len(cards), entries, skipped,
                                excluded, args.apply)
        print(f"manifest: {written}")
    except Exception as exc:  # noqa: BLE001
        print(f"warning: could not write the manifest: {exc}", file=sys.stderr)
    if not args.apply:
        print("dry run: nothing was moved. Re-run with --apply to move these cards.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
