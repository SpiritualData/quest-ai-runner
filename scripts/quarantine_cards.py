#!/usr/bin/env python3
"""Take inflated cards OUT of a context store, by moving them, never deleting them.

A card store is meant to hold hundreds of cards for a corpus of tens of thousands of files. Two
real stores reached 8,557 and 32,070. The bootstrap gates in
``quest_ai_runner/adapters/file_context_store.py`` stop that happening again; this tool cleans up
what already happened, for the ways it happened:

  item-id         a card stored under a vector point id (``card:<id>``), a second card next to the one the
                  edit was meant for. Its notes are folded into the card it names before it is moved.
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

The classification of what is PROVABLY a copy (compounded ids, cards pinning the same files) and which cards
are protected lives in ``quest_ai_runner/adapters/card_store_hygiene.py`` and ``card_origin.py``; the runner's
own store applies the provable part when it opens, so this tool is for the judgement calls above and for
looking at a store: a dry run starts with the store's health (cards by kind, ids that stack import prefixes,
cards pinning the same files, the corpus's folders) and shows, per reason, how many of the cards it would move
were created in the last 14 days.

NOTHING IS DELETED. Every card moves to ``<cards_dir>/_quarantine/<reason>/`` and the card store
ignores that directory, so the index shrinks and a wrong call is undone by moving files back. The
run is a DRY RUN unless ``--apply`` is passed, and it always writes a manifest naming every card
it moved or would move.

Cards it refuses to touch, because they are not inflation:
  * a card carrying ``managed_by`` (written and owned by another system, e.g. quest folder sync),
  * a card with ``usage_count`` above zero, or provenance other than the bootstrap,
    i.e. a hand-learned card. Those are only ever moved under the ``compounded`` reason, which is
    provable from the id alone: a compounded card is a COPY, so the original stays,
    provided a surviving equivalent exists in the same store.

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
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quest_ai_runner.adapters.card_origin import card_id_of_item, compounding_evidence  # noqa: E402,F401
from quest_ai_runner.adapters.card_repository import QUARANTINE_DIR_NAME  # noqa: E402
from quest_ai_runner.adapters.card_store_hygiene import (  # noqa: E402,F401  (re-exported)
    card_paths,
    fold_item_card,
    jaccard,
    keeper_rank,
    name_match_score,
    name_tokens,
    protection_reason as is_protected,
    render_health_report,
)
from quest_ai_runner.adapters.file_context_store import (  # noqa: E402
    _FILE_SET_DEDUP_JACCARD,
    _has_kept_child,
    card_is_too_thin,
    path_is_data_file,
    verdict_is_final,
)

ALL_REASONS = ("item-id", "compounded", "excluded", "surplus", "thin", "covered")
DEFAULT_REASONS = ("item-id", "compounded", "excluded", "surplus", "thin")
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


def excluded_prefixes_from_review(cards_dir: Path) -> List[str]:
    """Folders the cached folder review excludes, read with the CURRENT (fixed) rules.

    The cache file is data, not a decision: it holds a verdict per folder, and which of those
    verdicts actually excludes a folder is decided here by the same rule the bootstrap now uses.
    A final verdict (a nested repository, a duplicate tree) stands whatever its children say; only
    a model skip that the model itself called "mixed" is superseded by its kept children.
    """
    path = cards_dir / "folder_review.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    verdicts = loaded.get("folders") if isinstance(loaded, dict) else None
    if not isinstance(verdicts, dict):
        return []
    expanded = {"/".join(f.split("/")[:-1]) for f in verdicts if "/" in f}
    excluded: List[str] = []
    for folder, verdict in verdicts.items():
        if not isinstance(verdict, dict) or verdict.get("index", True):
            continue
        superseded = (
            not verdict_is_final(verdict)
            and bool(verdict.get("mixed"))
            and folder in expanded
            and _has_kept_child(verdicts, folder)
        )
        if not superseded:
            excluded.append(folder)
    return sorted(excluded)


def under_any_prefix(rel: str, prefixes: List[str]) -> Optional[str]:
    """The first prefix ``rel`` sits under, or None."""
    for prefix in prefixes:
        if not prefix or prefix == ".":
            continue
        if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
            return prefix
    return None


# ---------------------------------------------------------------------------
# The four reasons
# ---------------------------------------------------------------------------
def strip_prefix(rel: str, prefixes: List[str]) -> str:
    """``rel`` without the first leading prefix folder it sits under."""
    for prefix in prefixes:
        head = prefix.strip("/")
        if head and rel.startswith(head + "/"):
            return rel[len(head) + 1:]
    return rel


def plan(
    cards: List[Tuple[Path, Dict[str, Any]]],
    *,
    reasons: Set[str],
    excluded: List[str],
    threshold: float,
    covered_by: Optional[List[List[Tuple[Path, Dict[str, Any]]]]] = None,
    strip_prefixes: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Decide what moves. Returns ``(entries, skipped_counts)``; mutates nothing on disk."""
    entries: List[Dict[str, Any]] = []
    decided: Set[Path] = set()
    moved_reason: Dict[Path, str] = {}
    keeper_of: Dict[Path, Path] = {}
    skipped: Dict[str, int] = {}

    def record(path: Path, card: Dict[str, Any], reason: str, detail: str) -> None:
        entries.append({
            "reason": reason,
            "detail": detail,
            "card_id": str(card.get("id") or path.stem),
            "file": path.name,
            "files": len(card_paths(card)),
            "usage_count": card.get("usage_count", 0),
            "created_at": str((card.get("provenance") or {}).get("created_at") or "")
            if isinstance(card.get("provenance"), dict) else "",
        })
        decided.add(path)
        moved_reason[path] = reason

    def note_skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    # 0. Cards stored under a vector point id. Provable from the id; their notes are folded into the card
    #    they name when the move is applied, so nothing a run taught is lost.
    if "item-id" in reasons:
        for path, card in cards:
            card_id = str(card.get("id") or path.stem)
            if card_id.startswith("card:"):
                if card.get("managed_by"):
                    note_skip("vector-point-id card kept: managed by another system")
                    continue
                record(path, card, "item-id", f"stored under a vector point id; the card is {card_id_of_item(card_id)}")

    # 1. Compounded candidates. Provable from the id alone and overriding protection, but only
    #    MOVED in step 6, once the other reasons are planned and the survivors are known. Until
    #    then a candidate stays out of surplus and covered, and may still be taken by excluded
    #    or thin (without the protection check, as compounded always overrode it).
    pending: Dict[Path, str] = {}
    if "compounded" in reasons:
        for path, card in cards:
            evidence = compounding_evidence(str(card.get("id") or path.stem))
            if not evidence:
                continue
            if card.get("managed_by"):
                note_skip("compounded but managed by another system")
                continue
            pending[path] = evidence

    # 2. Cards mostly inside a folder the review excludes for good.
    if "excluded" in reasons and excluded:
        for path, card in cards:
            if path in decided:
                continue
            paths = card_paths(card)
            if not paths:
                continue
            hits = [under_any_prefix(p, excluded) for p in paths]
            inside = [h for h in hits if h]
            if len(inside) * 2 <= len(paths):
                continue
            protection = "" if path in pending else is_protected(card)
            if protection:
                note_skip(f"excluded-folder card kept: {protection}")
                continue
            record(path, card, "excluded",
                   f"{len(inside)} of {len(paths)} file(s) under excluded folders, e.g. {inside[0]}")

    # 3. Surplus cards covering the same file set as a card we keep.
    if "surplus" in reasons:
        groups: List[Dict[str, Any]] = []
        candidates = [(i, path, card, set(card_paths(card)))
                      for i, (path, card) in enumerate(cards)
                      if path not in decided and path not in pending and card_paths(card)]
        # Inverted path -> group index: an overlap at or above any positive threshold needs at
        # least one shared file, so comparing only the groups that share a path is exact, and it
        # keeps a 32,000-card store from becoming a quadratic scan.
        group_of_path: Dict[str, Set[int]] = {}
        for index, path, card, files in candidates:
            candidate_indexes: Set[int] = set()
            for rel in files:
                candidate_indexes |= group_of_path.get(rel, set())
            target: Optional[int] = None
            for gi in sorted(candidate_indexes):
                group = groups[gi]
                if files == group["files"] or jaccard(files, group["files"]) >= threshold:
                    target = gi
                    break
            if target is None:
                target = len(groups)
                groups.append({"members": [], "files": set()})
            groups[target]["members"].append((index, path, card))
            groups[target]["files"] |= files
            for rel in groups[target]["files"]:
                group_of_path.setdefault(rel, set()).add(target)
        for group in groups:
            members = group["members"]
            if len(members) < 2:
                continue
            ordered = sorted(members, key=lambda m: keeper_rank(m[2], m[1].stem))
            keeper_id = str(ordered[0][2].get("id") or ordered[0][1].stem)
            keeper_path = ordered[0][1]
            for _, path, card in ordered[1:]:
                protection = is_protected(card)
                if protection:
                    note_skip(f"duplicate card kept: {protection}")
                    continue
                record(path, card, "surplus", f"same files as {keeper_id}")
                keeper_of[path] = keeper_path

    # 4. Single generated-data-file cards.
    if "thin" in reasons:
        covered_by_kept: Set[str] = set()
        for path, card in cards:
            if path in decided or path in pending:
                continue
            paths = card_paths(card)
            if len(paths) >= 2:
                covered_by_kept.update(paths)
        for path, card in cards:
            if path in decided:
                continue
            paths = card_paths(card)
            if len(paths) != 1 or not path_is_data_file(paths[0]):
                continue
            protection = "" if path in pending else is_protected(card)
            if protection:
                note_skip(f"single data-file card kept: {protection}")
                continue
            record(path, card, "thin", card_is_too_thin(card, covered_by_kept)
                   or "single generated/data file")

    # 5. Cards whose files another store already holds (identical or a superset).
    if "covered" in reasons and covered_by:
        strips = list(strip_prefixes or [])

        def normalized(card: Dict[str, Any]) -> Set[str]:
            return {strip_prefix(p, strips) for p in card_paths(card)}

        for number, other_cards in enumerate(covered_by, start=1):
            holders: Dict[str, Set[int]] = {}
            other_sets: List[Set[str]] = []
            other_ids: List[str] = []
            for other_path, other_card in other_cards:
                files = normalized(other_card)
                other_sets.append(files)
                other_ids.append(str(other_card.get("id") or other_path.stem))
                for rel in files:
                    holders.setdefault(rel, set()).add(len(other_sets) - 1)
            for path, card in cards:
                if path in decided or path in pending:
                    continue
                files = normalized(card)
                if not files:
                    continue
                matches: Optional[Set[int]] = None
                for rel in files:
                    matches = set(holders.get(rel, set())) if matches is None \
                        else matches & holders.get(rel, set())
                    if not matches:
                        break
                if not matches:
                    continue
                protection = is_protected(card)
                if protection:
                    note_skip(f"covered card kept: {protection}")
                    continue
                index = min(matches)
                kind = "same files as" if other_sets[index] == files else "files inside those of"
                record(path, card, "covered",
                       f"{kind} {other_ids[index]} in covered-by store {number}")

    # 6. Compounded candidates not taken above: moved only when a surviving equivalent exists,
    #    i.e. another card neither moved in this run nor a candidate, with the same or more files.
    if pending:
        holders_by_path: Dict[str, Set[int]] = {}
        survivors: List[Tuple[str, Set[str]]] = []
        for path, card in cards:
            files = set(card_paths(card))
            if path in decided or path in pending or not files:
                continue
            survivors.append((str(card.get("id") or path.stem), files))
            for rel in files:
                holders_by_path.setdefault(rel, set()).add(len(survivors) - 1)
        for path, card in cards:
            if path in decided or path not in pending:
                continue
            files = set(card_paths(card))
            matches: Optional[Set[int]] = None
            for rel in files:
                matches = set(holders_by_path.get(rel, set())) if matches is None \
                    else matches & holders_by_path.get(rel, set())
                if not matches:
                    break
            if files and not matches:
                note_skip("compounded card kept: its only copy in this store")
                continue
            detail = pending[path]
            if matches:
                detail += f"; equivalent kept: {survivors[min(matches)][0]}"
            record(path, card, "compounded", detail)

    # A surplus keeper can itself be moved by a later reason: say so on the surplus entries.
    for entry_path, keeper_path in keeper_of.items():
        if keeper_path in moved_reason:
            for entry in entries:
                if entry["file"] == entry_path.name and entry["reason"] == "surplus":
                    entry["detail"] += f" ({keeper_path.stem} is also moved: {moved_reason[keeper_path]})"

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


