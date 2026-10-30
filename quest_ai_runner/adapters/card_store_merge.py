"""Merging a store into a wider store, moving its cards and vectors without re-embedding.

THE CASE
--------
A lane widens its corpus root from a subfolder to its parent (root ``X/sub`` becomes ``X``). Its
store ``X/sub/.quest-context`` has to become part of the wider store ``X/.quest-context`` (which may
already exist, with its own cards and identity), because a mounted store needs an owner lane to stay
fresh and re-indexing ``X/sub`` would re-card and re-embed everything.

WHAT MOVES
----------
* Each card keeps its card id; every file path gets the narrow root's prefix relative to the wide
  root (``a.py`` becomes ``sub/a.py``). Cards that are themselves copy-imports of another store are
  not moved (their original is the thing to mount).
* Non-card state directories the narrow store holds (turns, per-rep notes, anything that is not a
  vector directory) are copied to the same relative place in the wide store, never over an existing
  file.
* Vector points keep their stored vectors. The embedder is never called:
    - legacy scheme (point id derived from the card id) in both stores: the point id is unchanged,
      so the point is rewritten in place with the wide store's ``store_id`` and the prefixed paths;
    - any other combination (a qualified-scheme source, or a renamed card): the stored vector is
      copied under the new point id (``card:<store_id>:<card_id>``) with the new ``store_id`` and
      paths, and the old point is deleted when the source is pruned.
  Cards with no point yet are simply not embedded; the wide lane's next seed pass does that, as for
  any card.

ID COLLISIONS (deterministic, never overwrites)
-----------------------------------------------
For a narrow card whose id already exists in the wide store:
  1. the wide card carries ``provenance.merged_from`` naming this narrow store and card, or has the
     same name and the same set of file paths: it IS this card (an earlier run, or the same topic
     carded twice). The wide card is kept, nothing is written ("already present");
  2. otherwise the incoming card is renamed to ``<id>--m<first 8 hex of the narrow store id>`` and
     moved under that id; a later run finds it by rule 1. If that renamed id is itself taken by a
     different card the card is reported as unresolved and left in the source.
Running the command again therefore changes nothing.

THE SOURCE
----------
Without ``--prune-source`` the source's cards and meta are left as they were (and the command says
so): it still claims its subtree until it is pruned. With it, the moved cards (and their copied
state files) are removed from the source and its ``bootstrap_meta.json`` records ``merged_into``, which
discovery reads as "not a store", so the wide store is the only owner of the subtree. The wide store's
identity records ``merged_from`` (source store id, date, counts).

ORDER (a failure leaves a duplicate, never a gap): cards written, vectors relabelled, state copied,
wide identity recorded, source pruned.
"""
from __future__ import annotations

import copy
import logging
import os
import posixpath
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.vector_scopes import CARD_SEED_SCOPE
from . import card_limit, card_origin
from .card_repository import QUARANTINE_DIR_NAME, FilesystemCardRepository
from .card_store_mounts import copy_origin_root
from .card_store_identity import (
    SCHEME_LEGACY,
    SCHEME_QUALIFIED,
    identity_of,
    now_iso,
    read_meta,
    update_identity,
    write_meta,
)
from .qdrant_vector_store import PointMove

log = logging.getLogger("quest-ai-runner.card-merge")

# Directories of the narrow store that are not "state to move": vectors (handled as points), the
# refresh stamp and the quarantine of cards taken out of the index.
STATE_EXCLUDED_DIRS = frozenset({"qdrant", "cards-qdrant", "index-state", QUARANTINE_DIR_NAME})
RENAME_SEP = "--m"
PLACEHOLDER_STORE_ID = "st_new"

STATUS_MOVE = "move"
STATUS_RENAMED = "renamed"
STATUS_ALREADY = "already"
STATUS_UNRESOLVED = "unresolved"


# --------------------------------------------------------------------------------------------
# path and card rewriting
# --------------------------------------------------------------------------------------------

def prefixed(path: str, prefix: str) -> str:
    """``path`` (relative to the narrow root) as a path relative to the wide root."""
    if not path or not prefix or posixpath.isabs(path):
        return path
    return posixpath.normpath(posixpath.join(prefix, path))


