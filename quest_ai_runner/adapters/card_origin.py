"""Where a card came from, and the rules that keep one card from becoming many.

A card store that reuses the stores around it (nested below, an ancestor above) holds COPIES of their
cards. Copies used to be keyed by the id they had in the store they were copied from, so a copy of a
copy gained another namespace prefix on every hop, and two stores that import each other re-imported
each other's imports for ever (8,557 and 32,070 cards on one corpus, then 142,781 on another). What
stopped it was a guess about the DIRECTION a copy travelled, which any writer that dropped the
copy's ``provenance.imported_from`` silently undid.

The rule here does not depend on direction or on any provenance surviving:

  * Every copy records its ORIGIN: ``provenance.origin_store`` (the store that authored the card),
    ``origin_id`` (the id it has there) and ``origin_root`` (that store's root, relative to the
    store holding the copy).
  * A copy's id is ``<slug of origin_root>--<origin_id>``. It is a function of the origin alone, so a
    card that reaches a store by two routes is one id, and an id can never grow by hop.
  * A store never imports a card whose origin is itself, whichever way it came back.
  * No writer may store an id that repeats a namespace segment (``card_id_problem``).

Pure functions, stdlib only. The store logic that applies them is in ``context_store_bootstrap``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .card_store_identity import read_meta

log = logging.getLogger("quest-ai-runner.card-store")

NAMESPACE_SEP = "--"
# The vector store keys a card point ``card:<id>`` (or ``card:<store_id>:<id>``). That is an ITEM id,
# never a card id.
ITEM_PREFIX = "card:"
ORIGIN_STORE = "origin_store"
ORIGIN_ID = "origin_id"
ORIGIN_ROOT = "origin_root"
MAX_ID_CHARS = 200

# The slug of an import root (``_path_slug``): words joined by hyphens, then six hex digits.
NAMESPACE_PREFIX = re.compile(r"^(?:[a-z0-9]+-)+[0-9a-f]{6}$")
# ``context merge-into`` renames a colliding card ``<id>--m<8 hex of the source store id>``.
MERGE_RENAME = re.compile(r"--m[0-9a-f]{8}$")
STORE_QUALIFIER = re.compile(r"^st_[0-9a-f]{16}:")


# --------------------------------------------------------------------------------------------
# Card ids
# --------------------------------------------------------------------------------------------

def namespace_segments(card_id: str) -> List[str]:
    """The ``--`` separated parts of an id, in order."""
    return [part for part in str(card_id or "").split(NAMESPACE_SEP) if part]


def card_id_problem(card_id: str) -> str:
    """Why ``card_id`` may never be stored, or "" when it may.

    Two things can never be true of a card id. It never repeats a namespace segment (a repeated
    segment means the same import root appears twice, which is the signature of a card copied
    through a loop), and it is never a vector ITEM id (``card:<id>``): a card written under one is
    a second card nobody asked for, next to the one it was meant for.
    """
    text = str(card_id or "")
    if not text.strip():
        return "the id is empty"
    if text.startswith(ITEM_PREFIX):
        return f"the id starts with {ITEM_PREFIX!r}, which is how a vector point is keyed, not a card"
    parts = namespace_segments(text)
    seen: Set[str] = set()
    for part in parts:
        if part in seen:
            return f"the namespace segment {part!r} appears twice in the id"
        seen.add(part)
    return ""


def refuse(card_id: str) -> bool:
    """True, with an ERROR naming the id, when ``card_id`` may not be stored (``card_id_problem``).

    The one check every repository's ``write`` runs before it stores anything, so no writer can
    bring a looped or mis-keyed id into a store. It does not raise: a writer that promises not to
    raise gets its usual ``False`` back, and the ERROR says which id and why.
    """
    problem = card_id_problem(card_id)
    if problem:
        log.error("refusing to store the card %r: %s", str(card_id)[:120], problem)
    return bool(problem)


def is_store_qualified(card_id: str) -> bool:
    """Whether the id names a card of a MOUNTED store (``st_<16 hex>:<id>``), a read-only view here."""
    return bool(STORE_QUALIFIER.match(str(card_id or "")))


def card_id_of_item(item_id: str, own_store_id: str = "") -> str:
    """The card id a vector item id (``card:<id>`` or ``card:<store_id>:<id>``) stands for.

    Only the ``card:`` of a point id is removed. A store qualifier is removed too when it is THIS
    store's own id (its own points are keyed ``card:<own id>:<id>``); the qualifier of any other
    store stays, because ``<other id>:<id>`` is how a card mounted from that store is named here and
    the read-only guard must see it. An id that is not an item id comes back unchanged.
    """
    text = str(item_id or "")
    if not text.startswith(ITEM_PREFIX):
        return text
    bare = text[len(ITEM_PREFIX):]
    if own_store_id and bare.startswith(f"{own_store_id}:"):
        return bare[len(own_store_id) + 1:]
    return bare


def compounding_evidence(card_id: str) -> str:
    """Why this id is a compounded import copy, or "" when it is not. Readable from the id alone.

    An import namespaces the imported card's id with the source root's path, so a chain of imports
    leaves a chain of namespace segments: a repeated segment means the same root appears twice in one
    id, an interior ``root-`` hop means a store imported another store's own imports, and two or more
    namespace prefixes (``<words>-<6 hex>`` segments before the topic) mean the card was imported more
    than once. A single leading namespace is a legitimate one-level import and is NOT evidence.
    """
    segments = namespace_segments(card_id)
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
    prefixes = [s for s in segments[:-1] if NAMESPACE_PREFIX.match(s)]
    if len(prefixes) >= 2:
        return f"{len(prefixes)} import namespace prefixes ({', '.join(prefixes)})"
    return ""


def root_slug(origin_root: str) -> str:
    """The namespace a copy's id starts with: a stable slug of the origin's root path."""
    from .context_card_extraction import _path_slug  # local: that module pulls in the card renderer
    return _path_slug(origin_root)


def import_id(origin_root: str, origin_id: str) -> str:
    """The id of a copy of card ``origin_id`` of the store rooted at ``origin_root``.

    ``origin_root`` is relative to the store that holds the copy. A function of the origin only.
    """
    text = f"{root_slug(origin_root)}{NAMESPACE_SEP}{origin_id}"
    if len(text) <= MAX_ID_CHARS:
        return text
    return text[: MAX_ID_CHARS - 9] + "-" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]


# --------------------------------------------------------------------------------------------
# Origins
# --------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Origin:
    """The authoring store of a card, the id it has there, and that store's root (relative)."""

    store: str
    id: str
    root: str


