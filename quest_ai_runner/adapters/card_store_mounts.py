"""Mounting card stores read-only, in place.

THE PROBLEM THIS REPLACES
-------------------------
A corpus that contained (or sat inside) another bootstrapped store used to COPY that store's cards
under new namespaced ids with every file path rewritten. One card was therefore stored once per
importing corpus, embedded once per importing corpus, and kept current by every one of them; a store
whose root moved needed every id rewritten and every vector recomputed.

THE MODEL
---------
A mount is a READ-ONLY, IN-PLACE view of another store:

  * Cards are read from the owner's directory every time the owner changes. There is no snapshot:
    the change marker the reader watches (``MountedCardRepository.revision``) includes the owner
    store's own directory stamp, so the owner's next write is the reader's next read. Freshness of a
    mounted store is solely its owner's job.
  * A card is identified by ``(store_id, card_id)``; in the reader it appears as
    ``"<store_id>:<card_id>"``. Its file paths stay relative to the OWNER's root and are resolved
    with the mount's prefix at read time (``view_card``), never rewritten on disk.
  * Nothing the reader produces is ever written to the mounted store. ``MountedCardRepository``
    refuses a write or delete addressed to a mounted card, and the store's own writes only ever go to
    the reader's own directory.
  * The reader's indexing passes skip the mounted subtree entirely (see ``FileContextStore``): no
    walk, no cards, no staleness check, no embedding. A mount that is not readable by this process
    or unavailable leaves the reader with NO cards for that subtree. It never falls back to indexing
    those files itself, which would recreate the duplicate this exists to remove.
  * Access is what the reading process can read on disk (``card_store_identity.check_readable``),
    checked whenever the cards reload, so a permission change takes effect on the reader's next read.
    A permission error is logged, never swallowed. Reads that pass are still subject to the usual
    quest fences (``scope_tags``).

VECTORS
-------
``StoreFencedVectorStore`` makes the vector arm query the mounted store's EXISTING points in the
shared collection, selected by the ``store_id`` payload field. The reader embeds nothing for a mounted
store, so a card has one point however many corpora mount it. Whether the reader's vector store is
the same space as the owner's is declared (``vector_space``) rather than guessed; where it is not,
the mounted store is served by the keyword arm only.

MIGRATION
---------
``plan_migrate_imports`` / ``apply_migrate_imports`` convert a store that holds namespaced COPIES
(the old behaviour) to mounts, and list the orphan vector points the copies left behind. Nothing is
converted or deleted unless asked, and a dry run writes nothing.
"""
from __future__ import annotations

import logging
import os
import posixpath
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from hubofhubs.ports.context import VectorHit
from ..core.adapters_bases import VectorStoreBase
from hubofhubs.recall.walk import prune_dirnames
from .walk_settings import effective_skip_dirs
from .card_repository import CardRepository, FilesystemCardRepository
from .card_store_identity import (
    BOOTSTRAP_META_FILE,
    check_readable,
    EMBEDDED,
    ensure_store_id,
    is_merged,
    read_meta,
    store_id_of,
    update_identity,
    vector_location_of,
)
from .card_store_vectors import (
    ReaderFactory,
    RemoteMountReaders,
    compatibility,
    embedded_reason,
)

log = logging.getLogger("quest-ai-runner.card-mounts")

CARDS_DIR_NAME = ".quest-context"
MOUNT_ID_SEP = ":"
# Bound on how many parent directories ancestor discovery walks up.
MAX_ANCESTOR_LEVELS = 12

# A store id no store has: "the reader may see card points of no store at all".
NO_STORE = "st_none"

MODE_COPY = "copy"
MODE_MOUNT = "mount"


def mount_mode_from_env(env: Optional[Dict[str, str]] = None) -> str:
    """``QAR_CARD_MOUNT``: ``mount`` (read allowed stores in place) or ``copy`` (the legacy
    copy-import; the DEFAULT, so an existing deployment is unchanged until it opts in)."""
    raw = ((os.environ if env is None else env).get("QAR_CARD_MOUNT") or "").strip().lower()
    return MODE_MOUNT if raw in ("1", "true", "yes", "on", "mount") else MODE_COPY


