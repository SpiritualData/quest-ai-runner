#!/usr/bin/env python3
"""Take inflated cards OUT of a context store, by moving them, never deleting them.

A card store is meant to hold hundreds of cards for a corpus of tens of thousands of files. Two
real stores reached 8,557 and 32,070. The bootstrap gates in
``quest_ai_runner/adapters/file_context_store.py`` stop that happening again; this tool cleans up
what already happened, for the three ways it happened:

  compounded      a card imported from another store through a chain of import hops, so the same
                  card exists several times over under ids like
                  ``a--root-xxxx--root-yyyy--topic`` or with a namespace segment repeated. Two
                  stores sitting inside one another re-imported each other's imports.
  excluded        a card describing files in a folder the folder review now excludes for good (a
                  nested dependency repository, a duplicate tree, a generated-data subtree). The
                  cards are summaries of content that is not this corpus's knowledge.
  surplus         a card covering the same FILES as another card. Identical path sets, or an
                  overlap at or above the dedup threshold. One card is kept: the most used, else
                  the earliest created.
  thin            a card whose single file is generated data (``.json``/``.yaml``/``*state.json``,
                  a lock file, ``package.json``, a local cache). One data file is not a topic.

NOTHING IS DELETED. Every card moves to ``<cards_dir>/_quarantine/<reason>/`` and the card store
ignores that directory, so the index shrinks and a wrong call is undone by moving files back. The
run is a DRY RUN unless ``--apply`` is passed, and it always writes a manifest naming every card
it moved or would move.

Cards it refuses to touch, because they are not inflation:
  * a card carrying ``managed_by`` (written and owned by another system, e.g. quest folder sync),
  * a card with ``usage_count`` above zero, or provenance other than the bootstrap,
    i.e. a hand-learned card. Those are only ever moved under the ``compounded`` reason, which is
    provable from the id alone: a compounded card is a COPY, so the original stays.

Examples (paths are arguments; this script hardcodes none):

    python3 scripts/quarantine_cards.py --cards-dir /path/to/.quest-context
    python3 scripts/quarantine_cards.py --cards-dir /path/to/.quest-context --reasons compounded
    python3 scripts/quarantine_cards.py --cards-dir /path/to/.quest-context --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from quest_ai_runner.adapters.card_repository import QUARANTINE_DIR_NAME  # noqa: E402
from quest_ai_runner.adapters.file_context_store import (  # noqa: E402
    _FILE_SET_DEDUP_JACCARD,
    _has_kept_child,
    card_is_too_thin,
    card_preference_key,
    path_is_data_file,
    verdict_is_final,
)

ALL_REASONS = ("compounded", "excluded", "surplus", "thin")
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


def card_paths(card: Dict[str, Any]) -> List[str]:
    """A card's file entries as plain strings, in either stored shape."""
    out: List[str] = []
    for entry in card.get("files") or []:
        value = entry.get("path", "") if isinstance(entry, dict) else entry
        if value:
            out.append(str(value).replace("\\", "/"))
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
def compounding_evidence(card_id: str) -> str:
    """Why this id is a compounded import copy, or "" when it is not.

    Two shapes, both provable from the id alone. An import namespaces the imported card's id with
    the source root's path, so a chain of imports leaves a chain of namespace segments: a repeated
    segment means the same root appears twice in one id, and an interior ``root-`` hop means a
    store imported another store's own imports. A single leading namespace is a legitimate
    one-level import and is NOT evidence.
    """
    segments = [s for s in str(card_id or "").split("--") if s]
    if len(segments) < 2:
        return ""
    for segment in segments[1:]:
        if segment.startswith("root-"):
            return "interior root- import hop"
    seen: Set[str] = set()
    for segment in segments:
        if segment in seen:
            return f"namespace segment repeated ({segment})"
        seen.add(segment)
    return ""


