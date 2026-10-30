"""Tidying a card store: what is PROVABLY redundant, moved (never deleted), and a plain account of the store.

Three things are provable from the cards alone, and these are the only ones moved without a person
looking:

  compounded      a copy under an id that stacks import prefixes (``a--root-x--root-y--topic``: see
                  ``card_origin.compounding_evidence``) whose files are pinned, exactly, by another card of
                  the store. It is a copy of that card;
  duplicate-files two cards pinning exactly the same paths and carrying the same name are one card. The most
                  used stays, else the earliest, else the first by name. The same files under a different name
                  are two topics of one file, and both stay;
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
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import card_limit, card_origin
from ..core.scope_tags import union_scope_tags
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

    The most used, then (usage being equal) a card an index pass wrote before one somebody added to,
    then the best match between its name/keywords and its file and folder names, then the earliest
    created (an undated card after every dated one), then the id.
    """
    try:
        usage = int(card.get("usage_count") or 0)
    except (TypeError, ValueError):
        usage = 0
    created = str(card_origin.provenance_of(card).get("created_at") or "")
    return (-usage, 1 if card_origin.protection(card) else 0, -name_match_score(card), created or "\uffff",
            str(card.get("id") or fallback_id))


def protection_reason(card: Dict[str, Any]) -> str:
    """Why this card must not be moved, or "" when it may be: ``card_origin.protection``, the one definition."""
    return card_origin.protection(card)


@dataclass
class Facts:
    """What the tidy needs to know of one card, small enough to hold for 142,781 of them."""

    path: Path
    card_id: str
    files: bytes                      # fingerprint of the file set; b"" when the card pins no file
    usage: int = 0
    created: str = ""
    protection: str = ""              # why it may not be moved ("" when it is only what an index pass wrote)
    segments: int = 1
    compounded: str = ""
    name_match: float = 0.0           # worked out for the members of a group only (see ``plan_tidy``)
    name_key: str = ""                # the card's name, lower case on one line: what it is about


def name_key_of(card: Dict[str, Any]) -> str:
    return " ".join(str(card.get("name") or "").lower().split())


def store_id_of(cards_dir: Path) -> str:
    """The ``store_id`` a card folder recorded for itself, or "" (it never creates one)."""
    return str(read_meta(str(cards_dir)).get("store_id") or "")


def fingerprint(paths: Iterable[str]) -> bytes:
    unique = sorted({str(p) for p in paths if p})
    if not unique:
        return b""
    return hashlib.blake2b("\0".join(unique).encode("utf-8", "replace"), digest_size=16).digest()


def facts_of(path: Path, card: Dict[str, Any]) -> Facts:
    """The cheap facts of one card. The protection here is the quick check; a card that turns out to be a
    member of a group of cards pinning the same files gets the full one (``plan_tidy``)."""
    prov = card_origin.provenance_of(card)
    try:
        usage = int(card.get("usage_count") or 0)
    except (TypeError, ValueError):
        usage = 0
    card_id = str(card.get("id") or path.stem)
    return Facts(
        path=path, card_id=card_id, files=fingerprint(card_paths(card)), usage=usage,
        created=str(prov.get("created_at") or ""),
        protection=card_origin.protection(card, check_edits=False),
        segments=len(card_origin.namespace_segments(card_id)),
        compounded=card_origin.compounding_evidence(card_id),
        name_key=name_key_of(card),
    )


