"""Card-store identity, readability, and root moves.

A card store (a ``.quest-context`` directory, or any repository behind ``FileContextStore``) is a
thing with an IDENTITY that outlives its location:

  * ``store_id``  -- generated once, recorded in the store's ``bootstrap_meta.json``, NEVER derived
                    from a path. A card is identified by ``(store_id, card_id)``; its file paths
                    stay relative to its own store root and are never rewritten on import.
  * ``root``      -- the corpus root those relative paths resolve from. Recorded, so it can move
                    (``plan_move_root`` / ``apply_move_root``) without touching a card id, a
                    vector, or a relative path: zero re-embedding.

All of it lives in keys of the one ``bootstrap_meta.json`` the store already has. The functions here
only ever touch those keys and preserve every other key verbatim, so the bootstrap bookkeeping
(version, feature versions, card count) is never disturbed.

ACCESS
------
There is no access list. What a lane may read is what its OS user can read: the filesystem already
decides, and a policy field in a file the reader can open would only pretend to restrict what the
disk allows. A lane mounts a store iff the store is inside its corpus (nested) or is an ancestor of
its corpus root AND ``check_readable`` finds the store's directory readable by this process; a
permission error is reported (logged, and surfaced by ``store-info``) rather than swallowed, so a
lane never believes it has context it cannot read. The one place the filesystem does not enforce
this is the shared vector collection, which answers anyone who can reach the server; the vector arm
therefore restricts card queries to the stores actually mounted (``card_store_mounts``).

This module records identity only. Writes never reach a mounted store: a mount is read-only by
construction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from hubofhubs.filemodes import match_umask

log = logging.getLogger("quest-ai-runner.card-store")

BOOTSTRAP_META_FILE = "bootstrap_meta.json"

# Keys of bootstrap_meta.json that describe the store's identity rather than one bootstrap pass.
# ``_write_bootstrap_meta`` rewrites the pass keys every time and must carry these through.
IDENTITY_KEYS = ("store_id", "created_at", "root", "root_history", "mounts",
                 "vector_id_scheme", "vector_space", "vector_location",
                 "merged_from", "merged_into")

# How a store's cards are keyed in the shared vector collection:
#   "legacy"    -- ``card:<card_id>``; what every store embedded before it had an identity. Kept for
#                  stores that already have points, so giving them an id re-embeds nothing.
#   "qualified" -- ``card:<store_id>:<card_id>``; for stores born with an identity, so two stores
#                  sharing one collection can never overwrite each other's points.
SCHEME_LEGACY = "legacy"
SCHEME_QUALIFIED = "qualified"


# --------------------------------------------------------------------------------------------
# bootstrap_meta.json read / merge-write
# --------------------------------------------------------------------------------------------

def read_meta(cards_dir: str) -> Dict[str, Any]:
    """The store's ``bootstrap_meta.json`` as a dict, ``{}`` when absent or unreadable."""
    try:
        with open(Path(cards_dir) / BOOTSTRAP_META_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_meta(cards_dir: str, meta: Dict[str, Any]) -> bool:
    """Atomically replace ``bootstrap_meta.json`` with ``meta``. True on success."""
    try:
        Path(cards_dir).mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(cards_dir), prefix=".tmp_meta_", suffix=".json")
        match_umask(fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp, str(Path(cards_dir) / BOOTSTRAP_META_FILE))
            return True
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False
    except Exception:  # noqa: BLE001
        return False


def new_store_id() -> str:
    """A fresh store id. Random on purpose: an id derived from a path changes when the path does."""
    return "st_" + uuid.uuid4().hex[:16]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def identity_of(prior_meta: Dict[str, Any], *, root: Optional[str] = None) -> Dict[str, Any]:
    """The identity keys a (re)written ``bootstrap_meta.json`` must carry, given the previous one.

    Existing identity is preserved verbatim. A store with no id gets one now: ``qualified`` when it
    is brand new (no previous meta, so no points exist yet), ``legacy`` when it already had a meta
    (it may already have embedded points under the unqualified ids, which must keep their identity).
    ``root`` is recorded only when none is, because changing a recorded root is what ``move-root``
    is for; a bootstrap never moves it silently.
    """
    out = {k: prior_meta[k] for k in IDENTITY_KEYS if k in prior_meta}
    if not out.get("store_id"):
        out["store_id"] = new_store_id()
        out["created_at"] = now_iso()
        out["vector_id_scheme"] = SCHEME_LEGACY if prior_meta else SCHEME_QUALIFIED
    elif "vector_id_scheme" not in out:
        out["vector_id_scheme"] = SCHEME_LEGACY
    if root and not out.get("root"):
        out["root"] = str(root)
    return out


# --------------------------------------------------------------------------------------------
# Writer origin
# --------------------------------------------------------------------------------------------

ORIGIN_FIELD = "origin"