def path_ref(cards_dir: str) -> str:
    """A stable stand-in for a store identity when the store has none yet: a digest of its path."""
    try:
        resolved = str(Path(cards_dir).resolve())
    except OSError:
        resolved = str(cards_dir)
    return "path:" + hashlib.sha1(resolved.encode("utf-8", "replace")).hexdigest()[:12]


def store_ref(cards_dir: str) -> str:
    """The identity a store's cards are attributed to: its ``store_id``, else a digest of its path."""
    return str(read_meta(cards_dir).get("store_id") or "") or path_ref(cards_dir)


def provenance_of(card: Dict[str, Any]) -> Dict[str, Any]:
    prov = card.get("provenance")
    return prov if isinstance(prov, dict) else {}


def is_copy(card: Dict[str, Any]) -> bool:
    """Whether the card is a copy of another store's card (either generation of the record)."""
    prov = provenance_of(card)
    return bool(prov.get("imported_from") or prov.get(ORIGIN_STORE))


INDEX_HASH = "index_hash"            # provenance key: a hash of what the index pass wrote (see ``index_fingerprint``)
# Fields of a card that change without anybody editing it: the index's own bookkeeping and the heat a retrieval leaves.
VOLATILE_FILE_KEYS = frozenset({"sha256", "mtime", "git_sha", "tfdfidf_terms", "tfdfidf_v", "last_used_ts", "use_count"})
VOLATILE_CARD_KEYS = frozenset({"id", "provenance", "origin", "usage_count", "last_outcome"})


def index_fingerprint(card: Dict[str, Any]) -> str:
    """A short hash of what an index pass wrote on this card: everything except the fields that change on their
    own (the id, fingerprints of the files, retrieval heat, the usage count, the origin stamp, the provenance). A summary,
    keywords, a link or any field added by hand is in it, so the hash no longer matches once somebody edits one.
    Stored by the pass in ``provenance.index_hash``; a card without one is judged as before."""
    body = {k: v for k, v in card.items() if k not in VOLATILE_CARD_KEYS}
    body["files"] = [({k: v for k, v in fe.items() if k not in VOLATILE_FILE_KEYS} if isinstance(fe, dict) else fe)
                     for fe in card.get("files") or []]
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def stamp_index_hash(card: Dict[str, Any]) -> None:
    """Record what the index pass wrote, so a later edit by hand shows. Call it on the finished card."""
    prov = card.get("provenance")
    if not isinstance(prov, dict):
        prov = {}
        card["provenance"] = prov
    prov.pop(INDEX_HASH, None)
    prov[INDEX_HASH] = index_fingerprint(card)