def read_card(path: Path) -> Optional[Dict[str, Any]]:
    """The card in a file, or ``None`` when the file is gone or is not a card."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            card = json.load(fh)
    except Exception:  # noqa: BLE001
        return None
    return card if isinstance(card, dict) else None


# --------------------------------------------------------------------------------------------
# Joining one card's data onto another
# --------------------------------------------------------------------------------------------

def item_stamp(item: Dict[str, Any]) -> float:
    try:
        return float(item.get("ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


JOINED_COPIES = "joined_copies"      # provenance key of a card: the ids of the cards already joined onto it


def fold_card(source: Dict[str, Any], target: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
    """``target`` with what ``source`` holds joined onto it, or ``(None, why)`` when that is not safe.

    Used when a card that a person or a run touched is about to leave the index because another card
    stands for it (a stray stored under a vector point id, a compounded or superseded copy). Nothing may
    be lost, so: the notes of both are kept by item id (of two items with one id, the newer by ``ts``; two
    that differ with the same ``ts`` cannot be told apart, so the source stays), quest tags are the union,
    usage adds up, conventions are the union. A card another system owns is never folded. A name,
    description or summary that differs from the target's cannot be joined (which one is right is a
    judgement), so the source stays where it is and the reason is returned.

    Joining is IDEMPOTENT: the target records the id of every card joined onto it (``provenance.joined_copies``),
    and a source it already holds adds nothing again. A process killed between writing the target and moving
    the source therefore does not count the source's usage twice when the join is repeated.
    """
    if source.get("managed_by") or source.get("managed_fields") or source.get("managed_items"):
        return None, "managed by another system"
    for name in ("name", "description", "summary"):
        a, b = str(source.get(name) or "").strip(), str(target.get(name) or "").strip()
        if a and b and a != b:
            return None, f"its {name} differs from the card it would join"
    merged = json.loads(json.dumps(target))
    prov = merged.get("provenance") if isinstance(merged.get("provenance"), dict) else {}
    source_id = str(source.get("id") or "")
    already = bool(source_id) and source_id in (prov.get(JOINED_COPIES) or [])
    for name in ("name", "description", "summary"):
        if not str(merged.get(name) or "").strip() and str(source.get(name) or "").strip():
            merged[name] = source[name]
    items: List[Dict[str, Any]] = [i for i in (merged.get("content") or []) if isinstance(i, dict)]
    at = {str(i.get("id")): n for n, i in enumerate(items) if i.get("id")}
    for item in source.get("content") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or "")
        if key and key in at:
            held = items[at[key]]
            if item != held and item_stamp(item) == item_stamp(held):
                return None, f"note {key} differs and carries the same time, so neither is the newer"
            if item_stamp(item) > item_stamp(held):
                items[at[key]] = item
        elif item not in items:
            items.append(item)
            if key:
                at[key] = len(items) - 1
    if items or "content" in source or "content" in merged:
        merged["content"] = items
    tags = union_scope_tags(merged.get("scope_tags"), source.get("scope_tags"))
    if tags:
        merged["scope_tags"] = tags
    if not already:
        try:
            merged["usage_count"] = int(merged.get("usage_count") or 0) + int(source.get("usage_count") or 0)
        except (TypeError, ValueError):
            pass
    if not already:
        heat = {fe.get("path"): fe for fe in source.get("files") or []
                if isinstance(fe, dict) and fe.get("path") and int(fe.get("use_count") or 0) > 0}
        for fe in merged.get("files") or []:        # the heat retrievals left on the files joins the card that stays
            if isinstance(fe, dict) and fe.get("path") in heat:
                gone = heat[fe["path"]]
                fe["use_count"] = int(fe.get("use_count") or 0) + int(gone.get("use_count") or 0)
                fe["last_used_ts"] = max(float(fe.get("last_used_ts") or 0.0), float(gone.get("last_used_ts") or 0.0))
    if str(merged.get("last_outcome") or "unknown") == "unknown" and source.get("last_outcome"):
        merged["last_outcome"] = source["last_outcome"]
    conventions = list(merged.get("conventions") or [])
    conventions += [c for c in (source.get("conventions") or []) if c not in conventions]
    if conventions:
        merged["conventions"] = conventions
    if source_id and not already:
        prov[JOINED_COPIES] = list(prov.get(JOINED_COPIES) or []) + [source_id]
        merged["provenance"] = prov
    return merged, ""


def join_delta(before: Dict[str, Any], after: Dict[str, Any], source_id: str) -> Dict[str, Any]:
    """What a join changed on the card it was joined onto, in the terms ``undo_join`` needs to take it back."""
    held = {str(i.get("id")): i for i in (before.get("content") or []) if isinstance(i, dict) and i.get("id")}
    added: List[str] = []
    replaced: List[Dict[str, Any]] = []
    for item in after.get("content") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        key = str(item["id"])
        if key not in held:
            added.append(key)
        elif item != held[key]:
            replaced.append(held[key])
    def listed(card: Dict[str, Any], name: str) -> List[Any]:
        return list(card.get(name) or [])

    def heat(card: Dict[str, Any]) -> Dict[str, int]:
        return {fe["path"]: int(fe.get("use_count") or 0) for fe in card.get("files") or []
                if isinstance(fe, dict) and fe.get("path")}
    before_heat, after_heat = heat(before), heat(after)
    return {
        "file_use_added": {p: n - before_heat.get(p, 0) for p, n in after_heat.items() if n > before_heat.get(p, 0)},
        "source": source_id,
        "notes_added": added, "notes_replaced": replaced,
        "tags_added": [t for t in listed(after, "scope_tags") if t not in listed(before, "scope_tags")],
        "conventions_added": [c for c in listed(after, "conventions") if c not in listed(before, "conventions")],
        "fields_filled": [n for n in ("name", "description", "summary")
                          if not str(before.get(n) or "").strip() and str(after.get(n) or "").strip()],
        "usage_added": int(after.get("usage_count") or 0) - int(before.get("usage_count") or 0),
        "content_created": "content" not in before and "content" in after,
        "tags_created": "scope_tags" not in before and "scope_tags" in after,
        "conventions_created": "conventions" not in before and "conventions" in after,
    }


def undo_join(card: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """``card`` with one join taken back: the notes, tags, usage and fields ``delta`` says it added are removed
    and the older versions of the notes it replaced are put back. A note the card no longer has is left alone."""
    out = json.loads(json.dumps(card))
    added = set(delta.get("notes_added") or [])
    old = {str(i.get("id")): i for i in delta.get("notes_replaced") or [] if isinstance(i, dict)}
    items = []
    for item in out.get("content") or []:
        key = str(item.get("id")) if isinstance(item, dict) else ""
        if key in added:
            continue
        items.append(old.get(key, item))
    if items or ("content" in out and not delta.get("content_created")):
        out["content"] = items
    else:
        out.pop("content", None)
    for name, created, added_key in (("scope_tags", "tags_created", "tags_added"),
                                     ("conventions", "conventions_created", "conventions_added")):
        left = [v for v in out.get(name) or [] if v not in (delta.get(added_key) or [])]
        if left or (name in out and not delta.get(created)):
            out[name] = left
        else:
            out.pop(name, None)
    for name in delta.get("fields_filled") or []:
        out[name] = ""
    try:
        out["usage_count"] = max(0, int(out.get("usage_count") or 0) - int(delta.get("usage_added") or 0))
    except (TypeError, ValueError):
        pass
    for fe in out.get("files") or []:
        if isinstance(fe, dict) and fe.get("path") in (delta.get("file_use_added") or {}):
            fe["use_count"] = max(0, int(fe.get("use_count") or 0) - int(delta["file_use_added"][fe["path"]]))
    prov = out.get("provenance") if isinstance(out.get("provenance"), dict) else {}
    if delta.get("source") and delta["source"] in (prov.get(JOINED_COPIES) or []):
        prov[JOINED_COPIES] = [c for c in prov[JOINED_COPIES] if c != delta["source"]]
        if not prov[JOINED_COPIES]:
            prov.pop(JOINED_COPIES)
        out["provenance"] = prov
    return out


# --------------------------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------------------------

@dataclass
class Move:
    path: Path
    card_id: str
    reason: str
    detail: str
    keeper: Optional[Path] = None      # the card that pins the same files (compounded, duplicate-files)
    target_id: str = ""                # item-id: the card the stray stands for


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

    Not a compounded copy before a compounded copy, then the fewest import prefixes, then the most
    used, then (usage being equal) a card an index pass wrote before one somebody added to, so a hand-written
    or annotated card never displaces the topic card it sits beside; then the rest of the ``keeper_rank``
    the cleanup script uses (best name match, earliest, id). The choice is the same every run. Whether a
    card may be MOVED is a separate question (``Facts.protection``): a card somebody added to is kept
    whatever its rank.
    """
    return (1 if facts.compounded else 0, facts.segments, -facts.usage, 1 if facts.protection else 0,
            -facts.name_match, facts.created or "\uffff", facts.card_id)