def card_paths(card: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for fe in card.get("files", []) or []:
        p = fe.get("path") if isinstance(fe, dict) else fe
        if p:
            out.append(str(p))
    return out


def moved_card(card: Dict[str, Any], new_id: str, prefix: str, source_store_id: str) -> Dict[str, Any]:
    """The narrow card as the wide store holds it: new id, prefixed paths, provenance noted."""
    out = copy.deepcopy(card)
    old_id = str(card.get("id") or "")
    out["id"] = new_id
    files: List[Any] = []
    for fe in out.get("files", []) or []:
        if isinstance(fe, dict):
            fe["path"] = prefixed(str(fe.get("path") or ""), prefix)
            files.append(fe)
        else:
            files.append(prefixed(str(fe), prefix))
    if "files" in out:
        out["files"] = files
    for item in out.get("content", []) or []:
        locator = item.get("locator") if isinstance(item, dict) else None
        if isinstance(locator, dict) and isinstance(locator.get("path"), str):
            locator["path"] = prefixed(locator["path"], prefix)
    prov = out.get("provenance")
    prov = dict(prov) if isinstance(prov, dict) else {}
    prov["merged_from"] = {"store_id": source_store_id, "card_id": old_id}
    out["provenance"] = prov
    return out


def is_same_card(existing: Dict[str, Any], incoming: Dict[str, Any], source_store_id: str,
                 old_id: str) -> bool:
    prov = existing.get("provenance")
    mf = prov.get("merged_from") if isinstance(prov, dict) else None
    if isinstance(mf, dict) and mf.get("store_id") == source_store_id and mf.get("card_id") == old_id:
        return True
    paths = set(card_paths(existing))
    name = str(existing.get("name") or "")
    return (name == str(incoming.get("name") or "") and paths == set(card_paths(incoming))
            and bool(paths or name))


def renamed_id(card_id: str, source_store_id: str) -> str:
    short = source_store_id[3:] if source_store_id.startswith("st_") else source_store_id
    return f"{card_id}{RENAME_SEP}{short[:8]}"


def item_id(scheme: str, store_id: str, card_id: str) -> str:
    """The vector item id (``card:...``) a store of ``scheme`` gives a card."""
    return f"card:{store_id}:{card_id}" if scheme == SCHEME_QUALIFIED else f"card:{card_id}"


# --------------------------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------------------------

@dataclass
class CardMove:
    old_id: str
    new_id: str
    status: str
    card: Dict[str, Any] = field(default_factory=dict)   # the card as the wide store will hold it


@dataclass
class MergePlan:
    narrow_dir: str
    wide_dir: str
    narrow_store_id: str = ""
    wide_store_id: str = ""
    narrow_root: str = ""
    wide_root: str = ""
    prefix: str = ""
    wide_exists: bool = False
    narrow_scheme: str = SCHEME_LEGACY
    wide_scheme: str = SCHEME_QUALIFIED
    moves: List[CardMove] = field(default_factory=list)
    skipped_copies: int = 0
    wide_copies: List[str] = field(default_factory=list)          # wide copy-imports of the source
    wide_copies_removed: int = 0
    files_checked: int = 0
    files_missing: int = 0
    missing_samples: List[str] = field(default_factory=list)
    state_files: List[str] = field(default_factory=list)          # relative paths to copy
    state_identical: List[str] = field(default_factory=list)      # already there, same bytes
    state_collisions: List[str] = field(default_factory=list)     # different bytes: left alone
    vectors_checked: bool = False
    points: Dict[str, int] = field(default_factory=dict)          # relabel_points counts
    old_points_deleted: int = 0
    prune_source: bool = False
    wide_item_ids: set = field(default_factory=set)               # vector item ids of wide cards
    applied: bool = False
    source_pruned: bool = False
    problems: List[str] = field(default_factory=list)

    def count(self, *statuses: str) -> int:
        return sum(1 for m in self.moves if m.status in statuses)

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        c = self.count
        lines = [
            f"merge {self.narrow_store_id or '(no id)'}  ->  "
            f"{self.wide_store_id or ('(created on apply)' if not self.wide_exists else '(no id yet)')}",
            f"  narrow root {self.narrow_root}   wide root {self.wide_root}   "
            f"path prefix added: {self.prefix or '(none)'}",
            f"  cards to move: {c(STATUS_MOVE) + c(STATUS_RENAMED)}   already in the wide store: "
            f"{c(STATUS_ALREADY)}   id collisions: {c(STATUS_RENAMED, STATUS_UNRESOLVED)} "
            f"(renamed {c(STATUS_RENAMED)}, unresolved {c(STATUS_UNRESOLVED)})   "
            f"copy-imports not moved: {self.skipped_copies}",
            f"  copy-imports of the source already in the wide store: {len(self.wide_copies)} "
            f"(removed with --prune-source: they are superseded by the moved cards)",
            f"  file references: {self.files_checked}   missing under the wide root: {self.files_missing}",
            f"  state files to copy: {len(self.state_files)}   already there: {len(self.state_identical)}"
            f"   collisions (kept wide): {len(self.state_collisions)}",
        ]
        for m in self.moves:
            if m.status in (STATUS_RENAMED, STATUS_UNRESOLVED):
                lines.append(f"    collision {m.old_id}: {m.status}"
                             + (f" as {m.new_id}" if m.status == STATUS_RENAMED else ""))
        for s in self.missing_samples:
            lines.append(f"    missing: {s}")
        for s in self.state_collisions[:5]:
            lines.append(f"    state collision: {s}")
        if self.vectors_checked:
            p = self.points
            lines.append(f"  points to relabel (same point id): {p.get('relabelled', 0)}   copied under "
                         f"a new point id: {p.get('copied', 0)}   already at target: {p.get('done', 0)}   "
                         f"no point: {p.get('missing', 0)}   re-embeds: 0")
        else:
            lines.append("  vectors not checked (pass --qdrant-url or --qdrant-path); re-embeds: 0")
        for pr in self.problems:
            lines.append(f"  PROBLEM: {pr}")
        if self.applied:
            lines.append("  APPLIED. " + ("The source was pruned and marked merged." if self.source_pruned
                         else "The source store was left untouched and still claims its subtree: re-run "
                              "with --prune-source once the wide lane is verified."))
        else:
            lines.append("  dry run: nothing was changed")
        return "\n".join(lines)


def store_root(cards_dir: str, meta: Dict[str, Any]) -> str:
    return os.path.abspath(str(meta.get("root") or Path(cards_dir).resolve().parent))


def relative_prefix(narrow_root: str, wide_root: str) -> Optional[str]:
    rel = os.path.relpath(os.path.realpath(narrow_root), os.path.realpath(wide_root))
    if rel == ".":
        return ""
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return Path(rel).as_posix()


def state_files_of(directory: str) -> List[str]:
    out: List[str] = []
    base = Path(directory)
    try:
        for entry in sorted(base.iterdir()):
            if entry.is_dir() and entry.name not in STATE_EXCLUDED_DIRS and not entry.name.startswith("."):
                out.extend(p.relative_to(base).as_posix() for p in sorted(entry.rglob("*")) if p.is_file())
    except OSError:
        pass
    return out


def same_bytes(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def plan_merge(narrow_dir: str, wide_dir: str, *, vector_store: Any = None,
               prune_source: bool = False, max_cards: Optional[int] = None) -> MergePlan:
    """What merging ``narrow_dir`` into ``wide_dir`` would do. Reads only (and reads points).

    ``max_cards`` is the wide store's card limit (``None``: ``QAR_MAX_CARDS``; ``0``: none). A merge
    that would leave the wide store above it is a problem, so nothing is applied: the cards would
    be written around the limit that every other writer answers to.
    """
    narrow_meta = read_meta(narrow_dir)
    wide_meta = read_meta(wide_dir)
    plan = MergePlan(narrow_dir=str(narrow_dir), wide_dir=str(wide_dir), prune_source=prune_source,
                     narrow_store_id=str(narrow_meta.get("store_id") or ""),
                     wide_store_id=str(wide_meta.get("store_id") or ""),
                     wide_exists=bool(wide_meta))
    if os.path.realpath(narrow_dir) == os.path.realpath(wide_dir):
        plan.problems.append("the source and the target are the same store")
        return plan
    if not narrow_meta:
        plan.problems.append("the source has no bootstrap_meta.json: not a bootstrapped store")
        return plan
    if not plan.narrow_store_id:
        plan.problems.append("the source has no store_id yet: run `context store-info --init --apply` on it")
        return plan
    merged = narrow_meta.get("merged_into")
    if merged:
        target = str(merged.get("store_id") if isinstance(merged, dict) else merged)
        if target != plan.wide_store_id:
            plan.problems.append(f"the source was already merged into a different store ({target})")
            return plan
    plan.narrow_root = store_root(narrow_dir, narrow_meta)
    plan.wide_root = store_root(wide_dir, wide_meta)
    rel = relative_prefix(plan.narrow_root, plan.wide_root)
    if rel is None:
        plan.problems.append("the source's root is not inside the target's root: widen the target's "
                             "recorded root first (`context move-root`) or pick the right target")
        return plan
    plan.prefix = rel
    plan.narrow_scheme = str(narrow_meta.get("vector_id_scheme") or SCHEME_LEGACY)
    plan.wide_scheme = (str(wide_meta.get("vector_id_scheme") or SCHEME_LEGACY) if wide_meta
                        else SCHEME_QUALIFIED)

    narrow_cards = FilesystemCardRepository(narrow_dir).load_all()
    wide_cards = FilesystemCardRepository(wide_dir).load_all() if wide_meta else {}
    taken = set(wide_cards)
    wide_by_files: Dict[frozenset, str] = {}
    for wide_id, wide_card in sorted(wide_cards.items()):
        origin = copy_origin_root(wide_card, plan.wide_root)
        if (origin is not None and os.path.realpath(origin) == os.path.realpath(plan.narrow_root)) \
                or card_origin.provenance_of(wide_card).get(card_origin.ORIGIN_STORE) == plan.narrow_store_id:
            plan.wide_copies.append(wide_id)
        elif not card_origin.is_copy(wide_card) and card_origin.file_set(wide_card):
            wide_by_files.setdefault(card_origin.file_set(wide_card), wide_id)
    plan.wide_item_ids = {item_id(plan.wide_scheme, plan.wide_store_id or PLACEHOLDER_STORE_ID, cid)
                          for cid in wide_cards}
    for old_id in sorted(narrow_cards):
        card = narrow_cards[old_id]
        if card_origin.is_copy(card):
            plan.skipped_copies += 1
            continue
        incoming = moved_card(card, old_id, rel, plan.narrow_store_id)
        existing = wide_cards.get(old_id)
        same_files = wide_by_files.get(card_origin.file_set(incoming))
        if same_files is not None:
            # The wide store already has a card of its own pinning exactly these files (it carded
            # them before the narrow store existed): two cards pinning the same paths are one card.
            plan.moves.append(CardMove(old_id, same_files, STATUS_ALREADY, incoming))
        elif existing is None:
            plan.moves.append(CardMove(old_id, old_id, STATUS_MOVE, incoming))
            taken.add(old_id)
        elif is_same_card(existing, incoming, plan.narrow_store_id, old_id):
            plan.moves.append(CardMove(old_id, old_id, STATUS_ALREADY, incoming))
        else:
            new_id = renamed_id(old_id, plan.narrow_store_id)
            other = wide_cards.get(new_id)
            if other is None:
                plan.moves.append(CardMove(old_id, new_id, STATUS_RENAMED,
                                           moved_card(card, new_id, rel, plan.narrow_store_id)))
                taken.add(new_id)
            elif is_same_card(other, incoming, plan.narrow_store_id, old_id):
                plan.moves.append(CardMove(old_id, new_id, STATUS_ALREADY, incoming))
            else:
                plan.moves.append(CardMove(old_id, new_id, STATUS_UNRESOLVED, incoming))
    limit = card_limit.max_cards_from_env() if max_cards is None else max_cards
    arriving = plan.count(STATUS_MOVE, STATUS_RENAMED)
    leaving = len(plan.wide_copies) if prune_source else 0
    if limit and len(wide_cards) + arriving - leaving > limit:
        plan.problems.append(
            f"the wide store would hold {len(wide_cards) + arriving - leaving:,} cards after the merge, over "
            f"its limit of {limit:,} ({card_limit.ENV_MAX_CARDS}); raise the limit first, or tidy the wide store")
    for mv in plan.moves:
        if mv.status in (STATUS_MOVE, STATUS_RENAMED):
            for p in card_paths(mv.card):
                plan.files_checked += 1
                if not os.path.exists(os.path.join(plan.wide_root, p)):
                    plan.files_missing += 1
                    if len(plan.missing_samples) < 5:
                        plan.missing_samples.append(p)

    for rel_file in state_files_of(narrow_dir):
        dest = Path(wide_dir) / rel_file
        if not dest.exists():
            plan.state_files.append(rel_file)
        elif same_bytes(Path(narrow_dir) / rel_file, dest):
            plan.state_identical.append(rel_file)
        else:
            plan.state_collisions.append(rel_file)

    if vector_store is not None and hasattr(vector_store, "relabel_points"):
        plan.vectors_checked = True
        plan.points = vector_store.relabel_points(point_moves(plan), scope=CARD_SEED_SCOPE,
                                                  delete_old=False, apply=False)
    return plan


def point_moves(plan: MergePlan) -> List[PointMove]:
    wide_sid = plan.wide_store_id or PLACEHOLDER_STORE_ID
    out: List[PointMove] = []
    for mv in plan.moves:
        if mv.status not in (STATUS_MOVE, STATUS_RENAMED):
            continue
        old_item = item_id(plan.narrow_scheme, plan.narrow_store_id, mv.old_id)
        if mv.status == STATUS_RENAMED and old_item in plan.wide_item_ids:
            # Legacy ids: the source's point for this card IS the wide store's own card's point
            # (same point id), so it is not this card's vector. Leave it; the wide lane embeds the
            # renamed card on its next seed pass (one card).
            continue
        out.append(PointMove(
            old_item_id=old_item, new_item_id=item_id(plan.wide_scheme, wide_sid, mv.new_id),
            store_id=wide_sid, paths=tuple(card_paths(mv.card))))
    return out


# --------------------------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------------------------

def prepare_wide(plan: MergePlan) -> bool:
    """Give the wide store a meta and an id if it lacks them. The meta of a store created here
    inherits the source's bootstrap bookkeeping (its cards come from that bootstrap)."""
    wide_dir = plan.wide_dir
    meta = read_meta(wide_dir)
    if not meta:
        narrow_meta = read_meta(plan.narrow_dir)
        base = {k: narrow_meta[k] for k in ("version", "feature_versions", "completed_at")
                if k in narrow_meta}
        base["card_count"] = 0
        base.update(identity_of({}, root=plan.wide_root))
        return write_meta(wide_dir, base)
    if not meta.get("store_id") or not meta.get("root") or not meta.get("vector_id_scheme"):
        merged = dict(meta)
        merged.update(identity_of(meta, root=plan.wide_root))
        return write_meta(wide_dir, merged)
    return True


def copy_state(plan: MergePlan) -> int:
    copied = 0
    for rel_file in plan.state_files:
        src, dest = Path(plan.narrow_dir) / rel_file, Path(plan.wide_dir) / rel_file
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return copied


def prune_source(plan: MergePlan) -> bool:
    """Remove what moved from the source and mark it merged. Collided state files stay."""
    repo = FilesystemCardRepository(plan.narrow_dir)
    for mv in plan.moves:
        if mv.status in (STATUS_MOVE, STATUS_RENAMED, STATUS_ALREADY):
            repo.delete(mv.old_id)
    base = Path(plan.narrow_dir)
    for rel_file in [*plan.state_files, *plan.state_identical]:
        try:
            (base / rel_file).unlink()
        except OSError:
            pass
    for d in sorted((p for p in base.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()   # only empty ones
        except OSError:
            pass
    current = read_meta(plan.narrow_dir).get("merged_into")
    if isinstance(current, dict) and current.get("store_id") == plan.wide_store_id:
        return True
    return update_identity(plan.narrow_dir, merged_into={"store_id": plan.wide_store_id,
                                                         "merged_at": now_iso()})


def record_merge_in_wide(plan: MergePlan) -> bool:
    meta = read_meta(plan.wide_dir)
    entries = [e for e in meta.get("merged_from") or []
               if isinstance(e, dict) and e.get("store_id") != plan.narrow_store_id]
    previous = next((e for e in meta.get("merged_from") or []
                     if isinstance(e, dict) and e.get("store_id") == plan.narrow_store_id), {})
    rerun = bool(previous) and not plan.count(STATUS_MOVE, STATUS_RENAMED)
    entries.append({
        "store_id": plan.narrow_store_id,
        "merged_at": previous.get("merged_at") or now_iso(),
        "path_prefix": plan.prefix,
        "cards": previous.get("cards", 0) if rerun else plan.count(STATUS_MOVE, STATUS_RENAMED, STATUS_ALREADY),
        "renamed": previous.get("renamed", 0) if rerun else plan.count(STATUS_RENAMED),
    })
    original = dict(meta)
    meta.update(identity_of(meta))
    meta["merged_from"] = entries
    meta["card_count"] = len(FilesystemCardRepository(plan.wide_dir).load_all())
    if meta == original:
        return True     # a re-run records nothing new, so it writes nothing
    return write_meta(plan.wide_dir, meta)


def apply_merge(narrow_dir: str, wide_dir: str, *, vector_store: Any = None,
                prune_source_after: bool = False, max_cards: Optional[int] = None) -> MergePlan:
    """Carry out the merge (see the module docstring for the order). Idempotent."""
    plan = plan_merge(narrow_dir, wide_dir, prune_source=prune_source_after, max_cards=max_cards)
    if plan.problems:
        return plan
    if not prepare_wide(plan):
        plan.problems.append("could not create or update the wide store's bootstrap_meta.json")
        return plan
    plan = plan_merge(narrow_dir, wide_dir, vector_store=vector_store, prune_source=prune_source_after,
                      max_cards=max_cards)
    if plan.problems:
        return plan

    repo = FilesystemCardRepository(wide_dir)
    for mv in plan.moves:
        if mv.status in (STATUS_MOVE, STATUS_RENAMED):
            if not repo.write(mv.new_id, mv.card):
                plan.problems.append(f"could not write card {mv.new_id} to the wide store")
                return plan

    if vector_store is not None and hasattr(vector_store, "relabel_points"):
        plan.points = vector_store.relabel_points(point_moves(plan), scope=CARD_SEED_SCOPE,
                                                  delete_old=prune_source_after, apply=True)
        plan.vectors_checked = True
        if prune_source_after:
            # Cards already present in the wide store: the source's own point for them is surplus
            # when it is a different point from the wide store's.
            wide_sid = plan.wide_store_id
            surplus = [item_id(plan.narrow_scheme, plan.narrow_store_id, mv.old_id)
                       for mv in plan.moves if mv.status == STATUS_ALREADY
                       and item_id(plan.narrow_scheme, plan.narrow_store_id, mv.old_id)
                       != item_id(plan.wide_scheme, wide_sid, mv.new_id)]
            if surplus and hasattr(vector_store, "delete_items"):
                plan.old_points_deleted = vector_store.delete_items(surplus, scope=CARD_SEED_SCOPE)

    if prune_source_after and plan.wide_copies:
        # The wide store held copies of the source's cards under namespaced ids (copy mode). The
        # moved cards replace them, so they and their points are surplus.
        for copy_id in plan.wide_copies:
            if repo.delete(copy_id):
                plan.wide_copies_removed += 1
        if vector_store is not None and hasattr(vector_store, "delete_items"):
            vector_store.delete_items([item_id(plan.wide_scheme, plan.wide_store_id, c)
                                       for c in plan.wide_copies], scope=CARD_SEED_SCOPE)

    copy_state(plan)
    if not record_merge_in_wide(plan):
        plan.problems.append("could not record merged_from in the wide store's identity")
        return plan
    if prune_source_after:
        if not prune_source(plan):
            plan.problems.append("could not mark the source store as merged")
            return plan
        plan.source_pruned = True
    plan.applied = True
    return plan
