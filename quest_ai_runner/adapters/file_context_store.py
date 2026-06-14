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
       "sha256": "...", "why": "entry point", "symbols": ["run", "execute"]}
    ],
    "conventions": ["pointer to a rule that applies"],
    "provenance": {
      "created_by_task": "...", "model": "...",
      "created_at": "...", "last_verified_at": "..."
    },
    "usage_count": 0,
    "last_outcome": "met|failed|unknown"
  }
"""
from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.adapters import AssembledContext, ContextAssemblerBase

# ---------------------------------------------------------------------------
# Bootstrap constants
# ---------------------------------------------------------------------------

# Directories to skip entirely during repo walk.
_SKIP_DIRS: Set[str] = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".eggs", ".mypy_cache", ".pytest_cache", ".quest-context",
}

# Source / text file extensions worth indexing.
_SOURCE_EXTS: Set[str] = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".rb", ".md", ".sh", ".yaml", ".yml", ".toml", ".json",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
    ".html", ".css", ".scss", ".less", ".txt", ".rst",
}

# Max file size to fingerprint/parse during bootstrap (512 KB).
_BOOTSTRAP_MAX_BYTES = 512 * 1024

# Regex for non-Python symbol extraction (function/class names).
_SYMBOL_RE = re.compile(
    r"""
    (?:^|\s)
    (?:
        function\s+([A-Za-z_][A-Za-z0-9_]*)        # JS/TS function declaration
        |class\s+([A-Za-z_][A-Za-z0-9_]*)          # class in most languages
        |def\s+([A-Za-z_][A-Za-z0-9_]*)            # Python/Ruby def (fallback)
        |func\s+([A-Za-z_][A-Za-z0-9_]*)           # Go func
        |export\s+(?:const|function|class)\s+([A-Za-z_][A-Za-z0-9_]*)  # TS/JS export
        |(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:function|\()  # JS arrow/func
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

# ---------------------------------------------------------------------------
# Short stopwords dropped from keyword tokenization (pure ASCII, lowercase).
# ---------------------------------------------------------------------------

_STOPWORDS: Set[str] = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "has", "have", "how", "i", "in", "is", "it", "its", "me", "my",
    "not", "of", "on", "or", "that", "the", "this", "to", "was", "we",
    "what", "when", "where", "which", "who", "will", "with", "you",
}
_MIN_TOKEN_LEN = 3  # tokens shorter than this are always dropped


def _tokenize(text: str) -> Set[str]:
    """Lowercase-tokenize ``text`` to a keyword set, dropping short tokens and stopwords."""
    raw = re.findall(r"[a-z0-9]+", text.lower())
    return {t for t in raw if len(t) >= _MIN_TOKEN_LEN and t not in _STOPWORDS}


def _card_slug(task_text: str) -> str:
    """Stable card id: first few keywords (sorted) + short sha256 digest of the text."""
    tokens = sorted(_tokenize(task_text))
    prefix = "-".join(tokens[:4]) if tokens else "card"
    digest = hashlib.sha256(task_text.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{prefix}-{digest}"


def _path_slug(rel_path: str) -> str:
    """Stable card id for a source file derived from its relative path.

    Uses the file path itself (lowercased, non-alphanumeric chars replaced with
    hyphens) plus a short digest so two paths whose names collapse identically
    still get distinct ids.
    """
    clean = re.sub(r"[^a-z0-9]+", "-", rel_path.lower()).strip("-") or "root"
    digest = hashlib.sha256(rel_path.encode("utf-8", errors="replace")).hexdigest()[:6]
    return f"{clean}-{digest}"


def _extract_symbols(file_path: Path, max_symbols: int = 30) -> List[str]:
    """Extract top-level symbol names from a source file. Never raises.

    For ``.py`` files uses the stdlib ``ast`` module to collect top-level and
    class-level ``FunctionDef``, ``AsyncFunctionDef``, and ``ClassDef`` names.
    For other languages applies a lightweight regex set.  Returns at most
    ``max_symbols`` names (first encountered wins).
    """
    symbols: List[str] = []
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if file_path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=str(file_path))
                for node in ast.walk(tree):
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        # Only collect top-level and class-level (depth <= 2) defs.
                        # ast.walk gives us everything; we filter by checking that
                        # the parent is the module or a class body.  A simpler
                        # heuristic: only add names whose col_offset == 0 (module
                        # level) or whose parent class has col_offset == 0.
                        if hasattr(node, "col_offset") and node.col_offset == 0:
                            symbols.append(node.name)
                            if len(symbols) >= max_symbols:
                                break
            except SyntaxError:
                # Fall back to regex for syntax-invalid Python files.
                pass
        if not symbols:
            for m in _SYMBOL_RE.finditer(text):
                name = next((g for g in m.groups() if g), None)
                if name:
                    symbols.append(name)
                    if len(symbols) >= max_symbols:
                        break
    except Exception:  # noqa: BLE001
        pass
    return symbols[:max_symbols]


class FileContextStore(ContextAssemblerBase):
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

    In-memory card cache
    --------------------
    ``_load_all()`` reads and JSON-parses every card on disk.  For large repos this
    would make every ``assemble()`` call O(all-cards-from-disk).  To avoid that, the
    store keeps a lazily-populated in-memory cache of ``{card_id: card_dict}``.

    Invalidation is two-pronged:

    1. **Write-path dirty flag** -- ``record()`` and ``bootstrap()`` set
       ``_cache_dirty = True`` immediately after writing.  The next
       ``_load_all()`` call notices the flag, clears it, and reloads from disk.
       This guarantees that a ``record()`` followed by ``assemble()`` in the same
       process always sees the newly written card.

    2. **External-change detector** -- on every ``_load_all()`` call the store
       checks two cheap stats: the maximum child mtime and the file count of
       ``cards_dir``.  If either changed since the last load, the cache is
       reloaded unconditionally.  This catches cards written by other processes
       or agents sharing the same ``cards_dir``.
    """

    def __init__(
        self,
        cards_dir: str,
        *,
        repo_root: Optional[str] = None,
        max_cards_in_view: int = 8,
        auto_bootstrap: bool = True,
        confidence_threshold: float = 3.0,
    ) -> None:
        self._cards_dir = Path(cards_dir)
        self._repo_root = Path(repo_root).resolve() if repo_root else None
        self._max_cards = max_cards_in_view
        self._auto_bootstrap = auto_bootstrap
        # CONFIDENCE GATE (the never-worse-by-construction lever). A card is only injected when
        # its IDF match score clears this floor AND it is fresh. A weak/ambiguous match injects
        # NOTHING, so the run is plain Claude Code (the baseline). The system can therefore only
        # ADD a confident grounding or stay equal to the baseline; it never asserts a low-
        # confidence guess that could cost the agent a wasted glance. Set to 0.0 to inject any
        # positive match (old behaviour).
        self._confidence_threshold = confidence_threshold
        # Set to True once the lazy bootstrap has been attempted (success or failure).
        self._bootstrap_done: bool = False

        # In-memory card cache: {card_id: card_dict} or None when not yet loaded.
        self._cache: Optional[Dict[str, Dict[str, Any]]] = None
        # Dirty flag: set after any write so next _load_all() reloads from disk.
        self._cache_dirty: bool = False
        # Snapshot of (max_child_mtime, file_count) at last cache load.
        self._cache_dir_stamp: Tuple[float, int] = (0.0, 0)

    # ------------------------------------------------------------------
    # Public API: ContextAssemblerBase implementation
    # ------------------------------------------------------------------

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        """Select and render relevant cards for ``task_text``. Never raises.

        On the first call, if ``auto_bootstrap`` is enabled and the cards
        directory is empty (or absent), triggers ``bootstrap()`` once to seed
        the store from the repo tree before scoring.  The guard fires only once
        per instance regardless of outcome.
        """
        try:
            self._maybe_auto_bootstrap()
        except Exception:  # noqa: BLE001
            pass
        try:
            return self._assemble_inner(task_text)
        except Exception:  # noqa: BLE001
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Upsert a card for this task and write it atomically. Never raises."""
        try:
            self._record_inner(task_text, outcome)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Public helper: O(1) invalidation index
    # ------------------------------------------------------------------

    def stale_cards_for(self, path: str) -> Set[str]:
        """Return card ids that pin ``path`` AND whose fingerprint for it is now stale.

        The index is rebuilt on each call from the on-disk cards (bypasses the
        in-memory cache so concurrent writes from other agents are always
        reflected). Best-effort: returns an empty set on any error.
        """
        try:
            card_ids: Set[str] = set()
            for card in self._load_all().values():
                for fe in card.get("files", []):
                    if fe.get("path") == path:
                        fp = self._fingerprint(path)
                        if fp.get("sha256") and fe.get("sha256") != fp["sha256"]:
                            card_ids.add(card["id"])
            return card_ids
        except Exception:  # noqa: BLE001
            return set()

    # ------------------------------------------------------------------
    # Cold-start bootstrap
    # ------------------------------------------------------------------

    def bootstrap(
        self,
        root: Optional[str] = None,
        *,
        max_files: int = 10000,
        max_cards: int = 5000,
    ) -> int:
        """Seed the cards store by walking a source tree. Never raises. Returns cards written.

        Creates ONE CARD PER SOURCE FILE found under ``root``.  Each card captures:

        - ``id``       -- a stable slug derived from the file's relative path.
        - ``keywords`` -- tokens from path segments plus extracted symbol names.
        - ``summary``  -- ``"{rel_path}: {first ~12 symbols}"`` (or just rel_path).
        - ``files``    -- that single file, fingerprinted, with its symbols attached.
        - ``provenance.created_by_task`` == "bootstrap".

        File-granular cards give the IDF scorer precise routing: a query
        containing a distinctive symbol or path term lands on exactly that
        file's card rather than a coarse module-level card.

        Symbol extraction uses ``ast`` for ``.py`` files and a small regex set
        for other languages.  Never raises on a parse error (that file is
        skipped/included without symbols).

        Idempotent: cards are upserted by id (existing cards for the same file
        are overwritten).  Total cards written is capped at ``max_cards``
        (default 5000).
        """
        try:
            return self._bootstrap_inner(root=root, max_files=max_files, max_cards=max_cards)
        except Exception:  # noqa: BLE001
            return 0

    def _maybe_auto_bootstrap(self) -> None:
        """Trigger bootstrap once if auto_bootstrap is on and the store is empty. Never raises."""
        if self._bootstrap_done:
            return
        self._bootstrap_done = True  # set before any work so a failure is still final
        if not self._auto_bootstrap:
            return
        root = self._repo_root
        if root is None:
            return
        # Only bootstrap when there are no existing cards.
        if self._cards_dir.exists() and any(
            e.suffix == ".json" and not e.name.startswith(".")
            for e in self._cards_dir.iterdir()
        ):
            return
        try:
            self._bootstrap_inner(root=str(root))
        except Exception:  # noqa: BLE001
            pass

    def _bootstrap_inner(
        self,
        root: Optional[str] = None,
        *,
        max_files: int = 10000,
        max_cards: int = 5000,
    ) -> int:
        """Actual bootstrap logic. May raise; callers wrap in try/except."""
        walk_root = Path(root).resolve() if root else self._repo_root
        if walk_root is None or not walk_root.is_dir():
            return 0

        # Resolve the cards_dir so we can skip it if it's inside the walk root.
        cards_dir_resolved = self._cards_dir.resolve()

        self._cards_dir.mkdir(parents=True, exist_ok=True)
        cards_written = 0
        file_count = 0

        # --- Pass 1: walk the tree and collect (rel_str, syms, keywords) for each file ---
        # We separate the walk from fingerprinting so we can fingerprint in parallel.
        # Each entry: (rel_str, syms, all_keywords)
        walk_entries: List[tuple] = []

        for dirpath, dirnames, filenames in os.walk(walk_root):
            if file_count >= max_files or len(walk_entries) >= max_cards:
                break
            current_dir = Path(dirpath).resolve()
            # Skip the cards directory itself to avoid indexing stored card JSON files.
            if current_dir == cards_dir_resolved:
                dirnames[:] = []
                continue
            # Prune skip dirs in-place so os.walk doesn't recurse into them.
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS
                and (current_dir / d).resolve() != cards_dir_resolved
            ]
            for fname in filenames:
                if file_count >= max_files or len(walk_entries) >= max_cards:
                    break
                fpath = Path(dirpath) / fname
                if fpath.suffix not in _SOURCE_EXTS:
                    continue
                try:
                    if fpath.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                        continue
                except OSError:
                    continue

                file_count += 1
                rel = fpath.relative_to(walk_root)
                rel_str = str(rel)

                # Extract symbols from this single file.
                syms = _extract_symbols(fpath, max_symbols=30)
                syms = list(dict.fromkeys(syms))[:30]  # deduplicate

                # Keywords: path segment tokens + symbol names.
                seg_tokens = sorted(_tokenize(rel_str.replace("/", " ").replace("_", " ").replace(".", " ")))
                sym_tokens = [s.lower() for s in syms if len(s) >= _MIN_TOKEN_LEN]
                all_keywords = list(dict.fromkeys(seg_tokens + sym_tokens))[:50]

                walk_entries.append((rel_str, syms, all_keywords))

        # --- Pass 2: fingerprint all collected files in parallel ---
        # sha256 reads release the GIL; threads give a real speedup when many files
        # are being hashed.  Workers are bounded to min(8, n_files).  Order is preserved
        # (enumerate index -> result dict).  A failed fingerprint yields {} (never raises).
        fp_list: List[Dict[str, Any]] = [{}] * len(walk_entries)
        if walk_entries:
            n_workers = min(8, len(walk_entries))
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
                    idx_futures = {
                        pool.submit(self._fingerprint, rel_str): idx
                        for idx, (rel_str, _, _) in enumerate(walk_entries)
                    }
                    for fut in concurrent.futures.as_completed(idx_futures):
                        idx = idx_futures[fut]
                        try:
                            fp_list[idx] = fut.result()
                        except Exception:  # noqa: BLE001
                            fp_list[idx] = {}
            except Exception:  # noqa: BLE001
                # Fallback to serial if ThreadPoolExecutor fails unexpectedly.
                for idx, (rel_str, _, _) in enumerate(walk_entries):
                    try:
                        fp_list[idx] = self._fingerprint(rel_str)
                    except Exception:  # noqa: BLE001
                        fp_list[idx] = {}

        # --- Pass 3: write cards using the pre-computed fingerprints ---
        for (rel_str, syms, all_keywords), fp in zip(walk_entries, fp_list):
            if cards_written >= max_cards:
                break

            file_dict: Dict[str, Any] = {
                "path": rel_str,
                "sha256": fp.get("sha256", ""),
                "mtime": fp.get("mtime", 0.0),
                "git_sha": fp.get("git_sha", ""),
                "why": "",
                "symbols": syms,
            }

            # Short summary line.
            sym_sample = ", ".join(syms[:12])
            summary = f"{rel_str}: {sym_sample}" if sym_sample else rel_str

            card_id = _path_slug(rel_str)

            # Load existing card so we preserve usage_count / last_outcome if present.
            card_path = self._cards_dir / f"{card_id}.json"
            existing: Dict[str, Any] = {}
            if card_path.exists():
                try:
                    with open(card_path, "r", encoding="utf-8") as fh:
                        existing = json.load(fh)
                except Exception:  # noqa: BLE001
                    existing = {}

            card: Dict[str, Any] = {
                "id": card_id,
                "keywords": all_keywords,
                "summary": summary,
                "files": [file_dict],
                "conventions": [],
                "provenance": {
                    "created_by_task": "bootstrap",
                    "model": "",
                    "created_at": "",
                    "last_verified_at": "",
                },
                "usage_count": existing.get("usage_count", 0),
                "last_outcome": existing.get("last_outcome", "unknown"),
            }

            # Atomic write.
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self._cards_dir), prefix=".tmp_", suffix=".json"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(card, fh, indent=2, ensure_ascii=False)
                    fh.write("\n")
                os.replace(tmp_path, str(card_path))
                cards_written += 1
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        # Invalidate cache after all writes.
        self._cache_dirty = True
        return cards_written

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _card_searchable_terms(self, card: Dict[str, Any]) -> Set[str]:
        """Build the full searchable term set for a card (IDF universe).

        Includes tokens from: keywords, summary, each pinned file's path
        segments, and each file's symbols.  Lowercased, short/stopword-free.
        """
        parts: List[str] = []
        parts.extend(card.get("keywords", []))
        parts.append(card.get("summary", ""))
        for fe in card.get("files", []):
            parts.append(fe.get("path", "").replace("/", " ").replace("_", " "))
            parts.extend(fe.get("symbols", []))
        return _tokenize(" ".join(parts))

    def _assemble_inner(self, task_text: str) -> AssembledContext:
        task_kws = _tokenize(task_text)
        if not task_kws:
            return AssembledContext()

        cards = self._load_all()
        if not cards:
            return AssembledContext()

        # ---- IDF-weighted scoring ----
        # Build a term -> searchable set mapping for every card (computed once).
        card_term_sets: Dict[str, Set[str]] = {
            cid: self._card_searchable_terms(c) for cid, c in cards.items()
        }
        N = len(cards)

        # Compute document frequency per term across all cards.
        df: Dict[str, int] = {}
        for term_set in card_term_sets.values():
            for term in term_set:
                df[term] = df.get(term, 0) + 1

        # IDF(term) = log((N+1)/(df+1)) + 1  (smooth, always >= 1).
        def _idf(term: str) -> float:
            return math.log((N + 1) / (df.get(term, 0) + 1)) + 1.0

        # Score each card: sum of IDF for each query term present in the card's term set.
        # Tie-break by (usage_count DESC, last_verified_at DESC).
        scored: List[tuple] = []  # (-score, -usage_count, -last_verified_ts, card_dict)
        for cid, card in cards.items():
            card_terms = card_term_sets[cid]
            score = sum(_idf(t) for t in task_kws if t in card_terms)
            # CONFIDENCE GATE: only a match that clears the threshold is injected. A weak match
            # contributes NOTHING, so an uncertain query yields an empty context view and the run
            # falls back to plain Claude Code (never worse). This is what makes the layer dominate:
            # it adds a grounding only when confident, and otherwise equals the baseline.
            if score >= self._confidence_threshold:
                usage = card.get("usage_count", 0)
                verified_at = card.get("provenance", {}).get("last_verified_at", "") or ""
                scored.append((-score, -usage, -len(verified_at), verified_at, card))

        if not scored:
            return AssembledContext()

        # Sort: primary descending score, then tie-break descending usage_count,
        # then tie-break by presence of a verified_at string (longer = more recent).
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        top_cards = [x[4] for x in scored[: self._max_cards]]

        # Render each card, checking file freshness.
        # Collect every (card_index, file_entry) pair that needs a fingerprint check, then
        # compute all sha256 reads concurrently -- sha256 I/O releases the GIL so threads
        # help whenever a card set pins many files.  Order is preserved; a failed
        # fingerprint yields an empty dict (same as today, never raises).
        #
        # Build the flat list of (card_idx, fe) pairs to fingerprint.
        fp_jobs: List[tuple] = []  # (card_idx, fe_idx, fpath)
        for ci, card in enumerate(top_cards):
            for fi, fe in enumerate(card.get("files", [])):
                fp_jobs.append((ci, fi, fe.get("path", "")))

        # Dispatch fingerprint reads in parallel.
        fp_results: Dict[tuple, Dict[str, Any]] = {}  # (ci, fi) -> fp dict
        if fp_jobs:
            n_workers = min(8, len(fp_jobs))
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
                    futures = {
                        pool.submit(self._fingerprint, fpath): (ci, fi)
                        for ci, fi, fpath in fp_jobs
                    }
                    for fut in concurrent.futures.as_completed(futures):
                        key = futures[fut]
                        try:
                            fp_results[key] = fut.result()
                        except Exception:  # noqa: BLE001
                            fp_results[key] = {}
            except Exception:  # noqa: BLE001
                # ThreadPoolExecutor unavailable (shouldn't happen with stdlib, but guard anyway).
                for ci, fi, fpath in fp_jobs:
                    try:
                        fp_results[(ci, fi)] = self._fingerprint(fpath)
                    except Exception:  # noqa: BLE001
                        fp_results[(ci, fi)] = {}

        view_parts: List[str] = []
        card_ids: List[str] = []
        stale_list: List[str] = []

        for ci, card in enumerate(top_cards):
            card_id = card.get("id", "")
            card_ids.append(card_id)
            summary = card.get("summary", "(no summary)")
            files = card.get("files", [])

            file_lines: List[str] = []
            for fi, fe in enumerate(files):
                fpath = fe.get("path", "")
                stored_sha = fe.get("sha256", "")
                current_fp = fp_results.get((ci, fi), {})
                current_sha = current_fp.get("sha256", "")
                changed = (
                    (bool(stored_sha) and bool(current_sha) and current_sha != stored_sha)
                    or (bool(stored_sha) and not current_sha)  # file disappeared
                )
                if changed:
                    stale_list.append(fpath)
                    file_lines.append(f"  - {fpath} (changed since last capture)")
                else:
                    why = fe.get("why", "")
                    syms = fe.get("symbols", [])
                    sym_note = f" [{', '.join(syms[:5])}]" if syms else ""
                    file_lines.append(
                        f"  - {fpath}{sym_note}" + (f"  -- {why}" if why else "")
                    )

            file_block = "\n".join(file_lines) if file_lines else "  (no pinned files)"
            conventions = card.get("conventions", [])
            conv_block = (
                "\n".join(f"  * {c}" for c in conventions[:10]) if conventions else ""
            )
            part = f"### Card: {card_id}\n{summary}\n\nFiles:\n{file_block}"
            if conv_block:
                part += f"\n\nConventions:\n{conv_block}"
            view_parts.append(part)

        context_view = "\n\n---\n\n".join(view_parts)
        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=list(dict.fromkeys(stale_list)),  # deduplicate, preserve order
        )

    def _record_inner(self, task_text: str, outcome: Dict[str, Any]) -> None:
        card_id = _card_slug(task_text)
        card_path = self._cards_dir / f"{card_id}.json"

        # Load existing card or start fresh.
        if card_path.exists():
            try:
                with open(card_path, "r", encoding="utf-8") as fh:
                    card: Dict[str, Any] = json.load(fh)
            except Exception:  # noqa: BLE001 -- corrupt card: start fresh
                card = {}
        else:
            card = {}

        # Ensure required fields exist.
        card.setdefault("id", card_id)
        card.setdefault("keywords", sorted(_tokenize(task_text)))
        card.setdefault("summary", task_text[:200])
        card.setdefault("conventions", [])
        card.setdefault("usage_count", 0)
        card.setdefault("last_outcome", "unknown")
        card.setdefault("provenance", {
            "created_by_task": task_text[:100],
            "model": "",
            "created_at": "",
            "last_verified_at": "",
        })

        # Re-pin file fingerprints when outcome supplies a file list.
        file_paths: List[str] = outcome.get("files") or []
        if file_paths:
            existing_files: Dict[str, Dict[str, Any]] = {
                fe["path"]: fe for fe in card.get("files", []) if "path" in fe
            }
            refreshed: List[Dict[str, Any]] = []
            for fpath in file_paths:
                fp = self._fingerprint(fpath)
                entry = existing_files.get(fpath, {"path": fpath, "why": "", "symbols": []})
                entry = dict(entry)  # copy to avoid mutating the source dict
                entry["path"] = fpath
                entry["sha256"] = fp.get("sha256", "")
                entry["mtime"] = fp.get("mtime", 0.0)
                entry["git_sha"] = fp.get("git_sha", "")
                refreshed.append(entry)
            card["files"] = refreshed
        # else: keep existing file entries unchanged

        # Update usage and outcome.
        card["usage_count"] = card.get("usage_count", 0) + 1
        kind = outcome.get("kind")
        if kind in ("met", "failed", "unknown", "answer", "deep", "confirm"):
            # Normalize runner kind strings to the card schema vocabulary.
            card["last_outcome"] = kind if kind in ("met", "failed", "unknown") else {
                "answer": "met", "deep": "met", "confirm": "unknown"
            }.get(kind, "unknown")

        # Accept an optional timestamp for last_verified_at (avoids datetime.now in tests).
        ts = outcome.get("verified_at") or outcome.get("timestamp")
        if ts:
            card["provenance"]["last_verified_at"] = str(ts)

        # Atomic write via tmp + os.replace.
        self._cards_dir.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._cards_dir), prefix=".tmp_", suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(card, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, str(card_path))
            # Invalidate the in-memory cache so the next assemble() sees the new card.
            self._cache_dirty = True
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise  # re-raise so the outer try/except in record() catches it

    def _fingerprint(self, path: str) -> Dict[str, Any]:
        """Compute current sha256 + mtime + (optional) git_sha for a file path.

        Returns a dict with the fields that exist; missing/unreadable fields are omitted
        or set to empty string. Never raises.
        """
        result: Dict[str, Any] = {"sha256": "", "mtime": 0.0, "git_sha": ""}
        try:
            # Paths pinned from a run are relative to the corpus/repo root (that is what the
            # RetrievalAdapter and deep runner work in), NOT the process cwd. Resolve a relative
            # path against repo_root so freshness reads the real file rather than missing it.
            p = Path(path)
            if not p.is_absolute() and self._repo_root is not None:
                p = self._repo_root / path
            if not p.exists() or not p.is_file():
                return result
            stat = p.stat()
            result["mtime"] = stat.st_mtime
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
            result["sha256"] = h.hexdigest()
        except Exception:  # noqa: BLE001
            pass

        # Git blob SHA: best-effort, optional, fully wrapped.
        if self._repo_root is not None and result["sha256"]:
            try:
                proc = subprocess.run(
                    ["git", "-C", str(self._repo_root), "hash-object", path],
                    capture_output=True, text=True, timeout=5,
                )
                git_sha = proc.stdout.strip()
                if git_sha:
                    result["git_sha"] = git_sha
            except Exception:  # noqa: BLE001 -- git unavailable, no repo, any error: skip
                pass

        return result

    def _dir_stamp(self) -> Tuple[float, int]:
        """Cheap snapshot of cards_dir state: (max_child_mtime, file_count).

        Used to detect external writes (other agents / processes) without
        reading every card.  Returns (0.0, 0) if the directory does not exist
        or cannot be stat-ed.
        """
        try:
            if not self._cards_dir.exists():
                return (0.0, 0)
            max_mtime = 0.0
            count = 0
            for entry in self._cards_dir.iterdir():
                if entry.suffix == ".json" and not entry.name.startswith("."):
                    count += 1
                    try:
                        mt = entry.stat().st_mtime
                        if mt > max_mtime:
                            max_mtime = mt
                    except OSError:
                        pass
            return (max_mtime, count)
        except Exception:  # noqa: BLE001
            return (0.0, 0)

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all card JSON files from cards_dir. Returns {card_id: card_dict}.

        Uses an in-memory cache to avoid re-reading every card on every call.
        Invalidates the cache when:
        - ``_cache_dirty`` is set (after any local write via ``record()`` or
          ``bootstrap()``), OR
        - the directory's max-child-mtime or file count changed (external write).
        """
        current_stamp = self._dir_stamp()
        need_reload = (
            self._cache is None
            or self._cache_dirty
            or current_stamp != self._cache_dir_stamp
        )
        if not need_reload:
            return self._cache  # type: ignore[return-value]

        # Reload from disk.
        self._cache_dirty = False
        self._cache_dir_stamp = current_stamp

        if not self._cards_dir.exists():
            self._cache = {}
            return self._cache

        cards: Dict[str, Dict[str, Any]] = {}
        for entry in self._cards_dir.iterdir():
            if entry.suffix != ".json" or entry.name.startswith("."):
                continue
            try:
                with open(entry, "r", encoding="utf-8") as fh:
                    card = json.load(fh)
                card_id = card.get("id") or entry.stem
                cards[card_id] = card
            except Exception:  # noqa: BLE001 -- corrupt card: skip
                continue

        self._cache = cards
        return self._cache