def in_quarantine(cards_dir: Path, card_id: str) -> bool:
    """Whether a card of this id was moved out of the index (any reason)."""
    try:
        base = cards_dir / QUARANTINE_DIR_NAME
        return any((base / reason / f"{card_id}.json").is_file() for reason in os.listdir(base))
    except OSError:
        return False


def stray_target(cards_dir: Path, stray_id: str, own_store_id: str) -> Tuple[str, str]:
    """``(card id the stray stands for, "")``, or ``("", why)`` when it provably stands for none of this store's."""
    target_id = card_origin.card_id_of_item(stray_id, own_store_id)
    if card_origin.is_store_qualified(target_id):
        return "", "names a card mounted from another store"
    if card_origin.card_id_problem(target_id):
        return "", "the card it stands for could not be stored under that id"
    return target_id, ""


def plan_stray(cards_dir: Path, path: Path, own_store_id: str,
               get: Callable[[Path], Optional[Dict[str, Any]]] = read_card) -> Tuple[Optional[Move], str]:
    """The move for a card stored under a vector point id, or ``(None, why)`` when it must stay."""
    stray = get(path)
    if stray is None:
        return None, "unreadable"
    if stray.get("managed_by"):
        return None, "managed by another system"
    target_id, why = stray_target(cards_dir, path.stem, own_store_id)
    if why:
        return None, why
    target = get(cards_dir / f"{target_id}.json")
    if target is not None:
        merged, why = fold_card(stray, target)
        if merged is None:
            return None, why
        detail = f"stored under a vector point id; its notes join {target_id}"
    elif in_quarantine(cards_dir, target_id):
        return None, "the card it stands for was moved out of the index"
    else:
        detail = f"stored under a vector point id; it becomes {target_id}"
    return Move(path, path.stem, REASON_ITEM_ID, detail, target_id=target_id), ""