def mounted_card_id(store_id: str, card_id: str) -> str:
    return f"{store_id}{MOUNT_ID_SEP}{card_id}"


# --------------------------------------------------------------------------------------------
# Mount records and discovery
# --------------------------------------------------------------------------------------------

@dataclass
class Mount:
    """One store a corpus reads in place."""
    store_id: str
    cards_dir: str          # absolute, last known location of the owner's card directory
    root: str = ""          # absolute, last known corpus root of the owner (its meta is authoritative)
    vector_space: str = ""  # owner's declared vector space ("" = unknown)
    vector_location: Optional[Dict[str, Any]] = None  # owner's recorded vector_location (None = unknown)

    def as_record(self) -> Dict[str, str]:
        return {"store_id": self.store_id, "cards_dir": self.cards_dir, "root": self.root}


@dataclass
class MountOutcome:
    mounts: List[Mount] = field(default_factory=list)
    refused: List[Tuple[str, str]] = field(default_factory=list)       # (cards_dir, reason)
    # Roots whose subtree the reader must not index, whether or not the mount was granted.
    claimed_roots: List[Path] = field(default_factory=list)
    # Every store found around the reader, mounted or not, as (cards_dir, kind).
    found: List[Tuple[str, str]] = field(default_factory=list)


def claims_subtree(directory: Path) -> bool:
    """Whether ``directory`` is the root of a card store, i.e. its subtree has an owner.

    A store is recognised by its card directory, and an UNREADABLE card directory counts: judging
    "no readable bootstrap_meta.json" as "no store" is how a permission error used to make a corpus
    index a subtree that belongs to somebody else, then believe it had full context.
    """
    cards = directory / CARDS_DIR_NAME
    try:
        if not cards.is_dir():
            return False
        if not os.access(cards, os.R_OK | os.X_OK):
            return True
        if not (cards / BOOTSTRAP_META_FILE).is_file():
            return False
        # A store merged into a wider one no longer owns its subtree (`context merge-into`).
        return not is_merged(str(cards))
    except OSError:
        return True


def discover_nested_stores(walk_root: Path, own_cards_dir: Path, skip_dirs: Set[str],
                           gate: Any = None) -> List[Path]:
    """Every bootstrapped store BELOW ``walk_root``, flat (a store inside a store is found too).

    Returns each store's corpus ROOT (the directory holding its card directory). The reader mounts
    them side by side rather than through each other, so a card is reachable by exactly one path.
    A folder the ``gate`` (``context_risk_gate.folder_gate``) turned away is not entered, so no
    store beneath it is found, claimed or mounted.
    """
    found: List[Path] = []
    try:
        own = own_cards_dir.resolve()
        for dirpath, dirnames, _files in os.walk(walk_root):
            current = Path(dirpath).resolve()
            if current == own:
                dirnames[:] = []
                continue
            if gate is not None and gate.excludes_dir(current):
                dirnames[:] = []
                continue
            if current != walk_root.resolve() and claims_subtree(current):
                found.append(current)
            prune_dirnames(dirnames, current=current, base_skip=skip_dirs)
    except Exception:  # noqa: BLE001
        log.debug("nested store discovery failed", exc_info=True)
    return found


def discover_ancestor_stores(walk_root: Path, gate: Any = None) -> List[Path]:
    """Every bootstrapped store ABOVE ``walk_root`` (nearest first), as corpus roots.

    A store that is one of a series of dated copies, which the ``gate`` turned away, is left out."""
    found: List[Path] = []
    try:
        current = walk_root.resolve().parent
        for _ in range(MAX_ANCESTOR_LEVELS):
            if claims_subtree(current) and not (gate is not None and gate.ancestor_blocked(current)):
                found.append(current)
            if current.parent == current:
                break
            current = current.parent
    except Exception:  # noqa: BLE001
        pass
    return found