def derived_origin(env_id: str, root: Optional[str]) -> str:
    """A deterministic origin id for a corpus with no recorded store identity.

    Derived from (runner environment id, resolved corpus root), so the same lane gets the same id on
    every start and two lanes on one host with different roots get different ids.
    """
    resolved = ""
    if root:
        try:
            resolved = str(Path(root).resolve())
        except OSError:
            resolved = str(root)
    digest = hashlib.sha256(f"{env_id}\0{resolved}".encode("utf-8")).hexdigest()[:16]
    return "env_" + digest


def writer_origins(cards_dir: Optional[str], *, env_id: str, root: Optional[str]) -> Tuple[str, Set[str]]:
    """``(primary, accepted)`` origin ids for the corpus whose cards live in ``cards_dir``.

    ``primary`` is what a card written now is stamped with: the local store identity's ``store_id``
    when the lane has one, else the derived id. ``accepted`` is every id this corpus has ever
    legitimately written under (the store_id AND the derived id), so a store that is given an
    identity after it already wrote cards keeps ownership of them.
    """
    derived = derived_origin(env_id, root)
    store_id = str(read_meta(cards_dir).get("store_id") or "") if cards_dir else ""
    primary = store_id or derived
    return primary, {primary, derived} | ({store_id} if store_id else set())


def ensure_store_id(cards_dir: str) -> Optional[str]:
    """The store's id, creating and recording one if it has a meta but no id. None without a meta.

    Never creates a ``bootstrap_meta.json`` for a store that has none: that file's presence is what
    other corpora read as "this directory completed its own bootstrap", and a bare identity must not
    claim that.
    """
    meta = read_meta(cards_dir)
    if not meta:
        return None
    if meta.get("store_id"):
        return str(meta["store_id"])
    merged = dict(meta)
    merged.update(identity_of(meta))
    return str(merged["store_id"]) if write_meta(cards_dir, merged) else None


def store_id_of(cards_dir: str) -> Optional[str]:
    """The recorded store id, read only (never creates one)."""
    sid = read_meta(cards_dir).get("store_id")
    return str(sid) if sid else None


def merged_target(meta: Dict[str, Any]) -> str:
    """The store id a merged store's cards now live in ("" when ``meta`` is not a merged store)."""
    target = meta.get("merged_into")
    if isinstance(target, dict):
        return str(target.get("store_id") or "")
    return str(target or "")


def is_merged(cards_dir: str) -> bool:
    """Whether this store was merged into a wider one (``context merge-into --prune-source``).

    A merged store is not a store any more: discovery skips it, so the wider store is the only
    owner of the subtree and no reader sees two stores for the same files.
    """
    return bool(read_meta(cards_dir).get("merged_into"))


# --------------------------------------------------------------------------------------------
# Where a store's vectors live
# --------------------------------------------------------------------------------------------

EMBEDDED = "embedded"


def vector_location_record(location: str, *, collection_prefix: str = "qar_ctx",
                           vector_size: Optional[int] = None, embedder: str = "") -> Dict[str, Any]:
    """The ``vector_location`` identity value: where this store's card points are published.

    ``location`` is a Qdrant server URL, or ``"embedded"`` for a Qdrant living in a directory of
    the owner's (which only the owner's process can open). ``collection_prefix`` and
    ``vector_size`` name the collection (``<prefix>_default_<size>``); ``embedder`` is the model
    family that produced the vectors, so a reader can tell whether its own query embedder is
    compatible. Set by the OWNER; readers only read it from the mount.
    """
    loc = (location or "").strip()
    if loc.lower() == EMBEDDED:
        loc = EMBEDDED
    rec: Dict[str, Any] = {"location": loc, "collection_prefix": collection_prefix or "qar_ctx"}
    if vector_size:
        rec["vector_size"] = int(vector_size)
    if embedder:
        rec["embedder"] = embedder
    return rec