def has_cheap_evidence(name: str) -> bool:
    """Whether a card file NAME alone says something may be tidied: a vector point id (``card:<id>``) or an id
    that stacks import prefixes (``card_origin.compounding_evidence``). No card is read to ask."""
    stem = name[:-5] if name.endswith(".json") else name
    if stem.startswith(card_origin.ITEM_PREFIX):
        return True
    return card_origin.NAMESPACE_SEP in stem and bool(card_origin.compounding_evidence(stem))


def names_show_something(cards_dir: Path) -> bool:
    """Whether any card file name in ``cards_dir`` shows something to tidy (``has_cheap_evidence``): a listing
    of names, no card read, stopping at the first. The startup tidy asks this before it plans anything, so a
    clean store of any size pays one listing."""
    try:
        with os.scandir(cards_dir) as it:
            for entry in it:
                name = entry.name
                if (name.endswith(".json") and not name.startswith(".") and name not in _SIDECAR_FILES
                        and has_cheap_evidence(name)):
                    return True
    except OSError:
        return False
    return False


def plan_tidy(files: List[Path], pace: Pace = no_pace,
              should_stop: Callable[[], bool] = lambda: False, own_store_id: str = "",
              cards: Optional[Dict[str, Dict[str, Any]]] = None) -> TidyPlan:
    """What can be moved out of the store whose card files are ``files``, and why (a dry run).

    A card moves only when it is exactly what an index pass wrote (``card_origin.protection``), or when
    everything it holds can be joined onto the card that stands for it (``fold_card``). The plan is a
    proposal: ``apply_tidy`` looks at every card again, under the folder's lock, before it moves it.

    ``cards`` is the store's cards when something has already parsed them (``{id: card}``): the plan is then
    made from those and reads no card file, which is what the startup tidy does so the first turn is not
    made to parse a large store twice. Without it each file is read, one at a time.
    """
    def get(path: Path) -> Optional[Dict[str, Any]]:
        card = cards.get(path.stem) if cards is not None else None
        return card if card is not None else read_card(path)

    plan = TidyPlan(scanned=len(files))
    strays = [p for p in files if p.stem.startswith(card_origin.ITEM_PREFIX)]
    stray_set = set(strays)
    for path in strays:
        move, why = plan_stray(path.parent, path, own_store_id, get)
        if move is not None:
            plan.moves.append(move)
        else:
            plan.keep(f"a card stored under a vector point id stays: {why}")
        pace()
    groups: Dict[bytes, List[Facts]] = {}
    for path in files:
        if should_stop():
            break
        if path in stray_set:
            continue
        card = get(path)
        if card is not None:
            facts = facts_of(path, card)
            if facts.files:
                groups.setdefault(facts.files, []).append(facts)
            elif facts.compounded:
                plan.compounded_left += 1
        pace()
    for members in groups.values():
        if len(members) < 2:
            if members[0].compounded:
                plan.compounded_left += 1
            continue
        for facts in members:                       # only now is a card worth the rest of the work
            card = get(facts.path)
            if card is not None:
                facts.name_match = name_match_score(card)
                facts.protection = facts.protection or card_origin.protection(card)
        ordered = sorted(members, key=best_first)
        keeper = ordered[0]
        if keeper.compounded:
            plan.compounded_left += 1
        for candidate in ordered[1:]:
            if candidate.protection and (not candidate.compounded or candidate.protection.startswith("managed")):
                plan.keep(candidate.protection)
                continue
            if not candidate.compounded and candidate.name_key != keeper.name_key:
                # Two topics about one file are two cards (a handbook that says how billing works and how
                # sign-in works): the same files with a different name is not a copy.
                plan.keep("pins the same files as another card but is about something else (its name differs)")
                continue
            reason = REASON_COMPOUNDED if candidate.compounded else REASON_DUPLICATE
            detail = f"pins the same files as {keeper.card_id}"
            if candidate.protection:
                # A compounded copy that somebody added to: it moves only if everything it holds joins the
                # card it copies without loss. Decided here so the plan lists only moves that can happen.
                source, target = get(candidate.path), get(keeper.path)
                if source is None or target is None:
                    continue
                merged, why = fold_card(source, target)
                if merged is None:
                    plan.keep(f"{candidate.protection}, and it cannot join the card it copies: {why}")
                    continue
                detail += f"; what it holds ({candidate.protection}) joins that card first"
            plan.moves.append(Move(candidate.path, candidate.card_id, reason, detail, keeper=keeper.path))
    return plan


