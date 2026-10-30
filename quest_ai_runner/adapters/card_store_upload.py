"""Upload a local card store to the Quest backend (``quest-ai-runner context upload``).

Moves a local store's cards, and the vectors it already has, into the backend's card store without
re-embedding anything. Pure planning plus a thin apply step over ``QuestApiCardRepository.bulk_write``:

* Only the store's OWN cards are read (``FilesystemCardRepository``); mounted stores are read-only
  views that never exist as files here, so they are not uploaded (their owners upload them).
* Namespaced copy-imports (provenance ``imported_from``) are skipped: the source store uploads the
  original, and uploading the copy would duplicate it. Exception, opt-in with
  ``adopt_ancestor_copies``: copies whose ``imported_from`` starts with ``..`` come from a store ABOVE
  this root that is often stale and never uploaded, and their paths are already relative to THIS root,
  so they are uploaded as this store's own (``imported_from`` dropped, ``adopted_from`` records it).
  Nested imports (a store below this root, with its own lane) stay skipped.
* A vector is offered only when the store's embedder id equals the backend's and the dimension
  matches; otherwise the card is uploaded without one and the BACKEND embeds it. Nothing is ever
  embedded here.
* Every uploaded card is stamped with the uploading store's writer ``origin`` (its ``store_id``, else
  an id derived from the runner environment and corpus root), so the lane that owns the store is the
  one that manages these cards on the shared backend afterwards (see docs/context-card-stores.md).
* Idempotent: ids are kept, so a re-run upserts the same cards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.vector_scopes import CARD_SEED_SCOPE
from hubofhubs.recall import card_origin
from hubofhubs.recall import card_store_identity as ident
from hubofhubs.recall.card_repository import FilesystemCardRepository
from .card_store_merge import item_id
from .quest_api_card_repository import BULK_MAX_CARDS, runner_env_id, strip_server_owned

REASON_IMPORTED = "namespaced copy-import: the source store uploads them"
REASON_NO_ID = "no usable card id"
REASON_REFUSED_ID = "an id that may never be stored (it repeats an import namespace or is a vector point id)"
ANCESTOR_PREFIX = ".."


@dataclass
class UploadPlan:
    cards_dir: str
    team_id: str = ""                      # "" with no org = private
    org_id: str = ""                       # set = every card goes to this org
    total: int = 0
    uploads: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    skipped: Dict[str, List[str]] = field(default_factory=dict)   # reason -> card ids
    mounts: int = 0
    adopted: List[str] = field(default_factory=list)   # ancestor-store copies uploaded as own
    vectors: Dict[str, List[float]] = field(default_factory=dict)  # usable, by card id
    vectors_available: int = 0
    vector_note: str = ""
    embedder_id: str = ""
    dimension: int = 0
    problems: List[str] = field(default_factory=list)

    @property
    def batches(self) -> int:
        return -(-len(self.uploads) // BULK_MAX_CARDS)

    def render(self) -> str:
        scope = f"org {self.org_id}" if self.org_id else (f"team {self.team_id}" if self.team_id else "private")
        lines = [
            f"upload plan for {self.cards_dir} ({scope})",
            f"  cards in the store: {self.total}",
            f"  to upload:          {len(self.uploads)} in {self.batches} batch(es) of up to {BULK_MAX_CARDS}",
        ]
        for reason, ids in sorted(self.skipped.items()):
            lines.append(f"  skipped {len(ids):>5}:    {reason}")
        if self.adopted:
            lines.append(f"  adopted from an ancestor store: {len(self.adopted)} (uploaded as this store's own)")
        if self.mounts:
            lines.append(f"  mounted stores:     {self.mounts} (read-only views, never uploaded from here)")
        lines.append(f"  vectors available:  {self.vectors_available}; usable by the backend: "
                     f"{len(self.vectors)}")
        if self.vector_note:
            lines.append(f"  vectors:            {self.vector_note}")
        for problem in self.problems:
            lines.append(f"  PROBLEM: {problem}")
        return "\n".join(lines)


def plan_upload(
    cards_dir: str,
    *,
    team_id: str = "",
    org_id: str = "",
    adopt_ancestor_copies: bool = False,
    backend_embedder: Optional[Dict[str, Any]] = None,
    vector_store: Any = None,
    local_embedder_id: str = "",
) -> UploadPlan:
    """What ``upload`` would send. ``backend_embedder`` is ``{embedder_id, dimension}`` from the
    backend (None when it was not asked); ``vector_store`` an opened ``QdrantVectorStore`` (or None);
    ``local_embedder_id`` the embedder that produced the store's vectors."""
    plan = UploadPlan(cards_dir=cards_dir, team_id=team_id, org_id=org_id)
    meta = ident.read_meta(cards_dir)
    plan.mounts = len([m for m in meta.get("mounts") or [] if isinstance(m, dict)])
    cards = FilesystemCardRepository(cards_dir).load_all()
    plan.total = len(cards)
    root = str(meta.get("root") or Path(cards_dir).resolve().parent)
    origin, _accepted = ident.writer_origins(cards_dir, env_id=runner_env_id(), root=root)
    for card_id in sorted(cards):
        card = cards[card_id]
        prov = card.get("provenance")
        imported_from = str(prov.get("imported_from") or "") if isinstance(prov, dict) else ""
        adopt = bool(imported_from) and adopt_ancestor_copies and imported_from.startswith(ANCESTOR_PREFIX)
        if (imported_from or card_origin.is_copy(card)) and not adopt:
            plan.skipped.setdefault(REASON_IMPORTED, []).append(card_id)
            continue
        if not str(card_id).strip():
            plan.skipped.setdefault(REASON_NO_ID, []).append(card_id)
            continue
        if card_origin.card_id_problem(str(card_id)):
            plan.skipped.setdefault(REASON_REFUSED_ID, []).append(card_id)
            continue
        body = strip_server_owned(card)
        body["id"] = str(card.get("id") or card_id)
        body.pop("team_id", None)          # the request's scope decides, never a stale field
        body.pop("org_id", None)
        if adopt:
            adopted_prov = {k: v for k, v in prov.items()
                            if k not in ("imported_from", card_origin.ORIGIN_STORE, card_origin.ORIGIN_ID,
                                         card_origin.ORIGIN_ROOT)}
            adopted_prov["adopted_from"] = imported_from
            body["provenance"] = adopted_prov
            plan.adopted.append(str(card_id))
        body.setdefault("runner_env_id", runner_env_id())
        body[ident.ORIGIN_FIELD] = origin
        plan.uploads.append((str(card_id), body))

    if vector_store is None:
        plan.vector_note = "no local vector store given (--qdrant-url/--qdrant-path): the backend will embed"
        return plan
    scheme = str(meta.get("vector_id_scheme") or ident.SCHEME_LEGACY)
    store_id = str(meta.get("store_id") or "")
    if scheme == ident.SCHEME_QUALIFIED and not store_id:
        plan.problems.append("store uses qualified vector ids but has no store_id")
        return plan
    item_by_card = {cid: item_id(scheme, store_id, cid) for cid, _ in plan.uploads}
    found = vector_store.read_vectors(list(item_by_card.values()), scope=CARD_SEED_SCOPE)
    have = {cid: found[item] for cid, item in item_by_card.items() if item in found}
    plan.vectors_available = len(have)
    if not backend_embedder:
        plan.vector_note = "backend embedder unknown (not queried): vectors will not be sent"
        return plan
    plan.embedder_id = str(backend_embedder.get("embedder_id") or "")
    plan.dimension = int(backend_embedder.get("dimension") or 0)
    if not local_embedder_id or local_embedder_id != plan.embedder_id:
        plan.vector_note = (f"store embedder {local_embedder_id or 'unknown'!r} is not the backend's "
                            f"{plan.embedder_id!r}: vectors not sent, the backend will embed")
        return plan
    usable = {cid: vec for cid, vec in have.items() if len(vec) == plan.dimension}
    wrong = len(have) - len(usable)
    plan.vectors = usable
    plan.vector_note = (f"embedder matches {plan.embedder_id!r} (dimension {plan.dimension})"
                        + (f"; {wrong} vector(s) of another dimension withheld" if wrong else ""))
    return plan


def apply_upload(plan: UploadPlan, repository: Any, *, on_batch: Any = None) -> Dict[str, Any]:
    """Send the plan through ``repository.bulk_write`` (a ``QuestApiCardRepository``)."""
    return repository.bulk_write(
        plan.uploads,
        plan.vectors or None,
        team_id=plan.team_id,
        org_id=plan.org_id,
        embedder_id=plan.embedder_id,
        dimension=plan.dimension,
        on_batch=on_batch,
    )