def resolve_mounts(own_cards_dir: str, walk_root: Path,
                   skip_dirs: Optional[Set[str]] = None, gate: Any = None) -> MountOutcome:
    """Decide which stores around ``walk_root`` this process mounts, and which it must leave alone.

    Every nested and ancestor store is CLAIMED (its subtree is not the reader's to index). Each
    claimed store is then mounted only if this process can read it (``check_readable``); the rest
    are logged and simply contribute no cards.
    """
    out = MountOutcome()
    skip = skip_dirs if skip_dirs is not None else effective_skip_dirs(walk_root)
    own_resolved = os.path.realpath(own_cards_dir)
    nested = discover_nested_stores(walk_root, Path(own_cards_dir), skip, gate)
    ancestors = discover_ancestor_stores(walk_root, gate)
    for kind, roots in (("nested", nested), ("ancestor", ancestors)):
        for store_root in roots:
            cards_dir = store_root / CARDS_DIR_NAME
            if os.path.realpath(str(cards_dir)) == own_resolved:
                continue
            if kind == "nested":
                out.claimed_roots.append(store_root)
            out.found.append((str(cards_dir), kind))
            decision = check_readable(str(cards_dir))
            meta = read_meta(str(cards_dir)) if decision.allowed else {}
            if not decision.allowed:
                out.refused.append((str(cards_dir), decision.reason))
                if kind == "ancestor":
                    # Access is what this process can read. A store above the root that it cannot
                    # read is invisible to it, not a claim on its files: the files under this root
                    # are this corpus's own, and the wider store (whose owner CAN read them) is the
                    # one that mounts or indexes them, never the other way round.
                    log.info("context index: the store above this corpus at %s is not readable by "
                             "this process (%s); ignoring it.", str(store_root), decision.reason)
                    continue
                log.warning("card mount NOT SERVED for the %s store at %s: %s. This corpus will have "
                            "no cards for that subtree and will not index it itself.",
                            kind, store_root.name or str(store_root), decision.reason)
                continue
            out.mounts.append(Mount(
                store_id=str(meta["store_id"]), cards_dir=str(cards_dir),
                root=str(meta.get("root") or store_root),
                vector_space=str(meta.get("vector_space") or ""),
                vector_location=vector_location_of(meta)))
    return out


# --------------------------------------------------------------------------------------------
# Reading a mounted card
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Rebase:
    """How a path relative to a mounted store's root is expressed relative to the reader's root."""
    mount_root: str
    own_root: Optional[str]

    def label(self) -> str:
        """A human-readable prefix ("proj", "..") for logs and docs; not used to resolve paths."""
        if not self.own_root:
            return Path(self.mount_root).as_posix()
        rel = os.path.relpath(self.mount_root, self.own_root)
        return "" if rel == "." else Path(rel).as_posix()


def mount_prefix(mount_root: str, own_root: Optional[str]) -> Rebase:
    return Rebase(mount_root=mount_root, own_root=own_root)


def rebase_path(path: str, rebase: Rebase) -> Optional[str]:
    """``path`` (relative to the owner's root) as the reader sees it, or None when it falls outside
    the reader's root (an ancestor store's card may reference files the reader does not have).

    Resolved against the two real roots, so ``..`` segments cancel against the reader's own
    directory names: an ancestor's ``lane/a.py`` is the reader's ``a.py`` when the reader is ``lane``.
    """
    if not path:
        return None
    if posixpath.isabs(path):
        return path
    absolute = os.path.normpath(os.path.join(rebase.mount_root, path))
    if not rebase.own_root:
        return Path(absolute).as_posix()
    rel = os.path.relpath(absolute, rebase.own_root)
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return Path(rel).as_posix()