def is_protected(card: Dict[str, Any]) -> str:
    """Why this card must not be moved, or "" when it may be.

    ``managed_by`` is absolute: another system owns the card and rewrites it from its own source
    of truth, so moving it achieves nothing except breaking that system. Usage and non-bootstrap
    provenance mean a human or a run put something here that no file can regenerate.
    """
    if card.get("managed_by"):
        return "managed by another system"
    try:
        if int(card.get("usage_count") or 0) > 0:
            return "has been used"
    except (TypeError, ValueError):
        pass
    provenance = card.get("provenance")
    created_by = ""
    if isinstance(provenance, dict):
        created_by = str(provenance.get("created_by_task") or "")
    if created_by and created_by != "bootstrap":
        return f"provenance {created_by}"
    return ""


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def plan(
    cards: List[Tuple[Path, Dict[str, Any]]],
    *,
    reasons: Set[str],
    excluded: List[str],
    threshold: float,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Decide what moves. Returns ``(entries, skipped_counts)``; mutates nothing on disk."""
    entries: List[Dict[str, Any]] = []
    decided: Set[Path] = set()
    skipped: Dict[str, int] = {}

    def record(path: Path, card: Dict[str, Any], reason: str, detail: str) -> None:
        entries.append({
            "reason": reason,
            "detail": detail,
            "card_id": str(card.get("id") or path.stem),
            "file": path.name,
            "files": len(card_paths(card)),
            "usage_count": card.get("usage_count", 0),
        })
        decided.add(path)

    def note_skip(why: str) -> None:
        skipped[why] = skipped.get(why, 0) + 1

    # 1. Compounded copies. Decided first and overriding protection: a compounded card is a COPY,
    #    so whatever it carries also exists on the card it was copied from.
    if "compounded" in reasons:
        for path, card in cards:
            evidence = compounding_evidence(str(card.get("id") or path.stem))
            if not evidence:
                continue
            if card.get("managed_by"):
                note_skip("compounded but managed by another system")
                continue
            record(path, card, "compounded", evidence)

    # 2. Cards entirely inside a folder the review excludes for good.
    if "excluded" in reasons and excluded:
        for path, card in cards:
            if path in decided:
                continue
            paths = card_paths(card)
            if not paths:
                continue
            hits = [under_any_prefix(p, excluded) for p in paths]
            if not all(hits):
                continue
            protection = is_protected(card)
            if protection:
                note_skip(f"excluded-folder card kept: {protection}")
                continue
            record(path, card, "excluded", f"all {len(paths)} file(s) under {hits[0]}")

    # 3. Surplus cards covering the same file set as a card we keep.
    if "surplus" in reasons:
        groups: List[Dict[str, Any]] = []
        candidates = [(i, path, card, set(card_paths(card)))
                      for i, (path, card) in enumerate(cards)
                      if path not in decided and card_paths(card)]
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
            ordered = sorted(members, key=lambda m: card_preference_key(m[2], m[0]))
            keeper_id = str(ordered[0][2].get("id") or ordered[0][1].stem)
            for _, path, card in ordered[1:]:
                protection = is_protected(card)
                if protection:
                    note_skip(f"duplicate card kept: {protection}")
                    continue
                record(path, card, "surplus", f"same files as {keeper_id}")

    # 4. Single generated-data-file cards.
    if "thin" in reasons:
        covered_by_kept: Set[str] = set()
        for path, card in cards:
            if path in decided:
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
            protection = is_protected(card)
            if protection:
                note_skip(f"single data-file card kept: {protection}")
                continue
            record(path, card, "thin", card_is_too_thin(card, covered_by_kept)
                   or "single generated/data file")

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
    parser.add_argument("--reasons", default=",".join(ALL_REASONS),
                        help=f"comma-separated subset of: {', '.join(ALL_REASONS)}")
    parser.add_argument("--excluded-prefix", action="append", default=[], metavar="PATH",
                        help="extra corpus-relative folder to treat as excluded (repeatable)")
    parser.add_argument("--jaccard", type=float, default=_FILE_SET_DEDUP_JACCARD,
                        help="file-set overlap at which two cards count as the same card")
    parser.add_argument("--manifest", default=None,
                        help="where to write the manifest (default: inside the quarantine dir)")
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

    cards = load_cards(cards_dir)
    excluded = excluded_prefixes_from_review(cards_dir) + list(args.excluded_prefix)
    entries, skipped = plan(cards, reasons=reasons, excluded=excluded, threshold=args.jaccard)

    print_report(cards_dir, len(cards), entries, skipped, excluded, args.apply)

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
