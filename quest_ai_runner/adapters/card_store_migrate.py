"""migrate-imports: convert a store that holds namespaced COPIES (the old behaviour) to mounts.

``plan_migrate_imports`` / ``apply_migrate_imports`` list the orphan vector points the copies left behind. Nothing is converted or
deleted unless asked, and a dry run writes nothing. The mounts and the copy origin are the hub's
(``hubofhubs.recall.card_store_mounts``); the risk gate it asks, the skip set and the card folder name are QAR's.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

from hubofhubs.recall.card_repository import FilesystemCardRepository
from hubofhubs.recall.card_store_identity import ensure_store_id, read_meta, store_id_of, update_identity
from hubofhubs.recall.card_store_mounts import (
    Mount,
    copy_origin_root,
    discover_ancestor_stores,
    discover_nested_stores,
    resolve_mounts,
)

from ..core.vector_scopes import CARD_SEED_SCOPE
from .walk_settings import STORE_DIR_NAME, effective_skip_dirs


@dataclass
class MigratePlan:
    cards_dir: str
    root: str
    own_store_id: str = ""
    copies: List[str] = field(default_factory=list)          # copy card ids that become mounts
    kept_copies: List[Tuple[str, str]] = field(default_factory=list)  # (card id, why kept)
    mounts: List[Mount] = field(default_factory=list)
    refused: List[Tuple[str, str]] = field(default_factory=list)
    needs_store_id: List[str] = field(default_factory=list)  # nested stores that cannot be mounted yet
    risk_blocked: List[Tuple[str, str]] = field(default_factory=list)  # folders the risk gate turned away
    orphan_point_ids: List[str] = field(default_factory=list)  # vector item ids the copies left
    orphans_checked: bool = False
    applied: bool = False
    pruned_points: int = 0
    problems: List[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"store {self.own_store_id or '(no id yet: --apply will create one)'}   root {self.root}",
            f"  namespaced copies that become mounts: {len(self.copies)}",
            f"  copies kept (source not mountable): {len(self.kept_copies)}",
            f"  stores mounted: {len(self.mounts)}   refused: {len(self.refused)}   "
            f"not initialised: {len(self.needs_store_id)}",
        ]
        for m in self.mounts:
            lines.append(f"    mount {m.store_id}  <- {m.cards_dir}")
        for d, why in self.refused:
            lines.append(f"    REFUSED {d}: {why}")
        for d in self.needs_store_id:
            lines.append(f"    NOT INITIALISED {d}: run `context store-info --init` on it first")
        if self.orphans_checked:
            lines.append(f"  orphan vector points left by those copies: {len(self.orphan_point_ids)}")
        else:
            lines.append(f"  vector items the copies would have embedded: {len(self.orphan_point_ids)} "
                         f"(pass --qdrant-url or --qdrant-path to check which exist)")
        for d, why in self.risk_blocked:
            lines.append(f"    RISK, NOT READ {d}: {why} (no verdict yet; set \"index\": true for it in "
                         f"folder_review.json to include it)")
        for p in self.problems:
            lines.append(f"  PROBLEM: {p}")
        lines.append("  APPLIED" if self.applied else "  dry run: nothing was changed")
        return "\n".join(lines)


def plan_migrate_imports(cards_dir: str, root: Optional[str],
                         vector_store: Any = None) -> MigratePlan:
    """What converting this store's namespaced copies to mounts would do. Reads only."""
    meta = read_meta(cards_dir)
    own_root = os.path.abspath(root or str(meta.get("root") or ""))
    plan = MigratePlan(cards_dir=str(cards_dir), root=own_root if meta or root else "",
                       own_store_id=str(meta.get("store_id") or ""))
    if not meta:
        plan.problems.append("no bootstrap_meta.json here: not a bootstrapped store")
        return plan
    if not root and not meta.get("root"):
        plan.problems.append("the store has no recorded root; pass --root")
        return plan
    if not os.path.isdir(own_root):
        plan.problems.append("the root is not an existing directory")
        return plan
    # The same gate every card source asks, from cached verdicts only: a report makes no model call.
    from .context_risk_gate import folder_gate
    skip = effective_skip_dirs(Path(own_root))
    gate = folder_gate(Path(own_root), skip, None, None, Path(cards_dir), persist=False)
    plan.risk_blocked = gate.blocked_report()
    outcome = resolve_mounts(cards_dir, Path(own_root), skip, gate=gate, store_dir_name=STORE_DIR_NAME)
    plan.mounts = outcome.mounts
    plan.refused = outcome.refused
    mounted_roots = {os.path.realpath(m.root) for m in outcome.mounts}
    for store_root in [*discover_nested_stores(Path(own_root), Path(cards_dir), skip, gate, store_dir_name=STORE_DIR_NAME),
                       *discover_ancestor_stores(Path(own_root), gate, store_dir_name=STORE_DIR_NAME)]:
        if not store_id_of(str(store_root / STORE_DIR_NAME)):
            plan.needs_store_id.append(str(store_root / STORE_DIR_NAME))
    cards = FilesystemCardRepository(cards_dir).load_all()
    for cid, card in cards.items():
        origin = copy_origin_root(card, own_root)
        if origin is None:
            continue
        if os.path.realpath(origin) in mounted_roots:
            plan.copies.append(cid)
        else:
            plan.kept_copies.append((cid, "its source store is not mounted"))
    # A copy's points were keyed by this store's scheme at the time: plain ``card:<id>`` for a store
    # that predates identity, ``card:<store_id>:<id>`` for one born with it. Both are candidates.
    sid = plan.own_store_id
    qualified = bool(sid) and meta.get("vector_id_scheme") == "qualified"
    plan.orphan_point_ids = [f"card:{sid}:{cid}" if qualified else f"card:{cid}" for cid in plan.copies]
    if vector_store is not None and hasattr(vector_store, "existing_item_ids"):
        candidates = [f"card:{cid}" for cid in plan.copies]
        if sid:
            candidates += [f"card:{sid}:{cid}" for cid in plan.copies]
        plan.orphan_point_ids = vector_store.existing_item_ids(candidates, scope=CARD_SEED_SCOPE)
        plan.orphans_checked = True
    return plan


def apply_migrate_imports(plan: MigratePlan, *, prune_points: bool = False,
                          vector_store: Any = None) -> MigratePlan:
    """Carry out ``plan``: record the mounts, delete the converted copies, optionally prune points.

    Order is the safe one: the mounts are recorded FIRST, so a failure part-way leaves the reader
    with both the copy and the mount (duplicate, never a gap).
    """
    if plan.problems:
        return plan
    cards_dir = plan.cards_dir
    sid = ensure_store_id(cards_dir)
    if not sid:
        plan.problems.append("could not create a store id")
        return plan
    plan.own_store_id = sid
    if not update_identity(cards_dir, mounts=[m.as_record() for m in plan.mounts], root=plan.root):
        plan.problems.append("could not record the mounts in bootstrap_meta.json")
        return plan
    repo = FilesystemCardRepository(cards_dir)
    for cid in plan.copies:
        repo.delete(cid)
    if prune_points and vector_store is not None and hasattr(vector_store, "delete_items"):
        plan.pruned_points = vector_store.delete_items(plan.orphan_point_ids, scope=CARD_SEED_SCOPE)
    plan.applied = True
    return plan