def view_card(card: Dict[str, Any], mount: Mount, prefix: Rebase) -> Optional[Dict[str, Any]]:
    """The reader's read-only view of one mounted card, or None when it has nothing for the reader.

    A COPY OF THE DICT in memory only: files are rebased to the reader's root, the id becomes
    ``store_id:card_id``, and nothing is written. Cards that are themselves copies imported from
    another store are skipped, since the original is mounted directly.
    """
    try:
        prov = card.get("provenance")
        if isinstance(prov, dict) and prov.get("imported_from"):
            return None
        cid = str(card.get("id") or "")
        if not cid:
            return None
        out = dict(card)
        files: List[Any] = []
        for fe in card.get("files", []) or []:
            if isinstance(fe, dict):
                new = rebase_path(str(fe.get("path") or ""), prefix)
                if new is None:
                    continue
                files.append({**fe, "path": new})
            elif isinstance(fe, str):
                new = rebase_path(fe, prefix)
                if new is not None:
                    files.append(new)
        if card.get("files") and not files:
            return None
        out["files"] = files
        out["id"] = mounted_card_id(mount.store_id, cid)
        out["mounted_from"] = {"store_id": mount.store_id, "card_id": cid}
        return out
    except Exception:  # noqa: BLE001
        return None


class MountedCardRepository:
    """A ``CardRepository``: the reader's own cards plus read-only views of mounted stores.

    Reads (``load_all``/``read``) merge both. Writes and deletes are passed to the reader's own
    repository, except when the id is a mounted card's: those are REFUSED (False) and logged, which
    is the structural guarantee that a reading lane never writes into a store it mounts.
    ``revision()`` includes each owner's own change marker, so the reader never serves a stale view.
    """

    def __init__(self, own: CardRepository, own_cards_dir: str,
                 own_root: Callable[[], Optional[str]],
                 mounts: Callable[[], List[Mount]]) -> None:
        self.own = own
        self.own_cards_dir = own_cards_dir
        self.own_root = own_root
        self.mounts_source = mounts
        self.repos: Dict[str, FilesystemCardRepository] = {}
        self.warned: Set[Tuple[str, str]] = set()
        self.last_views: Dict[str, Tuple[Mount, Rebase]] = {}
        self.lock = threading.Lock()

    # -- which mounts are live right now -----------------------------------------------------

    def repo_for(self, mount: Mount) -> FilesystemCardRepository:
        repo = self.repos.get(mount.cards_dir)
        if repo is None:
            repo = FilesystemCardRepository(mount.cards_dir)
            self.repos[mount.cards_dir] = repo
        return repo

    def warn_once(self, mount: Mount, reason: str) -> None:
        key = (mount.store_id, reason)
        if key not in self.warned:
            self.warned.add(key)
            log.warning("card mount of store %s is not served: %s. The reader has no cards for it.",
                        mount.store_id, reason)

    def live_mounts(self) -> List[Tuple[Mount, Rebase]]:
        """``[(mount, prefix)]`` for every recorded mount that is readable and allowed NOW."""
        live: List[Tuple[Mount, Rebase]] = []
        own_root = self.own_root()
        for mount in self.mounts_source():
            decision = check_readable(mount.cards_dir)
            if not decision.allowed:
                self.warn_once(mount, f"{decision.reason} (moved? re-run the bootstrap or "
                                      f"migrate-imports to refresh the recorded location)")
                continue
            meta = read_meta(mount.cards_dir)
            if str(meta.get("store_id") or "") != mount.store_id:
                self.warn_once(mount, "the directory now holds a different store")
                continue
            mount.root = str(meta.get("root") or mount.root)
            mount.vector_space = str(meta.get("vector_space") or "")
            mount.vector_location = vector_location_of(meta)
            live.append((mount, mount_prefix(mount.root, own_root)))
        return live

    # -- CardRepository ------------------------------------------------------------------------

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        cards = dict(self.own.load_all())
        views: Dict[str, Tuple[Mount, Rebase]] = {}
        for mount, prefix in self.live_mounts():
            for card in self.repo_for(mount).load_all().values():
                view = view_card(card, mount, prefix)
                if view is not None:
                    cards[view["id"]] = view
                    views[view["id"]] = (mount, prefix)
        with self.lock:
            self.last_views = views
        return cards

    def is_mounted(self, card_id: str) -> bool:
        if MOUNT_ID_SEP not in card_id:
            return False
        sid = card_id.split(MOUNT_ID_SEP, 1)[0]
        return any(m.store_id == sid for m in self.mounts_source())

    def read(self, card_id: str) -> Optional[Dict[str, Any]]:
        if not self.is_mounted(card_id):
            return self.own.read(card_id)
        sid, cid = card_id.split(MOUNT_ID_SEP, 1)
        for mount, prefix in self.live_mounts():
            if mount.store_id == sid:
                raw = self.repo_for(mount).read(cid)
                return view_card(raw, mount, prefix) if isinstance(raw, dict) else None
        return None

    def write(self, card_id: str, card: Dict[str, Any]) -> bool:
        if self.is_mounted(card_id):
            log.warning("refused a write to mounted card %s: mounted stores are read-only", card_id)
            return False
        return self.own.write(card_id, card)

    def delete(self, card_id: str) -> bool:
        if self.is_mounted(card_id):
            log.warning("refused a delete of mounted card %s: mounted stores are read-only", card_id)
            return False
        return self.own.delete(card_id)

    def exists(self, card_id: str) -> bool:
        if self.is_mounted(card_id):
            return self.read(card_id) is not None
        return self.own.exists(card_id)

    def revision(self) -> Any:
        """The own stamp plus, per mounted store, the OWNER's change marker and meta file stamp.

        This is the whole freshness story: when a mounted store's owner writes a card (or moves its
        root), this changes, the store reloads, and the reader sees the owner's current state.
        """
        stamps: List[Any] = []
        for mount in self.mounts_source():
            try:
                meta_stamp = (Path(mount.cards_dir) / BOOTSTRAP_META_FILE).stat().st_mtime_ns
            except OSError:
                meta_stamp = 0
            stamps.append((mount.store_id, self.repo_for(mount).revision(), meta_stamp))
        return (self.own.revision(), tuple(stamps))