# --------------------------------------------------------------------------------------------
# Doing it
# --------------------------------------------------------------------------------------------

@dataclass
class TidyResult:
    moved: int = 0
    failed: int = 0
    by_reason: Dict[str, int] = field(default_factory=dict)
    left: Dict[str, int] = field(default_factory=dict)         # why a planned move was not made, with counts
    manifest: Optional[Path] = None
    moved_ids: List[str] = field(default_factory=list)         # the ids that left the index, for a cache to forget
    touched_ids: List[str] = field(default_factory=list)       # cards that were rewritten (a keeper joined onto)


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


def rewrite_card_file(path: Path, card: Dict[str, Any]) -> bool:
    """Replace the card file at ``path`` (any folder) with ``card``, atomically. False on failure."""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(card, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, str(path))
        return True
    except OSError:
        return False


def keeper_of_moved(cards_dir: Path, card_id: str) -> str:
    """The card that stands for ``card_id`` now, if a tidy or a retirement moved it out and recorded that in a
    manifest (``_quarantine/*manifest*.jsonl``): "" when none did. The newest manifest is read first. This is only
    asked when a writer comes with an edit for a card that is no longer in the index, so the manifests are
    read then, never on the way to a move."""
    manifests: List[Path] = []
    for base in (cards_dir / QUARANTINE_DIR_NAME, cards_dir / "tidy-state"):   # the repository tidy's state folder
        try:
            manifests += [p for p in base.rglob("*manifest*.jsonl") if p.is_file()]
        except OSError:
            continue
    manifests.sort(key=lambda p: p.name, reverse=True)
    for manifest in manifests:
        try:
            with open(manifest, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for raw in reversed(lines):
            if card_id not in raw:
                continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and line.get("card_id") == card_id and line.get("keeper"):
                return str(line["keeper"])
    return ""


def write_card_file(cards_dir: Path, card_id: str, card: Dict[str, Any]) -> bool:
    from .card_repository import FilesystemCardRepository
    return FilesystemCardRepository(str(cards_dir)).write(card_id, card)


def join_onto(cards_dir: Path, card: Dict[str, Any], target_path: Path) -> Tuple[str, Dict[str, Any]]:
    """Join what ``card`` holds onto the card in ``target_path``: ``("", delta)`` when done, else ``(why, {})``.

    ``delta`` says what the join changed on the target (``join_delta``), for the manifest and for the undo.
    The caller holds the folder's lock, and moves ``card`` only after this returns an empty reason.
    """
    target = read_card(target_path)
    if target is None:
        return "the card it joins is gone", {}
    merged, why = fold_card(card, target)
    if merged is None:
        return why, {}
    if not write_card_file(cards_dir, str(target.get("id") or target_path.stem), merged):
        return "the card it joins could not be written", {}
    return "", join_delta(target, merged, str(card.get("id") or ""))


@dataclass
class Settled:
    """What came of looking at a planned move again, just before making it."""

    moved_to: Optional[Path] = None
    why: str = ""                      # why it was not moved ("gone" when somebody else already moved it)
    keeper: str = ""                   # the card that stands for the one that moved
    joined: Dict[str, Any] = field(default_factory=dict)   # what was joined onto the keeper (``join_delta``)
    created: str = ""                  # a stray with no card to join became this card


def settle_move(cards_dir: Path, move: Move, own_store_id: str) -> Settled:
    """Look at the card again and, if the plan still holds, move it.

    The caller holds the folder's lock. Everything the plan decided is decided again from the files as
    they are NOW: a note added in between makes the card protected, a keeper that changed no longer
    covers it, a card somebody else already moved is not counted twice.
    """
    card = read_card(move.path)
    if card is None:
        return Settled(why="gone")
    keeper_id, joined, created = "", {}, ""
    if move.reason == REASON_ITEM_ID:
        if card.get("managed_by"):
            return Settled(why="managed by another system")
        target_id, why = stray_target(cards_dir, move.path.stem, own_store_id)
        if why:
            return Settled(why=why)
        target_path = cards_dir / f"{target_id}.json"
        if target_path.is_file():
            why, joined = join_onto(cards_dir, card, target_path)
            if why:
                return Settled(why=why)
            keeper_id = target_id
        elif in_quarantine(cards_dir, target_id):
            return Settled(why="the card it stands for was moved out of the index")
        else:
            becomes = dict(card)
            becomes["id"] = target_id
            if not write_card_file(cards_dir, target_id, becomes):
                return Settled(why="the card it becomes could not be written")
            keeper_id, created = target_id, target_id
    else:
        keeper = read_card(move.keeper) if move.keeper is not None else None
        if keeper is None:
            return Settled(why="the card it duplicates is gone")
        if set(card_paths(keeper)) != set(card_paths(card)) or not card_paths(card):
            return Settled(why="it no longer pins the same files as the card it duplicates")
        if move.reason == REASON_DUPLICATE and name_key_of(card) != name_key_of(keeper):
            return Settled(why="it is no longer named like the card it duplicates")
        why = card_origin.protection(card)
        if why:
            if move.reason != REASON_COMPOUNDED or card.get("managed_by"):
                return Settled(why=why)
            why, joined = join_onto(cards_dir, card, move.keeper)
            if why:
                return Settled(why=why)
        keeper_id = str(keeper.get("id") or move.keeper.stem)
    target = move_to(cards_dir, move.path, move.reason)
    if target is None:
        return Settled(why="could not be moved")
    return Settled(moved_to=target, keeper=keeper_id, joined=joined, created=created)


def apply_tidy(cards_dir: Path, plan: TidyPlan, pace: Pace = no_pace,
               should_stop: Callable[[], bool] = lambda: False, own_store_id: str = "") -> TidyResult:
    """Carry out ``plan``, one card at a time under the folder's lock, and write a manifest as it goes.

    Each card is looked at again just before it moves (``settle_move``), so a plan made minutes ago on a
    paced scan cannot move a card that gained notes since, and several processes tidying one folder
    move every card once. The manifest is one file per run (pid and a random suffix in its name), one
    line per move appended as it happens, so a run that stops half way still leaves its record: the line
    names the card that stands for the one that moved and what was joined onto it, which is what
    ``undo_tidy`` needs to take the move back. Never deletes.
    """
    from .card_store_lock import StoreLock
    result = TidyResult()
    lock = StoreLock.for_dir(str(cards_dir))
    manifest = cards_dir / QUARANTINE_DIR_NAME / (
        f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{os.urandom(3).hex()}-tidy-manifest.jsonl")
    journal = None
    try:
        for move in plan.moves:
            if should_stop():
                break
            with lock:
                settled = settle_move(cards_dir, move, own_store_id)
                if settled.moved_to is not None:
                    lock.bump()
            if settled.moved_to is not None:
                result.moved += 1
                result.moved_ids.append(move.card_id)
                if settled.keeper and settled.keeper not in result.touched_ids:
                    result.touched_ids.append(settled.keeper)
                result.by_reason[move.reason] = result.by_reason.get(move.reason, 0) + 1
                try:
                    if journal is None:
                        manifest.parent.mkdir(parents=True, exist_ok=True)
                        journal = open(manifest, "a", encoding="utf-8")
                        result.manifest = manifest
                    journal.write(json.dumps({
                        "card_id": move.card_id, "file": move.path.name, "reason": move.reason,
                        "detail": move.detail, "moved_to": f"{QUARANTINE_DIR_NAME}/{move.reason}/{settled.moved_to.name}",
                        "keeper": settled.keeper, "joined": settled.joined, "created": settled.created,
                        "at": card_origin.now_iso()}) + "\n")
                    journal.flush()                  # one line per move on disk as it happens: a kill leaves the record
                except OSError:
                    pass
            elif settled.why == "gone":
                pass                                  # somebody else moved it; not ours to count
            elif settled.why == "could not be moved":
                result.failed += 1
            else:
                result.left[settled.why] = result.left.get(settled.why, 0) + 1
            pace()
    finally:
        if journal is not None:
            journal.close()
    if result.manifest is not None:
        try:
            with open(result.manifest, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"summary": {"cards_dir": str(cards_dir), "moved": result.moved,
                                                 "by_reason": result.by_reason, "failed": result.failed,
                                                 "left": result.left, "kept": plan.kept}}) + "\n")
        except OSError:
            pass
    return result