def matches_index_hash(card: Dict[str, Any]) -> bool:
    """Whether the pass recorded what it wrote on this card and the card still is exactly that."""
    recorded = provenance_of(card).get(INDEX_HASH)
    return bool(recorded) and recorded == index_fingerprint(card)


def file_heat(card: Dict[str, Any]) -> int:
    """How many times retrievals have rendered this card's files (``files[].use_count``, summed)."""
    total = 0
    for fe in card.get("files") or []:
        if isinstance(fe, dict):
            try:
                total += max(0, int(fe.get("use_count") or 0))
            except (TypeError, ValueError):
                continue
    return total


def is_run_brief_record(card: Dict[str, Any]) -> bool:
    """Whether ``card`` is a run's brief that was stored as if it were knowledge. Structure only.

    A queued run's text is a composed brief (standing instructions, scope, history): input to the
    run, not something learned. ``record`` used to store one card per run (or per thread) keyed on
    that text, so such cards hold no file, no note, no convention and nothing an updater or a person
    wrote (no ``name``/``description``), and their ``provenance`` is either the run's task id (a
    thread record) or the first 100 characters of a multi-paragraph request. A card with any file,
    content, convention, managed field or scope-free editor field is never one. Selection ignores
    these; the words of the card are not read.
    """
    if card.get("files") or card.get("content") or card.get("conventions"):
        return False
    if card.get("name") or card.get("description") or card.get("managed_by") \
            or card.get("managed_fields") or card.get("managed_items"):
        return False
    prov = provenance_of(card)
    if prov.get("task_id"):
        return True
    made_by = str(prov.get("created_by_task") or "")
    return "\n\n" in made_by


def protection(card: Dict[str, Any], check_edits: bool = True) -> str:
    """Why this card may never be moved out of the index by a tidy, the card limit or a retirement, or "".

    A card is movable only while it is exactly what an index pass wrote and nothing else touched:
    written by the bootstrap (``created_by_task`` is ``bootstrap``; a card with no provenance was written
    by a person), not used (the card's ``usage_count``, or retrievals that rendered its files), not owned by
    another system, with no notes added by a run (a ``content`` key: a pass never writes one, the card-update
    API always does), no quest tags and no conventions, and, when the pass recorded what it wrote
    (``provenance.index_hash``), unchanged since: a summary, keywords, links or any field somebody edited by
    hand no longer matches. ``check_edits=False`` skips that last comparison (it costs a hash of the card) for
    a first, cheap look. Anything else holds something that no file can regenerate. This is the one
    definition; the tidy, the limit, the retirement of copies and ``scripts/quarantine_cards.py`` all ask it.
    """
    if card.get("managed_by") or card.get("managed_fields") or card.get("managed_items"):
        return "managed by another system"
    try:
        if int(card.get("usage_count") or 0) > 0 or file_heat(card) > 0:
            return "has been used"
    except (TypeError, ValueError):
        pass
    created_by = str(provenance_of(card).get("created_by_task") or "")
    if created_by != "bootstrap":
        return f"provenance {created_by}" if created_by else "written by a person (no provenance)"
    if "content" in card:
        return "has notes or edits added by a run"
    if card.get("scope_tags"):
        return "carries quest tags"
    if card.get("conventions"):
        return "carries conventions"
    if check_edits:
        recorded = provenance_of(card).get(INDEX_HASH)
        if recorded and recorded != index_fingerprint(card):
            return "edited by hand since the index wrote it"
    return ""


def exported_origin(card: Dict[str, Any], source_ref: str, source_root: Path) -> Optional[Origin]:
    """The origin a card offered by ``source_root``'s store is a copy of, relative to that root.

    ``None`` when the card cannot be traced to an origin and so must not be offered at all:

      * a copy made before origins were recorded, more than one hop deep (its id carries more than
        one namespace and the authoring store is not recorded anywhere): it is re-imported from its
        real source once the store that holds it refreshes;
      * an id that carries a namespace but no record of where it came from (a copy that lost its
        provenance): it is nobody's original.

    A copy made before origins were recorded, ONE hop deep, is read from its id and the store it
    points at. An original is its own origin.
    """
    prov = provenance_of(card)
    cid = str(card.get("id") or "")
    if prov.get(ORIGIN_STORE) and prov.get(ORIGIN_ID):
        return Origin(str(prov[ORIGIN_STORE]), str(prov[ORIGIN_ID]), str(prov.get(ORIGIN_ROOT) or "."))
    imported_from = str(prov.get("imported_from") or "")
    if imported_from:
        head = f"{root_slug(imported_from)}{NAMESPACE_SEP}"
        rest = cid[len(head):] if cid.startswith(head) else ""
        if not rest or NAMESPACE_SEP in rest:
            return None
        far = os.path.normpath(os.path.join(str(source_root), imported_from))
        return Origin(store_ref(os.path.join(far, ".quest-context")), rest, imported_from)
    if NAMESPACE_SEP in cid and not MERGE_RENAME.search(cid):
        return None
    return Origin(source_ref, cid, ".")