# --------------------------------------------------------------------------------------------
# Vector arm: query the owner's existing points, embed nothing for them
# --------------------------------------------------------------------------------------------

class StoreFencedVectorStore(VectorStoreBase):
    """Wraps a vector store so the card arm sees exactly the stores this reader may see.

    * ``search`` restricts CARD points to the reader's own ``store_id`` plus the live mounts whose
      ``vector_space`` does not contradict the reader's, and rebases a mounted hit's paths and id to the reader's
      terms. The vector server answers anyone who can reach it, so this filter is the only fence the
      vector arm has: a store the reader did not mount is never returned, and the task associations
      the reader's own runs record are attributed to its own store and visible to nobody else.
    * ``sync``/``upsert`` accept only the reader's own items: an item carrying another store's id is
      dropped, so a reader can never write into a mounted store's points.
    """

    def __init__(self, inner: Any, store: Any, vector_space: str = "",
                 include_unattributed: Optional[bool] = None,
                 reader_factory: Optional[ReaderFactory] = None) -> None:
        self.inner = inner
        self.store = store              # a FileContextStore: own_store_id() / live_mounts()
        self.vector_space = vector_space
        # Opt-in (QAR_VECTOR_UNATTRIBUTED=1) for a single-tenant vector store whose task
        # associations predate the fence and are all this reader's own.
        self.include_unattributed = (
            include_unattributed if include_unattributed is not None
            else (os.getenv("QAR_VECTOR_UNATTRIBUTED") or "").strip().lower() in ("1", "true", "yes"))
        self.unsupported_warned = False
        # Read-only handles on OTHER Qdrant backends that mounted stores publish their points on.
        self.remote = RemoteMountReaders(reader_factory)

    def classify(self, mount: Mount) -> str:
        """Where a mounted store's points are, relative to this reader's own vector store.

        ``same``: queried through this reader's own store with the store fence (the shared-server
        case, and any mount whose owner recorded no location: unchanged behaviour). ``remote``:
        the owner recorded a different server or collection prefix, queried read-only. ``other``:
        the owner's points are in an embedded Qdrant in a directory that is not this reader's.
        """
        rec = mount.vector_location
        mine = getattr(self.inner, "location", None)
        if not rec or mine is None:
            return "same"
        loc = str(rec.get("location") or "")
        if loc == EMBEDDED:
            theirs = str(rec.get("path") or "")
            own_path = str(getattr(self.inner, "path", "") or "")
            if not theirs or (mine == EMBEDDED and own_path
                              and os.path.realpath(theirs) == os.path.realpath(own_path)):
                return "same"
            return "other"
        prefix = str(rec.get("collection_prefix") or "")
        if loc == mine and (not prefix or prefix == getattr(self.inner, "collection_prefix", prefix)):
            return "same"
        return "remote"

    def visible_store_ids(self) -> Tuple[List[str], Dict[str, Tuple[Mount, Rebase]]]:
        own = self.store.own_store_id()
        ids: List[str] = [own] if own else []
        by_id: Dict[str, Tuple[Mount, Rebase]] = {}
        for mount, prefix in self.store.live_mounts():
            by_id[mount.store_id] = (mount, prefix)
            if self.classify(mount) != "same":
                continue    # served by search_remote (or by keyword search alone)
            # Queried unless BOTH sides declare a vector space and they differ (a different server
            # or collection holds the owner's points, so there is nothing to find there).
            if self.vector_space and mount.vector_space and mount.vector_space != self.vector_space:
                continue
            ids.append(mount.store_id)
        return ids, by_id

    def rewrite_hit(self, hit: VectorHit, by_id: Dict[str, Tuple[Mount, Rebase]]) -> VectorHit:
        """A mounted store's hit expressed in the reader's terms (id and paths)."""
        payload = dict(hit.payload or {})
        sid = str(payload.get("store_id") or "")
        if sid and sid in by_id:
            mount, prefix = by_id[sid]
            paths = [p for p in (rebase_path(str(x), prefix)
                                 for x in payload.get("paths") or []) if p]
            payload["paths"] = paths
            payload["mounted_from"] = {"store_id": sid}
            bare = str(hit.id)
            bare = bare[len("card:"):] if bare.startswith("card:") else bare
            bare = bare[len(sid) + 1:] if bare.startswith(sid + MOUNT_ID_SEP) else bare
            return VectorHit(id=f"card:{mounted_card_id(sid, bare)}",
                             score=hit.score, text=hit.text, payload=payload)
        return hit

    def search_remote(self, query: str, by_id: Dict[str, Tuple[Mount, Rebase]],
                      top_k: int) -> List[VectorHit]:
        """Hits from mounts whose points are on another backend. READ-ONLY; never raises.

        The query is embedded with THIS reader's query embedder once, and only when some mount
        needs it. A mount that cannot be queried (embedded elsewhere, incompatible embedder,
        unreachable server) is logged once and left to the keyword arm: nothing is copied or
        embedded locally for it.
        """
        out: List[VectorHit] = []
        qvec: Optional[List[float]] = None
        embedded_query = False
        for mount, _prefix in list(by_id.values()):
            kind = self.classify(mount)
            if kind == "same":
                continue
            rec = mount.vector_location or {}
            sid = mount.store_id
            if kind == "other":
                self.remote.log_once(("other", sid), "mounted store %s: vector arm skipped, %s; its "
                                     "cards are served by keyword search only", sid, embedded_reason(rec))
                continue
            if not embedded_query:
                embedded_query = True
                try:
                    qvec = self.inner.embed_query(query)
                except Exception:  # noqa: BLE001
                    qvec = None
            verdict = compatibility(rec, query_dim=len(qvec) if qvec else None,
                                    reader_embedder=str(getattr(self.inner, "embedder_id", "") or ""))
            if not verdict.ok:
                self.remote.log_once(("incompatible", sid, verdict.reason),
                                     "mounted store %s: vector arm skipped, %s; its cards are served "
                                     "by keyword search only", sid, verdict.reason)
                continue
            loc = str(rec.get("location") or "")
            pfx = str(rec.get("collection_prefix") or "qar_ctx")
            reader = self.remote.reader_for(loc, pfx)
            if reader is None:
                continue
            try:
                hits = reader.query(qvec or [], sid, top_k=top_k)
            except Exception as exc:  # noqa: BLE001
                self.remote.mark_down(loc, pfx, exc)
                continue
            out.extend(self.rewrite_hit(h, by_id) for h in hits)
        return out

    def search(self, query: str, *, scope: Optional[Dict[str, Any]] = None,
               top_k: int = 8) -> List[VectorHit]:
        try:
            ids, by_id = self.visible_store_ids()
            if not ids:
                if not self.unsupported_warned:
                    log.warning("this store has no store_id and no readable mounts, so the vector "
                                "arm returns no card points; run `context store-info --init`")
                ids = [NO_STORE]  # fenced with nothing mounted: card points of NO store are visible
            try:
                hits = self.inner.search(query, scope=scope, top_k=top_k, store_ids=ids,
                                         own_store_id=self.store.own_store_id() or NO_STORE,
                                         include_unattributed=self.include_unattributed)
            except TypeError:
                if not self.unsupported_warned:
                    self.unsupported_warned = True
                    log.warning("the vector store does not support store_ids filtering; mounted "
                                "stores are served by the keyword arm only")
                hits = self.inner.search(query, scope=scope, top_k=top_k)
            out: List[VectorHit] = [self.rewrite_hit(h, by_id) for h in hits]
            remote_hits = self.search_remote(query, by_id, top_k)
            if remote_hits:
                # One ranking across both backends: scores are comparable because the embedder
                # was checked compatible with the one that produced the remote vectors.
                out = sorted(out + remote_hits, key=lambda h: h.score, reverse=True)[:max(top_k, 1)]
            return out
        except Exception:  # noqa: BLE001
            log.debug("StoreFencedVectorStore.search failed", exc_info=True)
            return []

    def own_only(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        own = self.store.own_store_id()
        kept = []
        for item in items or []:
            sid = str((item.get("payload") or {}).get("store_id") or "")
            if sid and own and sid != own:
                log.warning("refused to write vector item %s for store %s from this reader",
                            item.get("id"), sid)
                continue
            kept.append(item)
        return kept

    def attributed(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """``own_only`` items, each stamped with this reader's store id when it has none.

        What a reader produces (a task association, a card embedding) is attributed to the reader's
        own store, which is what keeps it out of every other store's fenced searches.
        """
        own = self.store.own_store_id()
        out = []
        for item in self.own_only(items):
            payload = dict(item.get("payload") or {})
            if own and not payload.get("store_id"):
                payload["store_id"] = own
            out.append({**item, "payload": payload})
        return out

    def upsert(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> None:
        self.inner.upsert(self.attributed(items), scope=scope)

    def sync(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> int:
        return self.inner.sync(self.attributed(items), scope=scope)

    def count(self, *, scope: Optional[Dict[str, Any]] = None) -> int:
        return self.inner.count(scope=scope)

    def evict_oldest(self, n: int, *, scope: Optional[Dict[str, Any]] = None, ts_key: str = "ts") -> int:
        return self.inner.evict_oldest(n, scope=scope, ts_key=ts_key)


# --------------------------------------------------------------------------------------------
# migrate-imports
# --------------------------------------------------------------------------------------------

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


def copy_origin_root(card: Dict[str, Any], own_root: str) -> Optional[str]:
    prov = card.get("provenance")
    rel = str(prov.get("imported_from") or "") if isinstance(prov, dict) else ""
    if not rel:
        return None
    return os.path.normpath(os.path.join(own_root, rel))


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
    outcome = resolve_mounts(cards_dir, Path(own_root), skip, gate=gate)
    plan.mounts = outcome.mounts
    plan.refused = outcome.refused
    mounted_roots = {os.path.realpath(m.root) for m in outcome.mounts}
    for store_root in [*discover_nested_stores(Path(own_root), Path(cards_dir), skip, gate),
                       *discover_ancestor_stores(Path(own_root), gate)]:
        if not store_id_of(str(store_root / CARDS_DIR_NAME)):
            plan.needs_store_id.append(str(store_root / CARDS_DIR_NAME))
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
        from ..core.vector_scopes import CARD_SEED_SCOPE
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
        from ..core.vector_scopes import CARD_SEED_SCOPE
        plan.pruned_points = vector_store.delete_items(plan.orphan_point_ids, scope=CARD_SEED_SCOPE)
    plan.applied = True
    return plan
