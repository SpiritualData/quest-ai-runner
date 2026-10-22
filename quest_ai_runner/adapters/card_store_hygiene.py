"""Tidying a card store: what is PROVABLY redundant, moved (never deleted), and a plain account of the store.

Three things are provable from the cards alone, and these are the only ones moved without a person
looking:

  compounded      a copy under an id that stacks import prefixes (``a--root-x--root-y--topic``: see
                  ``card_origin.compounding_evidence``) whose files are pinned, exactly, by another card of
                  the store. It is a copy of that card;
  duplicate-files two cards pinning exactly the same paths are one card. The most used stays, else the
                  earliest, else the first by name;
  item-id         a card stored under a vector point id (``card:<id>``): the card it was meant for is
                  ``<id>``. Its notes are folded into that card, and the stray one is moved.

Judgement calls (a folder the review excludes, a card overlapping another by most of its files, a card
about one generated file) stay with ``scripts/quarantine_cards.py``, which a person runs and reads.

A card is never touched when another system owns it (``managed_by``), has been used, or was learned from
a run (any ``created_by_task`` but ``bootstrap``), unless it is a compounded copy whose notes the card it
copies already has. Cards move to ``<store>/_quarantine/<reason>/`` and the index ignores that folder, so
moving the files back undoes it. A manifest of the move is written beside them.

The scan is streamed, one card at a time, keeping only a fingerprint of each card's file set, because the
stores this exists for held 142,781 cards. The id check reads file NAMES only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from . import card_limit, card_origin
from .card_store_identity import read_meta
from .card_repository import _SIDECAR_FILES, QUARANTINE_DIR_NAME

log = logging.getLogger("quest-ai-runner.card-store")

REASON_COMPOUNDED = "compounded"
REASON_DUPLICATE = "duplicate-files"
REASON_ITEM_ID = "item-id"
RECENT_DAYS = 14
Pace = Callable[[], None]


def no_pace() -> None:
    return None


# --------------------------------------------------------------------------------------------
# Reading a store, cheaply
# --------------------------------------------------------------------------------------------

def card_files_in(cards_dir: Path) -> List[Path]:
    """The card files directly inside ``cards_dir`` (names only; nothing is opened)."""
    out: List[Path] = []
    try:
        with os.scandir(cards_dir) as it:
            for entry in it:
                name = entry.name
                if (name.endswith(".json") and not name.startswith(".") and name not in _SIDECAR_FILES
                        and entry.is_file()):
                    out.append(Path(entry.path))
    except OSError:
        pass
    return out


def compounded_ids(ids: Iterable[str]) -> Dict[str, str]:
    """``{id: why}`` for every id that is a compounded import copy. Reads the ids alone."""
    out: Dict[str, str] = {}
    for card_id in ids:
        why = card_origin.compounding_evidence(card_id)
        if why:
            out[card_id] = why
    return out


def card_paths(card: Dict[str, Any]) -> List[str]:
    """A card's file entries as plain strings, in either stored shape (backslashes read as slashes)."""
    out: List[str] = []
    for entry in card.get("files") or []:
        value = entry.get("path", "") if isinstance(entry, dict) else entry
        if value:
            out.append(str(value).replace("\\", "/"))
    return out


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def name_tokens(text: str) -> Set[str]:
    """Lowercase alphanumeric words of ``text`` (two letters or more)."""
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if len(t) >= 2}