def vector_location_of(meta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The recorded ``vector_location`` of a store's meta, or None when absent or malformed."""
    rec = meta.get("vector_location")
    if isinstance(rec, dict) and str(rec.get("location") or "").strip():
        return rec
    return None


# --------------------------------------------------------------------------------------------
# Readability: the only access rule
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str


def check_readable(cards_dir: str) -> AccessDecision:
    """Whether THIS process can actually read the store at ``cards_dir``. Never raises.

    A real read, not a mode-bit guess: the directory is listed and its ``bootstrap_meta.json`` is
    opened, so ACLs, group membership and a mount that went away all show up as what they are. The
    reason names the failure (permission denied, missing, ...) so the caller can log it.
    """
    try:
        if not os.path.isdir(cards_dir):
            return AccessDecision(False, "the card directory does not exist")
        if not os.access(cards_dir, os.R_OK | os.X_OK):
            return AccessDecision(False, "permission denied: this user cannot read the card directory")
        with os.scandir(cards_dir) as it:
            next(it, None)
        with open(os.path.join(cards_dir, BOOTSTRAP_META_FILE), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("merged_into"):
            return AccessDecision(False, "the store was merged into a wider store "
                                         f"({merged_target(data)}); mount that one instead")
        if not isinstance(data, dict) or not data.get("store_id"):
            return AccessDecision(False, "the store has no store_id yet (its owner must initialise it: "
                                         "`context store-info --init`)")
        return AccessDecision(True, "readable")
    except PermissionError:
        return AccessDecision(False, "permission denied: this user cannot read the store")
    except FileNotFoundError:
        return AccessDecision(False, "the store has no bootstrap_meta.json")
    except (OSError, ValueError) as exc:
        return AccessDecision(False, f"the store could not be read ({exc.__class__.__name__})")


# --------------------------------------------------------------------------------------------
# Move root
# --------------------------------------------------------------------------------------------

@dataclass
class MoveRootPlan:
    cards_dir: str
    store_id: str
    old_root: str
    new_root: str
    card_count: int = 0
    file_refs: int = 0
    refs_present_at_new_root: int = 0
    missing_samples: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    # The invariants a move keeps by construction; printed so the dry run says what does NOT change.
    card_ids_changed: int = 0
    relative_paths_changed: int = 0
    reembeds: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [
            f"store {self.store_id or '(no id yet)'}",
            f"  root: {self.old_root or '(not recorded)'}  ->  {self.new_root}",
            f"  cards: {self.card_count}   file references: {self.file_refs} "
            f"({self.refs_present_at_new_root} present under the new root)",
            f"  card ids changed: {self.card_ids_changed}   relative paths changed: "
            f"{self.relative_paths_changed}   re-embeds: {self.reembeds}",
        ]
        for s in self.missing_samples:
            lines.append(f"  missing under new root: {s}")
        for p in self.problems:
            lines.append(f"  PROBLEM: {p}")
        return "\n".join(lines)


def plan_move_root(cards_dir: str, new_root: str, *, sample_limit: int = 5) -> MoveRootPlan:
    """What moving ``cards_dir``'s recorded root to ``new_root`` would change. Reads only."""
    from .card_repository import FilesystemCardRepository  # local: card_repository is a sibling

    meta = read_meta(cards_dir)
    new_abs = os.path.abspath(os.path.expanduser(new_root))
    plan = MoveRootPlan(cards_dir=str(cards_dir), store_id=str(meta.get("store_id") or ""),
                        old_root=str(meta.get("root") or ""), new_root=new_abs)
    if not meta:
        plan.problems.append("no bootstrap_meta.json here: not a bootstrapped store")
        return plan
    if not os.path.isdir(new_abs):
        plan.problems.append("the new root is not an existing directory")
    if plan.old_root and os.path.realpath(plan.old_root) == os.path.realpath(new_abs):
        plan.problems.append("the new root is the recorded root: nothing to move")
    try:
        cards = FilesystemCardRepository(str(cards_dir)).load_all()
    except Exception:  # noqa: BLE001
        cards = {}
    plan.card_count = len(cards)
    for card in cards.values():
        for fe in card.get("files", []) or []:
            rel = fe.get("path") if isinstance(fe, dict) else fe
            if not rel:
                continue
            plan.file_refs += 1
            if os.path.exists(os.path.join(new_abs, str(rel))):
                plan.refs_present_at_new_root += 1
            elif len(plan.missing_samples) < sample_limit:
                plan.missing_samples.append(str(rel))
    return plan


def apply_move_root(cards_dir: str, new_root: str, *, force: bool = False) -> MoveRootPlan:
    """Record ``new_root`` as the store's root. Cards, ids, vectors and relative paths are untouched.

    Refuses (leaving the store as it was) when the plan has problems, unless ``force``. The previous
    root is kept in ``root_history`` so a move can be undone by moving back.
    """
    plan = plan_move_root(cards_dir, new_root)
    meta = read_meta(cards_dir)
    if not meta:
        return plan
    if plan.problems and not force:
        return plan
    if not meta.get("store_id"):
        meta.update(identity_of(meta))
        plan.store_id = str(meta["store_id"])
    history = list(meta.get("root_history") or [])
    if meta.get("root"):
        history.append({"root": meta["root"], "until": now_iso()})
    meta["root_history"] = history[-20:]
    meta["root"] = plan.new_root
    if not write_meta(cards_dir, meta):
        plan.problems.append("could not write bootstrap_meta.json")
    return plan


def update_identity(cards_dir: str, **fields: Any) -> bool:
    """Merge identity ``fields`` (any of ``IDENTITY_KEYS``) into an existing meta. False if no meta."""
    meta = read_meta(cards_dir)
    if not meta:
        return False
    meta.update(identity_of(meta))
    for k, v in fields.items():
        if k in IDENTITY_KEYS:
            meta[k] = v
    return write_meta(cards_dir, meta)
