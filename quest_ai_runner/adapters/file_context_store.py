"""FileContextStore -- a stdlib-only ContextAssembler backed by per-card JSON files.

Cards are the source of truth: one JSON file per card under a configurable ``cards_dir``.
The store selects relevant cards by keyword overlap with the task text, checks each pinned
file's freshness (sha256 + mtime; git blob SHA when a repo_root is set and git is available),
renders fresh cards into a ``context_view`` string, and flags stale files. ``record()``
upserts a card keyed by a stable slug of the task and re-pins file fingerprints.

No LLM calls, no third-party imports -- stdlib only (json, hashlib, os, re, subprocess,
pathlib, ast, math, etc.).

Card JSON schema (matches docs/context-assembly.md exactly):
  {
    "id": "subsystem-or-hash",
    "keywords": ["chat", "ai", "conversation"],
    "summary": "what this subsystem is and how it is wired",
    "files": [
      {"path": "rel/path.py", "git_sha": "...", "mtime": 1700000000.0,
       "sha256": "...", "why": "entry point", "symbols": ["run", "execute"],
       "last_used_ts": 1700000000.0, "use_count": 3}
    ],
    "conventions": ["pointer to a rule that applies"],
    "provenance": {
      "created_by_task": "...", "model": "...",
      "created_at": "...", "last_verified_at": "..."
    },
    "usage_count": 0,
    "last_outcome": "met|failed|unknown",
    "managed_fields": ["name", "description"],   # optional: fields only the card's WRITER may set
    "managed_items": ["<content item id>"]       # optional: items only the card's WRITER may edit
  }

``managed_fields`` / ``managed_items`` are how a CONSUMER-MANAGED card (one derived from the
consumer's own source of truth and rewritten whenever that source changes) protects the parts it
owns from the card-update API, while still letting a learning updater ADD content to it. See
``_update_card_inner``.

A file entry's ``mtime``/``sha256``/``git_sha`` are FINGERPRINTS (has this file changed since we
captured it), never usage. ``usage_count``/``last_outcome`` are CARD-level (was this card used).
``last_used_ts``/``use_count`` on a file entry or a content item are PER-SOURCE usage recency (was
THIS source used), which is what lets a card's cold sources sink below its hot ones.
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from esoi import implementation
from ..core.adapters import ContextAssemblerBase
from ._walk import effective_skip_dirs
from .card_content_render import (
    MAX_CARD_CONTENT_ITEMS as _MAX_CARD_CONTENT_ITEMS,
    MAX_CARD_REF_CHARS as _MAX_CARD_REF_CHARS,
    MAX_CARD_REF_ITEM_CHARS as _MAX_CARD_REF_ITEM_CHARS,
    MAX_CARD_REFS as _MAX_CARD_REFS,
    content_item_text as _content_item_text,
    dedupe_content as _dedupe_content,
    mark_items_used as _mark_items_used,
    normalize_content as _normalize_content,
    rank_content_by_recency_relevance as _rank_content_by_recency_relevance,
    tokenize as _tokenize,
)
from .card_repository import CardRepository, FilesystemCardRepository
from .card_store_identity import is_merged as is_merged_store, read_meta as read_store_meta
from .card_store_mounts import MODE_MOUNT, Mount, MountedCardRepository, mount_mode_from_env
from .tfdfidf_sampling import extract_terms as tfdfidf_extract_terms

from .context_bootstrap_support import (
    _BOOTSTRAP_MAX_BYTES,
    _BOOTSTRAP_META_FILE,
    _BOOTSTRAP_VERSION,
    _BOOTSTRAP_WORKERS_DEFAULT,
    _MAX_MERGED_KEYWORDS,
    _SOURCE_EXTS,
    _TFDFIDF_VERSION,
    _bootstrap_max_cards,
    _bootstrap_max_files,
    _bootstrap_workers,
    _card_file_paths,
    _log,
    _merge_card_group,
    _read_bootstrap_meta,
    _select_representative_files,
    _write_bootstrap_meta,
)
from .context_card_ceilings import (
    _FILE_SET_DEDUP_JACCARD,
    apply_card_ceilings,
    card_folder_key,
    card_is_too_thin,
    card_preference_key,
    collapse_cards_by_file_set,
    path_is_data_file,
)
from .context_folder_review import (
    _FOLDER_REVIEW_FILE,
    _ai_instruction_files_in,
    _dir_file_counts,
    _duplicate_folders,
    _folder_review,
    _has_kept_child,
    _is_excluded_folder,
    _nested_vcs_dirs,
    _prune_dead_cards,
    drop_verdicts_under_final_exclusions,
    final_exclusion_covering,
    verdict_is_final,
    verdict_is_pinned,
)
from .context_card_extraction import (
    _RECENCY_BOOST_MAX_DEFAULT,
    _build_rich_summary,
    _card_slug,
    _extract_file_snippet,
    _extract_leading_comment,
    _extract_symbols,
    _extract_text_description,
    _managed_names,
    _numbered_tree,
    _read_head,
    _read_whole_if_small,
    _resolve_file_refs,
    _summarize_snippet,
    _trim_content_by_recency,
)
from .context_card_dedup import _is_prompt_echo, _record_summary
from .context_store_origins import OriginsMixin
from .context_store_cards import CardsMixin
from .context_store_assembly import AssemblyMixin
from .context_store_bootstrap import BootstrapMixin
from .context_store_bootstrap_walk import BootstrapWalkMixin




@implementation("select-context", "file")
class FileContextStore(OriginsMixin, CardsMixin, AssemblyMixin, BootstrapMixin, BootstrapWalkMixin, ContextAssemblerBase):
    """Stdlib-only ContextAssembler backed by per-card JSON files.

    Constructor args:
      cards_dir         -- directory where card JSON files live (created on first write).
      repo_root         -- optional path to a git repo root for git-blob-SHA staleness checks
                          and as the default walk root for ``bootstrap()``.
                          Best-effort, optional: if git is unavailable the check is skipped.
      max_cards_in_view -- maximum number of cards included in a single assembled context view.
      auto_bootstrap    -- when True (default), the first ``assemble()`` call on an empty
                          store triggers ``bootstrap()`` once, best-effort, if a repo root is
                          known.  The guard fires only once per instance.
      card_repository   -- optional ``CardRepository`` that owns where/how cards persist. When
                          omitted the store builds a ``FilesystemCardRepository(cards_dir)`` (the
                          default per-card-JSON-files behavior, byte-for-byte). Injecting a
                          repository (e.g. a database-backed one) keeps ALL card logic here while
                          swapping only the persistence.
      quest_folder_map  -- optional ``{goal_or_quest_id: folder}`` (e.g.
                          ``RunnerConfig.quest_folder_map``). When ``assemble()`` is called with a
                          ``meta`` whose ``goal_id`` or ``quest_id`` (goal_id checked first)
                          matches a key here, cards pinning files under that folder get a BOOST
                          (see ``_QUEST_FOLDER_BOOST``) so a run already known to be about that
                          quest grounds preferentially on its linked folder. Entries whose folder
                          isn't under ``repo_root`` are ignored (logged once at construction).
      reuse_nested_cards -- when True (default; env ``QAR_REUSE_NESTED_CARDS=0`` to disable),
                          ``bootstrap()``/``refresh_stale()`` reuse any sub-corpus that already has
                          its own completed bootstrap (a ``.quest-context/bootstrap_meta.json``
                          under a subdirectory) instead of re-running LLM topic discovery on the
                          same files. E.g. a wider corpus root (``~/hq``) bootstrapping over a
                          narrower one already indexed by its own store (``~/hq/.../product``)
                          imports that store's cards wholesale, at zero extra LLM cost.

    Persistence boundary
    --------------------
    All raw card reads/writes/deletes go through a ``CardRepository`` (see
    ``card_repository.py``). The default ``FilesystemCardRepository`` stores one
    ``<cards_dir>/<id>.json`` file per card; a consumer can inject any other repository to
    persist cards elsewhere. The store keeps every bit of card LOGIC (selection / IDF / recency /
    the card-update API / ``export_for_embedding`` / bootstrap) and only delegates PERSISTENCE.

    In-memory card cache
    --------------------
    ``_load_all()`` fetches and JSON-parses every card via ``repo.load_all()``.  For large repos this
    would make every ``assemble()`` call O(all-cards).  To avoid that, the
    store keeps a lazily-populated in-memory cache of ``{card_id: card_dict}``.

    Invalidation is two-pronged:

    1. **Write-path dirty flag** -- ``record()`` and ``bootstrap()`` set
       ``_cache_dirty = True`` immediately after writing.  The next
       ``_load_all()`` call notices the flag, clears it, and reloads from the repo.
       This guarantees that a ``record()`` followed by ``assemble()`` in the same
       process always sees the newly written card.

    2. **External-change detector** -- on every ``_load_all()`` call the store
       checks the repository's cheap ``revision()`` change-stamp (for the filesystem
       repo: the maximum child mtime and the file count of ``cards_dir``).  If it
       changed since the last load, the cache is reloaded unconditionally.  This
       catches cards written by other processes or agents sharing the same store.
    """

    def __init__(
        self,
        cards_dir: str,
        *,
        repo_root: Optional[str] = None,
        max_cards_in_view: int = 8,
        auto_bootstrap: bool = True,
        confidence_threshold: float = 9.0,
        dry_run: bool = False,
        provider: Any = None,
        model: Optional[str] = None,
        reference_resolvers: Optional[Dict[str, Any]] = None,
        max_card_refs: int = _MAX_CARD_REFS,
        max_card_ref_chars: int = _MAX_CARD_REF_CHARS,
        card_repository: Optional[CardRepository] = None,
        recency_boost_max: float = _RECENCY_BOOST_MAX_DEFAULT,
        quest_folder_map: Optional[Dict[str, str]] = None,
        reuse_nested_cards: Optional[bool] = None,
        mount_mode: Optional[str] = None,
    ) -> None:
        self._cards_dir = Path(cards_dir)
        # Card PERSISTENCE is pluggable behind a CardRepository. Default: per-card JSON files under
        # cards_dir (byte-for-byte the prior behavior). A consumer may inject a database-backed repo.
        self._repo: CardRepository = card_repository or FilesystemCardRepository(cards_dir)
        # An INJECTED repository may be SHARED (the Quest card backend lists every card the lane can
        # read, other lanes' and other team members' included). Only then does card ownership matter:
        # see ``_is_own_card``. With the default per-store filesystem repository every card is ours.
        self._injected_repo: bool = card_repository is not None
        self._origin_cache: Optional[Tuple[str, Set[str]]] = None
        self._repo_root = Path(repo_root).resolve() if repo_root else None
        # STORE MOUNTING (see card_store_mounts). In ``mount`` mode a corpus reads the bootstrapped
        # stores around it IN PLACE and read-only, instead of copying their cards under new ids.
        # ``copy`` (the default, QAR_CARD_MOUNT) is the legacy copy-import, so a deployment changes
        # nothing until it opts in. ``_own_repo`` is the only thing this store ever writes to;
        # ``_repo`` additionally serves the mounted views (and refuses writes addressed to them).
        self._own_repo: CardRepository = self._repo
        self._mount_mode = (mount_mode or mount_mode_from_env()).strip().lower()
        if self._mount_mode == MODE_MOUNT and card_repository is not None:
            # Expected under the Quest card backend (cards are remote, there is nothing to mount):
            # info there, a warning for any other injected repository.
            (_log.info if type(card_repository).__name__ == "QuestApiCardRepository" else _log.warning)(
                "card mounting needs the default filesystem card repository; an injected "
                "card_repository (%s) owns card storage, so mounting is OFF for this store",
                type(card_repository).__name__)
            self._mount_mode = "copy"
        self._mounts: List[Mount] = []
        self._mounts_stamp: Any = "unset"
        self._mounted_repo: Optional[MountedCardRepository] = None
        if self._mount_mode == MODE_MOUNT:
            self._mounted_repo = MountedCardRepository(
                self._own_repo, str(self._cards_dir),
                own_root=self._effective_root,
                mounts=self.current_mounts,
            )
            self._repo = self._mounted_repo
        # Precompute {quest_id: repo-root-relative POSIX prefix} once, so per-assemble() lookup is
        # a plain dict get. A folder outside repo_root can't be matched against card file paths
        # (which are stored relative to repo_root), so such entries are dropped with a warning.
        self._quest_folder_map: Dict[str, str] = {}
        if quest_folder_map:
            if self._repo_root is None:
                _log.warning("quest_folder_map given but no repo_root configured; ignoring it")
            else:
                for qid, folder in quest_folder_map.items():
                    try:
                        rel = Path(folder).resolve().relative_to(self._repo_root)
                        self._quest_folder_map[str(qid)] = rel.as_posix()
                    except (ValueError, OSError):
                        _log.warning(
                            "quest_folder_map entry %s -> %s is not under repo_root %s; ignoring",
                            qid, folder, self._repo_root,
                        )
        self._max_cards = max_cards_in_view
        self._auto_bootstrap = auto_bootstrap
        self._dry_run = dry_run
        # Reuse a sub-corpus's already-bootstrapped cards during bootstrap() instead of
        # re-discovering them (see _discover_nested_card_dirs / _import_nested_cards). Default ON:
        # it is pure filesystem reuse (no LLM call), matching the "reuse before re-exploring"
        # principle. QAR_REUSE_NESTED_CARDS=0 opts a deployment out.
        self._reuse_nested_cards = (
            reuse_nested_cards
            if reuse_nested_cards is not None
            else os.getenv("QAR_REUSE_NESTED_CARDS", "1").strip().lower() not in ("0", "false", "no")
        )
        # Optional ModelProvider for LLM-based card relevance filtering.
        # When wired, IDF-selected candidates are re-ranked and filtered by the LLM
        # so only cards genuinely relevant to the task are injected.
        self._provider = provider
        # Resolved model ID to use for LLM card filtering (e.g. "balanced" tier resolved by config).
        self._filter_model = model
        # CONFIDENCE GATE (the never-worse-by-construction lever). A card is only injected when
        # its IDF match score clears this floor AND it is fresh. A weak/ambiguous match injects
        # NOTHING, so the run is plain Claude Code (the baseline). The system can therefore only
        # ADD a confident grounding or stay equal to the baseline; it never asserts a low-
        # confidence guess that could cost the agent a wasted glance. Set to 0.0 to inject any
        # positive match (old behaviour).
        self._confidence_threshold = confidence_threshold
        # RECENCY/USAGE ranking boost cap (keyword arm). Clamped to >= 0.0; 0.0 disables the boost.
        # Applied only to the ranking score, never to the confidence gate (see _recency_boost_factor).
        self._recency_boost_max = max(0.0, float(recency_boost_max))
        # Set to True once the lazy bootstrap has been attempted (success or failure).
        self._bootstrap_done: bool = False
        # SHUTDOWN SIGNAL for background indexing. Bootstrap/refresh are designed to run in a
        # background thread (see config._bootstrap_if_needed), and a walk over a big corpus (with a
        # ``git hash-object`` per file) can easily outlive whatever started it. A background thread
        # that outlives its owner is a defect, not just a test problem: it keeps burning I/O and
        # spawning subprocesses for a store nobody will read again, and its stray ``git`` calls land
        # in whatever the process does next. ``close()`` sets this; the bootstrap loops and the git
        # fingerprint helper check it and return promptly. Never set in the normal in-process path,
        # so production indexing is unchanged.
        self._closed = threading.Event()

        # --- Source-agnostic CONTENT resolution config -------------------------------------
        # Recency-bound limits for resolving a card's ``content`` items during assemble().
        self._max_card_refs = max_card_refs
        self._max_card_ref_chars = max_card_ref_chars
        # The {type: ReferenceResolver} registry. The built-in ``file`` resolver reuses THIS store's
        # fresh-read path (``_read_file_fresh``); ``note`` is built-in; collection/conversation/query
        # are consumer-injected via ``reference_resolvers``. An un-wired type degrades to a graceful
        # unresolved-pointer line at render time (never an error). Local import avoids any cycle.
        try:
            from .reference_resolver import build_resolver_registry
            self._resolvers: Dict[str, Any] = build_resolver_registry(
                file_read_text=self._read_file_fresh,
                consumer_resolvers=reference_resolvers,
            )
        except Exception:  # noqa: BLE001 — resolver wiring must never break construction
            self._resolvers = {}

        # In-memory card cache: {card_id: card_dict} or None when not yet loaded.
        # (see register_reference_resolver below for adding a resolver after construction)
        self._cache: Optional[Dict[str, Dict[str, Any]]] = None
        # Per-card IDF term-weight maps, reused only while the card is the SAME object the cache
        # holds (a reload or rewrite yields a new dict, so it recomputes). Rebuilding all of them
        # every turn was ~5s of CPU on a 5,000-card store. See _scoring_index.
        self._term_weight_cache: Dict[str, Tuple[Dict[str, Any], Dict[str, float]]] = {}
        self._load_lock = threading.Lock()
        self._df_cache: Optional[Tuple[frozenset, Dict[str, int]]] = None
        # Dirty flag: set after any write so next _load_all() reloads from the repo.
        self._cache_dirty: bool = False
        # The repository revision (``repo.revision()``) captured at the last cache load. For the
        # filesystem repo this is the (max_child_mtime, file_count) stamp; for another repo it is
        # whatever opaque change-stamp that repo returns. _load_all() reloads when it changes. The
        # initial sentinel never matches a real revision, so the first _load_all() always loads.
        self._cache_dir_stamp: Any = (0.0, 0)