def record_retirement(cards_dir: Path, moved_to: Path, card_id: str, keeper_id: str, joined: Dict[str, Any]) -> None:
    """Note, for a copy retired by a refresh (``superseded-import``), the card that stands for it and what was
    joined onto that card, in ``_quarantine/superseded-import/manifest.jsonl``, so an edit meant for it finds the
    keeper (``keeper_of_moved``) and ``undo_tidy`` can take the retirement back. Never raises."""
    try:
        with open(moved_to.parent / "manifest.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "card_id": card_id, "file": f"{card_id}.json", "reason": "superseded-import",
                "moved_to": str(moved_to.relative_to(cards_dir)), "keeper": keeper_id, "joined": joined,
                "created": "", "at": card_origin.now_iso()}) + "\n")
    except (OSError, ValueError):
        log.debug("could not record the retirement of %s", card_id, exc_info=True)


@dataclass
class UndoResult:
    restored: int = 0
    unjoined: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)


def undo_tidy(cards_dir: Path, manifest: Path, pace: Pace = no_pace) -> UndoResult:
    """Take back what a tidy manifest records, newest move first: each card goes back into the index, and what
    was joined onto the card that stood for it is taken off again (``undo_join``).

    A card whose place in the index is taken again, one already back, or one whose file is gone is skipped
    and counted. Moving the files back by hand puts the cards back but not what was joined onto their
    keepers, so each joined note would then exist twice; this is the undo that is exact.
    """
    from .card_store_lock import StoreLock
    result = UndoResult()
    lock = StoreLock.for_dir(str(cards_dir))
    lines: List[Dict[str, Any]] = []
    try:
        with open(manifest, "r", encoding="utf-8") as fh:
            for raw in fh:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict) and line.get("moved_to"):
                    lines.append(line)
    except OSError:
        result.skipped["the manifest could not be read"] = 1
        return result

    def skip(why: str) -> None:
        result.skipped[why] = result.skipped.get(why, 0) + 1

    for line in reversed(lines):
        with lock:
            moved = cards_dir / str(line["moved_to"])
            back = cards_dir / str(line.get("file") or f"{line['card_id']}.json")
            if not moved.is_file():
                skip("the moved file is gone")
                continue
            if back.exists():
                skip("a card is already in its place")
                continue
            keeper_id = str(line.get("keeper") or "")
            if line.get("joined") and keeper_id:
                keeper_path = cards_dir / f"{keeper_id}.json"
                keeper = read_card(keeper_path)
                if keeper is not None and write_card_file(cards_dir, keeper_id, undo_join(keeper, line["joined"])):
                    result.unjoined += 1
            created = str(line.get("created") or "")
            if created and (cards_dir / f"{created}.json").is_file():
                move_to(cards_dir, cards_dir / f"{created}.json", "undone")
            os.replace(str(moved), str(back))
            lock.bump()
            result.restored += 1
        pace()
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