def origin_relative_to(origin: Origin, source_root: Path, importer_root: Path) -> str:
    """Where the origin store's root is, relative to ``importer_root`` (``.`` when it is the same folder).

    ``origin.root`` is relative to the store that offered the card (``source_root``). Resolved against
    the real folders, so a way up and back down (``../product/runner`` from inside ``product``) is the
    same place as the way straight there (``runner``), and a card reached by two routes gets one id.
    """
    far = os.path.realpath(os.path.join(str(source_root), origin.root or "."))
    rel = os.path.relpath(far, os.path.realpath(str(importer_root)))
    return Path(rel).as_posix()


def origin_fields(origin_store: str, origin_id: str, origin_root: str) -> Dict[str, str]:
    return {ORIGIN_STORE: origin_store, ORIGIN_ID: origin_id, ORIGIN_ROOT: origin_root}


def file_set(card: Dict[str, Any]) -> frozenset:
    """The paths a card pins (entries may be plain strings or ``{"path": ...}`` dicts)."""
    out = set()
    for entry in card.get("files") or []:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if path:
            out.add(str(path))
    return frozenset(out)


def choose_imports(offered: List[Dict[str, Any]], own_cards: List[Dict[str, Any]],
                   skipped_for_own: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """The imports worth keeping out of everything the stores around offered, in offered order.

    One copy per id: the same card reached by two routes is one id (``import_id``), and the copy
    taken from its authoring store (``hops`` 0) beats one taken from another copy. One copy per file
    set: a copy that pins exactly the files a card of this store's own already pins, or that another
    copy pins, is the same card described twice; the one from the more deeply rooted store is kept
    (the store nearest the files wrote it), then the lower id. Nothing is judged by a score or a
    count: two cards pinning the same paths are one card. An import left out only because a card of
    this store's own pins its files is appended to ``skipped_for_own`` when that list is given.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for item in offered:
        held = best.get(item["id"])
        if held is None or item.get("hops", 0) < held.get("hops", 0):
            best[item["id"]] = item
    # A card of this store's own that is NOT marked as a copy but carries the id (and pins the files) of an
    # offered import is that import, written by an older version that did not record where it came from.
    # It must not shadow the import for ever as an "original": the import is written over it, keeping
    # what was added to it.
    unmarked_copies = {c.get("id") for c in own_cards
                       if c.get("id") in best and not is_copy(c) and file_set(c) == file_set(best[c["id"]])}
    own_sets = {file_set(c) for c in own_cards
                if not is_copy(c) and c.get("id") not in unmarked_copies} - {frozenset()}

    def rank(item: Dict[str, Any]):
        root = str((item.get("origin_fields") or {}).get(ORIGIN_ROOT) or ".")
        # A copy somebody added to loses to an untouched one pinning the same files: it is the one that
        # is joined onto the other and retired, so what is offered again each pass is what stays.
        return (1 if item.get("protected") else 0, item.get("hops", 0),
                -len([p for p in root.split("/") if p not in ("", "..", ".")]), item["id"])

    winner: Dict[frozenset, Dict[str, Any]] = {}
    for item in best.values():
        files = file_set(item)
        if files in own_sets:
            if skipped_for_own is not None:
                skipped_for_own.append(item)
            continue
        held = winner.get(files)
        if held is None or rank(item) < rank(held):
            winner[files] = item
    keep = {id(item) for item in winner.values()}
    return [item for item in best.values() if id(item) in keep]


# --------------------------------------------------------------------------------------------
# When a card was first written
# --------------------------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp_created_at(card: Dict[str, Any], existing: Optional[Dict[str, Any]]) -> None:
    """Record ``provenance.created_at`` (UTC, ISO) the first time a card is written. Additive.

    ``existing`` is the card already stored under this id, or ``None``/empty when there is none. A
    card that is already dated keeps its date; a card that exists WITHOUT one (written before dates
    were kept) stays undated, because stamping it now would claim an age it does not have.
    ``last_verified_at`` is never touched.
    """
    prov = card.get("provenance")
    if not isinstance(prov, dict):
        prov = {}
        card["provenance"] = prov
    if prov.get("created_at"):
        return
    if isinstance(existing, dict) and existing:
        prior = provenance_of(existing).get("created_at")
        if prior:
            prov["created_at"] = prior
        return
    prov["created_at"] = now_iso()