def name_match_score(card: Dict[str, Any]) -> float:
    """How well a card's name and keywords match the words in its file and folder names (0.0 to 1.0).

    The share of the card's name/keyword words found among the words of its files' basenames and
    parent folders. Pure: reads only the card.
    """
    keywords = card.get("keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    own = name_tokens(card.get("name", ""))
    for keyword in keywords:
        own |= name_tokens(keyword)
    if not own:
        return 0.0
    seen: Set[str] = set()
    for rel in card_paths(card):
        seen |= name_tokens(rel)
    return len(own & seen) / len(own)


def keeper_rank(card: Dict[str, Any], fallback_id: str) -> Tuple[Any, ...]:
    """Sort key for cards pinning the same files; the first one sorted is kept.

    The most used, then the best match between its name/keywords and its file and folder names, then
    the earliest created (an undated card after every dated one), then the id.
    """
    try:
        usage = int(card.get("usage_count") or 0)
    except (TypeError, ValueError):
        usage = 0
    created = str(card_origin.provenance_of(card).get("created_at") or "")
    return (-usage, -name_match_score(card), created or "\uffff", str(card.get("id") or fallback_id))


def protection_reason(card: Dict[str, Any]) -> str:
    """Why this card must not be moved, or "" when it may be.

    ``managed_by`` is absolute: another system owns the card and rewrites it from its own source of
    truth. Usage and non-bootstrap provenance mean a person or a run put something here that no file
    can regenerate.
    """
    if card.get("managed_by"):
        return "managed by another system"
    try:
        if int(card.get("usage_count") or 0) > 0:
            return "has been used"
    except (TypeError, ValueError):
        pass
    created_by = str(card_origin.provenance_of(card).get("created_by_task") or "")
    if created_by and created_by != "bootstrap":
        return f"provenance {created_by}"
    return ""


@dataclass
class Facts:
    """What the tidy needs to know of one card, small enough to hold for 142,781 of them."""

    path: Path
    card_id: str
    files: bytes                      # fingerprint of the file set; b"" when the card pins no file
    usage: int = 0
    created: str = ""
    managed: bool = False
    learned: bool = False
    content: frozenset = frozenset()
    segments: int = 1
    compounded: str = ""
    name_match: float = 0.0


def fingerprint(paths: Iterable[str]) -> bytes:
    unique = sorted({str(p) for p in paths if p})
    if not unique:
        return b""
    return hashlib.blake2b("\0".join(unique).encode("utf-8", "replace"), digest_size=16).digest()


def facts_of(path: Path, card: Dict[str, Any]) -> Facts:
    prov = card_origin.provenance_of(card)
    created_by = str(prov.get("created_by_task") or "")
    try:
        usage = int(card.get("usage_count") or 0)
    except (TypeError, ValueError):
        usage = 0
    card_id = str(card.get("id") or path.stem)
    items = card.get("content")
    content = frozenset(str(i.get("id")) for i in items if isinstance(i, dict) and i.get("id")) \
        if isinstance(items, list) else frozenset()
    return Facts(
        path=path, card_id=card_id, files=fingerprint(card_paths(card)), usage=usage, name_match=name_match_score(card),
        created=str(prov.get("created_at") or ""), managed=bool(card.get("managed_by")),
        learned=bool(created_by and created_by != "bootstrap"), content=content,
        segments=len(card_origin.namespace_segments(card_id)),
        compounded=card_origin.compounding_evidence(card_id),
    )


def read_facts(files: Iterable[Path], pace: Pace = no_pace,
               should_stop: Callable[[], bool] = lambda: False) -> Iterator[Facts]:
    """Stream the facts of each card file; a card that cannot be read is skipped, and left alone."""
    for path in files:
        if should_stop():
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                card = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(card, dict):
            yield facts_of(path, card)
        pace()


# --------------------------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------------------------

@dataclass
class Move:
    path: Path
    card_id: str
    reason: str
    detail: str


@dataclass
class TidyPlan:
    scanned: int = 0
    moves: List[Move] = field(default_factory=list)
    kept: Dict[str, int] = field(default_factory=dict)         # why a candidate was left, with counts
    compounded_left: int = 0                                   # compounded, but nothing proves it a copy

    def by_reason(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for move in self.moves:
            out[move.reason] = out.get(move.reason, 0) + 1
        return out

    def keep(self, why: str) -> None:
        self.kept[why] = self.kept.get(why, 0) + 1


def best_first(facts: Facts) -> Tuple[Any, ...]:
    """Order inside a group of cards pinning the same files: the card to keep sorts first.

    Not a compounded copy before a compounded copy, then the fewest import prefixes, then the
    ``keeper_rank`` the cleanup script uses (most used, best name match, earliest, id), so the choice is
    the same every run and the same one a person running the script would make.
    """
    return (1 if facts.compounded else 0, facts.segments, -facts.usage, -facts.name_match,
            facts.created or "\uffff", facts.card_id)


def movable(candidate: Facts, keeper: Facts) -> str:
    """Why ``candidate`` must stay although it duplicates ``keeper``, or "" when it may move."""
    if candidate.managed:
        return "managed by another system"
    if candidate.content - keeper.content:
        return "carries notes the card it duplicates does not have"
    if not candidate.compounded:
        if candidate.usage > 0:
            return "has been used"
        if candidate.learned:
            return "learned from a run"
    return ""


def plan_tidy(files: List[Path], pace: Pace = no_pace,
              should_stop: Callable[[], bool] = lambda: False) -> TidyPlan:
    """What can be moved out of the store whose card files are ``files``, and why (a dry run)."""
    plan = TidyPlan(scanned=len(files))
    phantoms = [p for p in files if p.stem.startswith(card_origin.ITEM_PREFIX)]
    for path in phantoms:
        plan.moves.append(Move(path, path.stem, REASON_ITEM_ID,
                               f"stored under a vector point id; the card is {card_origin.card_id_of_item(path.stem)}"))
    phantom_set = set(phantoms)
    groups: Dict[bytes, List[Facts]] = {}
    for facts in read_facts([p for p in files if p not in phantom_set], pace, should_stop):
        if facts.files:
            groups.setdefault(facts.files, []).append(facts)
        elif facts.compounded:
            plan.compounded_left += 1
    for members in groups.values():
        if len(members) < 2:
            if members[0].compounded:
                plan.compounded_left += 1
            continue
        ordered = sorted(members, key=best_first)
        keeper = ordered[0]
        if keeper.compounded:
            plan.compounded_left += 1
        for candidate in ordered[1:]:
            why_kept = movable(candidate, keeper)
            if why_kept:
                plan.keep(why_kept)
                continue
            reason = REASON_COMPOUNDED if candidate.compounded else REASON_DUPLICATE
            plan.moves.append(Move(candidate.path, candidate.card_id, reason,
                                   f"pins the same files as {keeper.card_id}"))
    return plan


# --------------------------------------------------------------------------------------------
# Doing it
# --------------------------------------------------------------------------------------------

@dataclass
class TidyResult:
    moved: int = 0
    failed: int = 0
    by_reason: Dict[str, int] = field(default_factory=dict)
    manifest: Optional[Path] = None


def move_to(cards_dir: Path, path: Path, reason: str) -> Optional[Path]:
    """Move one card file into ``_quarantine/<reason>/`` without overwriting anything. None on failure."""
    try:
        folder = cards_dir / QUARANTINE_DIR_NAME / reason
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / path.name
        suffix = 1
        while target.exists():
            target = folder / f"{path.stem}.{suffix}.json"
            suffix += 1
        os.replace(str(path), str(target))
        return target
    except OSError:
        return None


def fold_item_card(cards_dir: Path, phantom: Path) -> None:
    """Carry the notes of a card stored under a vector point id onto the card it stands for.

    The target keeps everything it has; only content items it lacks (by item id) are added. When the
    target does not exist the notes become that card. The stray file is moved by the caller.
    """
    try:
        stray = json.loads(phantom.read_text(encoding="utf-8"))
        target_id = card_origin.card_id_of_item(phantom.stem)
        if not isinstance(stray, dict) or card_origin.card_id_problem(target_id):
            return
        target_path = cards_dir / f"{target_id}.json"
        if target_path.exists():
            target = json.loads(target_path.read_text(encoding="utf-8"))
            have = {i.get("id") for i in target.get("content") or [] if isinstance(i, dict)}
            extra = [i for i in stray.get("content") or [] if isinstance(i, dict) and i.get("id") not in have]
            if not extra:
                return
            target["content"] = list(target.get("content") or []) + extra
        else:
            target = dict(stray)
            target["id"] = target_id
        tmp = target_path.with_name(f".tmp_fold_{os.getpid()}.json")
        tmp.write_text(json.dumps(target, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(target_path))
    except Exception:  # noqa: BLE001
        log.debug("could not fold %s onto its card", phantom, exc_info=True)


def apply_tidy(cards_dir: Path, plan: TidyPlan, pace: Pace = no_pace,
               should_stop: Callable[[], bool] = lambda: False) -> TidyResult:
    """Carry out ``plan``: move each card, fold item-id cards first, write the manifest. Never deletes."""
    result = TidyResult()
    entries: List[Dict[str, Any]] = []
    for move in plan.moves:
        if should_stop():
            break
        if move.reason == REASON_ITEM_ID:
            fold_item_card(cards_dir, move.path)
        target = move_to(cards_dir, move.path, move.reason)
        if target is None:
            result.failed += 1
        else:
            result.moved += 1
            result.by_reason[move.reason] = result.by_reason.get(move.reason, 0) + 1
            entries.append({"card_id": move.card_id, "reason": move.reason, "detail": move.detail,
                            "moved_to": str(target.relative_to(cards_dir))})
        pace()
    if entries:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        manifest = cards_dir / QUARANTINE_DIR_NAME / f"{stamp}-tidy-manifest.json"
        try:
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({
                "cards_dir": str(cards_dir), "generated_at": card_origin.now_iso(), "moved": result.moved,
                "by_reason": result.by_reason, "kept": plan.kept, "entries": entries}, indent=2),
                encoding="utf-8")
            result.manifest = manifest
        except OSError:
            pass
    return result


def tidy_message(result: TidyResult) -> str:
    return (f"Tidied {result.moved:,} duplicate context cards. They are kept in a folder and can be put back.")


# --------------------------------------------------------------------------------------------
# A plain account of a store
# --------------------------------------------------------------------------------------------

@dataclass
class Health:
    """Facts about a store, with no judgement in them. Counts, and the evidence they come from."""

    total: int = 0
    own: int = 0
    imported: int = 0
    topic: int = 0
    learned: int = 0
    managed: int = 0
    compounded_ids: int = 0
    duplicate_file_sets: int = 0      # cards beyond the first of each group pinning identical files
    item_id_cards: int = 0
    dated: int = 0
    recent: int = 0                   # created in the last RECENT_DAYS, of those that carry a date
    quarantined: Dict[str, int] = field(default_factory=dict)
    folders: Optional[int] = None     # directories of the corpus that hold an indexable file at any depth
    per_folder: int = 1               # cards the folder rule allows per folder (0: no rule)
    topic_over_folders: bool = False

    def rows(self) -> List[Tuple[str, str]]:
        out = [
            ("cards in the index", f"{self.total:,}"),
            ("  written here", f"{self.own:,}"),
            ("  copies imported from other stores", f"{self.imported:,}"),
            ("  topic cards (written from the corpus)", f"{self.topic:,}"),
            ("  learned from runs", f"{self.learned:,}"),
            ("  from quests (managed)", f"{self.managed:,}"),
            ("ids that stack import prefixes", f"{self.compounded_ids:,}"),
            ("cards pinning the same files as another", f"{self.duplicate_file_sets:,}"),
            ("cards stored under a vector point id", f"{self.item_id_cards:,}"),
            ("cards with a creation date", f"{self.dated:,}"),
            (f"  created in the last {RECENT_DAYS} days", f"{self.recent:,}" if self.dated else "unknown (no dates yet)"),
            ("moved out of the index already", f"{sum(self.quarantined.values()):,}"),
        ]
        if self.folders is not None:
            out.append(("folders of the corpus that hold files", f"{self.folders:,}"))
            if self.per_folder:
                out.append(("  topic cards beyond one per folder", "yes" if self.topic_over_folders else "no"))
        return out

    def render(self) -> str:
        width = max(len(label) for label, _ in self.rows())
        return "\n".join(f"  {label:<{width}}  {value:>10}" for label, value in self.rows())


def indexable_folders(root: Path) -> int:
    """How many directories of the corpus hold an indexable file at any depth: the most places a topic
    card can be about (a card is about the deepest folder its files share). A fact about the corpus."""
    from ._walk import effective_skip_dirs
    from .context_folder_review import _dir_file_counts
    keys: Set[str] = set()
    for rel in _dir_file_counts(root, effective_skip_dirs(root)):
        parts = [p for p in rel.split("/") if p and p != "."]
        keys.add(".")
        for depth in range(1, len(parts) + 1):
            keys.add("/".join(parts[:depth]))
    return len(keys)


def topic_cards_over_folders(topic_count: int, folders: Optional[int], per_folder: int) -> bool:
    """True when the store holds more topic cards than the folder rule lets the corpus have.

    Both numbers are facts, not choices: how many folders the corpus has, and the per-folder rule in
    force (``QAR_BOOTSTRAP_MAX_CARDS_PER_FOLDER``, one by default). With no per-folder rule (0) the
    statement cannot be made and this is False.
    """
    return bool(folders) and per_folder > 0 and topic_count > folders * per_folder


def store_health(cards_dir: Path, *, root: Optional[Path] = None,
                 cards: Optional[Dict[str, Dict[str, Any]]] = None) -> Health:
    """Count what a store holds. ``cards`` may be passed when the caller has them loaded already."""
    from .context_card_ceilings import max_cards_per_folder
    health = Health(per_folder=max_cards_per_folder())
    if cards is None:
        cards = {}
        for path in card_files_in(cards_dir):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if isinstance(loaded, dict):
                cards[str(loaded.get("id") or path.stem)] = loaded
    health.total = len(cards)
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    by_files: Dict[frozenset, int] = {}
    for cid, card in cards.items():
        kind = card_limit.card_kind(card)
        health.imported += kind == card_limit.KIND_IMPORTED
        health.topic += kind == card_limit.KIND_TOPIC
        health.learned += kind == card_limit.KIND_LEARNED
        health.managed += kind == card_limit.KIND_MANAGED
        health.compounded_ids += bool(card_origin.compounding_evidence(cid))
        health.item_id_cards += cid.startswith(card_origin.ITEM_PREFIX)
        files = card_origin.file_set(card)
        if files:
            by_files[files] = by_files.get(files, 0) + 1
        created = str(card_origin.provenance_of(card).get("created_at") or "")
        if created:
            try:
                stamp = datetime.fromisoformat(created)
                stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
                health.dated += 1
                health.recent += stamp >= cutoff
            except ValueError:
                pass
    health.own = health.total - health.imported
    health.duplicate_file_sets = sum(n - 1 for n in by_files.values() if n > 1)
    health.quarantined = quarantine_counts(cards_dir)
    if root is not None and root.is_dir():
        health.folders = indexable_folders(root)
        health.topic_over_folders = topic_cards_over_folders(health.topic, health.folders, health.per_folder)
    return health


def quarantine_counts(cards_dir: Path) -> Dict[str, int]:
    from .card_limit_report import count_quarantined
    return count_quarantined(cards_dir)


def render_health_report(cards_dir: Path, entries: List[Dict[str, Any]], *, root: Optional[Path] = None) -> str:
    """The short account the cleanup script prints on a dry run: what the store holds, then, per reason,
    how many cards it would move and how many of those were created in the last two weeks.

    ``entries`` are the script's planned moves (each may carry ``created_at``). Counts only; a card with
    no date cannot be placed in time, and the table says so instead of guessing.
    """
    if root is None:
        recorded = str(read_meta(str(cards_dir)).get("root") or "")
        root = Path(recorded) if recorded else None
    health = store_health(cards_dir, root=root)
    lines = ["store health (counts, with no judgement in them):", health.render()]
    if root is None:
        lines.append("  (pass --root to count the folders of the corpus)")
    cutoff = datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)
    reasons: Dict[str, List[Dict[str, Any]]] = {}
    for entry in entries:
        reasons.setdefault(str(entry.get("reason")), []).append(entry)
    if reasons:
        lines += ["", f"{'would move, by reason':<26}{'cards':>9}   created in the last {RECENT_DAYS} days"]
        for reason, group in sorted(reasons.items()):
            dated = recent = 0
            for entry in group:
                try:
                    stamp = datetime.fromisoformat(str(entry.get("created_at") or ""))
                except ValueError:
                    continue
                stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
                dated += 1
                recent += stamp >= cutoff
            when = f"{recent:,} of {dated:,} dated" if dated else "no dates on these cards"
            lines.append(f"  {reason:<24}{len(group):>9,}   {when}")
    return "\n".join(lines)