def move_entries(cards_dir: Path, entries: List[Dict[str, Any]]) -> Tuple[int, int]:
    """Move each planned card into ``_quarantine/<reason>/``. Returns ``(moved, failed)``."""
    moved = 0
    failed = 0
    for entry in entries:
        source = cards_dir / entry["file"]
        destination_dir = cards_dir / QUARANTINE_DIR_NAME / entry["reason"]
        if entry["reason"] == "item-id":
            fold_item_card(cards_dir, source)
        try:
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination = destination_dir / source.name
            suffix = 1
            while destination.exists():
                destination = destination_dir / f"{source.stem}.{suffix}.json"
                suffix += 1
            shutil.move(str(source), str(destination))
            entry["moved_to"] = str(destination.relative_to(cards_dir))
            moved += 1
        except Exception as exc:  # noqa: BLE001 -- one stuck file never stops the rest
            entry["error"] = str(exc)
            failed += 1
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
    parser.add_argument("--jaccard", type=float, default=_FILE_SET_DEDUP_JACCARD,
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
    entries, skipped = plan(cards, reasons=reasons, excluded=excluded, threshold=args.jaccard,
                           covered_by=covered_stores, strip_prefixes=args.covered_prefix_strip)

    print_report(cards_dir, len(cards), entries, skipped, excluded, args.apply)
    print("")
    print(render_health_report(cards_dir, entries, root=Path(os.path.expanduser(args.root)).resolve()
                               if args.root else None))

    if args.apply and entries:
        moved, failed = move_entries(cards_dir, entries)
        print("")
        print(f"moved {moved} card(s) into {cards_dir / QUARANTINE_DIR_NAME}"
              + (f"; {failed} could not be moved" if failed else ""))

    stamp = time.strftime("%Y%m%d-%H%M%S")
    default_name = f"{stamp}-{'apply' if args.apply else 'dryrun'}-manifest.json"
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