FOLDER_COUNT_FILE = "index-state/folder_count.json"


def cached_folder_count(cards_dir: Path, root: Path) -> Optional[int]:
    """The corpus's folder count remembered from the last time it was worked out, while the store's last
    completed pass is still the one it was worked out for; None when there is none or it is out of date."""
    try:
        data = json.loads((cards_dir / FOLDER_COUNT_FILE).read_text(encoding="utf-8"))
        if data.get("root") == str(root) and data.get("stamp") == str(read_meta(str(cards_dir)).get("completed_at") or ""):
            return int(data["folders"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def remember_folder_count(cards_dir: Path, root: Path, folders: int) -> None:
    try:
        target = cards_dir / FOLDER_COUNT_FILE
        target.parent.mkdir(parents=True, exist_ok=True)
        stamp = str(read_meta(str(cards_dir)).get("completed_at") or "")
        rewrite_card_file(target, {"root": str(root), "stamp": stamp, "folders": int(folders)})
    except OSError:
        log.debug("could not remember the corpus folder count", exc_info=True)


@dataclass
class InflationCheck:
    topic: int
    folders: Optional[int]
    per_folder: int
    over: bool


def inflation_check(cards_dir: Path, root: Path, cards: Dict[str, Dict[str, Any]]) -> InflationCheck:
    """Whether the store holds more topic cards than its corpus has folders (times the per-folder rule).

    The folder count is a walk of the whole corpus (7 to 10 seconds on a large one), and it changes slowly,
    so it is remembered against the store's last completed pass. A remembered count can only ever say "not
    inflated" (a corpus that has grown has more room, not less); an answer of "inflated" is always checked
    against a fresh walk before it is believed, because it stops the store being embedded.
    """
    from .context_card_ceilings import max_cards_per_folder
    per_folder = max_cards_per_folder()
    topics = sum(1 for c in cards.values() if card_limit.card_kind(c) == card_limit.KIND_TOPIC)
    if per_folder <= 0 or topics == 0:
        return InflationCheck(topics, None, per_folder, False)
    folders = cached_folder_count(cards_dir, root)
    if folders is not None and not topic_cards_over_folders(topics, folders, per_folder):
        return InflationCheck(topics, folders, per_folder, False)
    folders = indexable_folders(root)
    remember_folder_count(cards_dir, root, folders)
    return InflationCheck(topics, folders, per_folder, topic_cards_over_folders(topics, folders, per_folder))


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
