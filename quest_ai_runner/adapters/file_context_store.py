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
from ._walk import effective_skip_dirs, prune_dirnames

# ---------------------------------------------------------------------------
# Bootstrap constants
# ---------------------------------------------------------------------------

# Kept for backwards-compat: external code that imported _SKIP_DIRS directly
# still works, but the walk now uses effective_skip_dirs() which also reads
# the project's .gitignore.  Do not add entries here — update _walk.py instead.
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

# Patterns that identify test files for down-weighting.
_TEST_PATH_RE = re.compile(
    r"(?:^|/)tests?/"                   # in a tests/ or test/ directory
    r"|/test_[^/]+$"                    # filename starts with test_
    r"|/_?test\.[^/]+$"                 # filename is test.<ext>
    r"|[^/]+_test\.[^/]+$",            # filename ends with _test.<ext>
    re.IGNORECASE,
)

# Weight given to test-file cards vs source-file cards (used in summary metadata).
_TEST_FILE_WEIGHT = 0.5
_SOURCE_FILE_WEIGHT = 1.0

# Max length for a card summary built from docstrings/descriptions (~400 chars).
_SUMMARY_MAX_CHARS = 400

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


def _first_sentence(text: str, max_len: int = 120) -> str:
    """Return the first non-empty sentence/line of ``text``, capped at ``max_len`` chars."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            # Trim at the first sentence boundary (period + space) within the line.
            dot = line.find(". ")
            if 0 < dot < max_len:
                return line[: dot + 1]
            return line[:max_len]
    return ""


def _is_test_path(rel_path: str) -> bool:
    """Return True when *rel_path* looks like a test file."""
    return bool(_TEST_PATH_RE.search(rel_path))


def _extract_docstrings(file_path: Path) -> Dict[str, Any]:
    """Extract module docstring and top-level class/function docstrings from a .py file.

    Returns a dict::

        {
          "module": "<first sentence of module docstring>",
          "defs": [("ClassName", "<first sentence of docstring>"), ...],
        }

    Falls back to an empty dict on any error.  Never raises.
    """
    result: Dict[str, Any] = {"module": "", "defs": []}
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text, filename=str(file_path))
        # Module docstring.
        mod_doc = ast.get_docstring(tree) or ""
        result["module"] = _first_sentence(mod_doc)
        # Top-level class and function docstrings (col_offset == 0).
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if getattr(node, "col_offset", -1) == 0:
                    doc = ast.get_docstring(node) or ""
                    first = _first_sentence(doc)
                    result["defs"].append((node.name, first))
    except Exception:  # noqa: BLE001
        pass
    return result


def _extract_text_description(file_path: Path) -> str:
    """Extract the first heading + first paragraph from a .md/.rst/.txt file.

    Returns a single-line blurb, or an empty string on failure.  Never raises.
    """
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        heading = ""
        para_lines: List[str] = []
        in_para = False
        for line in lines:
            stripped = line.strip()
            # Detect heading: Markdown #/##/### or plain text ALL-CAPS / underline style.
            if not heading:
                if stripped.startswith("#"):
                    heading = stripped.lstrip("#").strip()
                    continue
                if stripped and stripped == stripped.upper() and len(stripped) > 3:
                    heading = stripped
                    continue
                if stripped and re.match(r"^[=\-]{3,}$", stripped):
                    # Underline; the heading was the previous line.
                    if para_lines:
                        heading = para_lines[-1]
                        para_lines = []
                    continue
            # First paragraph: non-empty lines after the heading.
            if heading or in_para:
                if stripped:
                    in_para = True
                    para_lines.append(stripped)
                elif in_para:
                    break  # end of first paragraph
        blurb_parts = []
        if heading:
            blurb_parts.append(heading)
        if para_lines:
            first_para = " ".join(para_lines)
            blurb_parts.append(_first_sentence(first_para, max_len=160))
        return ". ".join(p for p in blurb_parts if p)
    except Exception:  # noqa: BLE001
        return ""


def _extract_leading_comment(file_path: Path) -> str:
    """Extract leading block/line comments from non-Python, non-doc source files.

    Returns a short blurb from the first comment block, or empty on failure.  Never raises.
    """
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        comment_lines: List[str] = []
        for line in lines[:20]:
            stripped = line.strip()
            # Skip blank lines at the top.
            if not stripped and not comment_lines:
                continue
            # Single-line comment styles: //, #, --, *, /*, */
            m = re.match(r"^(?://|#|--|/\*|\*/?)\s*(.*)", stripped)
            if m:
                content = m.group(1).strip()
                if content:
                    comment_lines.append(content)
                    if len(comment_lines) >= 3:
                        break
            elif comment_lines:
                break  # end of leading comment block
            else:
                break  # no comment at all
        blurb = " ".join(comment_lines)
        return _first_sentence(blurb, max_len=200) if blurb else ""
    except Exception:  # noqa: BLE001
        return ""


def _build_rich_summary(
    rel_path: str,
    file_path: Path,
    syms: List[str],
) -> Tuple[str, str]:
    """Build a rich (summary, description) pair for a file card.

    ``summary`` -- compact, readable blurb (~400 chars) for the card's ``summary`` field.
    ``description`` -- the full orientation text to embed in the vector arm (module
                       docstring + key symbol docstrings).

    For ``.py`` files: module docstring first line + class/fn names with their
    docstring first lines.  For ``.md/.rst/.txt``: first heading + first paragraph.
    For other code: leading block comment.  Falls back to symbol-name list when
    nothing richer is available.  Never raises.
    """
    suffix = file_path.suffix.lower()
    description = ""
    summary_parts: List[str] = [rel_path]

    try:
        if suffix == ".py":
            ds = _extract_docstrings(file_path)
            mod_doc = ds.get("module", "")
            defs = ds.get("defs", [])
            if mod_doc:
                summary_parts.append(mod_doc)
                description = mod_doc
            # Build "Key: ClassName -- docstring, fn -- docstring" section.
            def_notes: List[str] = []
            for name, doc in defs[:8]:
                if doc:
                    def_notes.append(f"{name} -- {doc}")
                else:
                    def_notes.append(name)
            if def_notes:
                key_blurb = "Key: " + ", ".join(def_notes)
                summary_parts.append(key_blurb)
                description = (description + ". " + key_blurb).strip(". ")
            elif syms:
                # No docstrings: fall back to symbol list.
                sym_list = ", ".join(syms[:12])
                summary_parts.append(sym_list)
                if not description:
                    description = sym_list
        elif suffix in (".md", ".rst", ".txt"):
            blurb = _extract_text_description(file_path)
            if blurb:
                summary_parts.append(blurb)
                description = blurb
            elif syms:
                summary_parts.append(", ".join(syms[:12]))
        else:
            blurb = _extract_leading_comment(file_path)
            if blurb:
                summary_parts.append(blurb)
                description = blurb
            elif syms:
                summary_parts.append(", ".join(syms[:12]))
    except Exception:  # noqa: BLE001
        # Absolute fallback: just symbols.
        if syms:
            summary_parts.append(", ".join(syms[:12]))

    # Assemble and cap.
    summary = " -- ".join(p for p in summary_parts if p)
    if len(summary) > _SUMMARY_MAX_CHARS:
        summary = summary[: _SUMMARY_MAX_CHARS - 1] + "…"
    return summary, description


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

    def refresh_stale(self, root: Optional[str] = None) -> int:
        """Re-index only files whose content changed since the last bootstrap.

        Walks the source tree but skips writing any card whose file sha256 still
        matches the stored fingerprint.  New files get a card; changed files get
        an updated card; deleted files keep their old card (stale but harmless).

        Designed to be called from a background thread at startup so the context
        index stays warm without blocking the caller.  Never raises; returns the
        number of cards written (0 = everything was already up to date).
        """
        try:
            return self._bootstrap_inner(root=root, skip_unchanged=True)
        except Exception:  # noqa: BLE001
            return 0

    def _bootstrap_inner(
        self,
        root: Optional[str] = None,
        *,
        max_files: int = 10000,
        max_cards: int = 5000,
        skip_unchanged: bool = False,
    ) -> int:
        """Actual bootstrap logic. May raise; callers wrap in try/except."""
        walk_root = Path(root).resolve() if root else self._repo_root
        if walk_root is None or not walk_root.is_dir():
            return 0

        # Resolve the cards_dir so we can skip it if it's inside the walk root.
        cards_dir_resolved = self._cards_dir.resolve()
        skip_dirs = effective_skip_dirs(walk_root)

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
            prune_dirnames(dirnames, current=current_dir, base_skip=skip_dirs)
            # Also exclude the cards dir itself (it's internal state, not source).
            dirnames[:] = [
                d for d in dirnames
                if (current_dir / d).resolve() != cards_dir_resolved
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

                # Build rich summary + description (docstrings / headings / comments).
                rich_summary, description = _build_rich_summary(rel_str, fpath, syms)

                # Test-file flag and weight.
                is_test = _is_test_path(rel_str)
                weight = _TEST_FILE_WEIGHT if is_test else _SOURCE_FILE_WEIGHT

                # Keywords: path segment tokens + symbol names.
                seg_tokens = sorted(_tokenize(rel_str.replace("/", " ").replace("_", " ").replace(".", " ")))
                sym_tokens = [s.lower() for s in syms if len(s) >= _MIN_TOKEN_LEN]
                all_keywords = list(dict.fromkeys(seg_tokens + sym_tokens))[:50]

                walk_entries.append((rel_str, syms, all_keywords, rich_summary, description, is_test, weight))

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
                        for idx, (rel_str, _, _, _, _, _, _) in enumerate(walk_entries)
                    }
                    for fut in concurrent.futures.as_completed(idx_futures):
                        idx = idx_futures[fut]
                        try:
                            fp_list[idx] = fut.result()
                        except Exception:  # noqa: BLE001
                            fp_list[idx] = {}
            except Exception:  # noqa: BLE001
                # Fallback to serial if ThreadPoolExecutor fails unexpectedly.
                for idx, (rel_str, _, _, _, _, _, _) in enumerate(walk_entries):
                    try:
                        fp_list[idx] = self._fingerprint(rel_str)
                    except Exception:  # noqa: BLE001
                        fp_list[idx] = {}

        # --- Pass 3: write cards using the pre-computed fingerprints ---
        for (rel_str, syms, all_keywords, rich_summary, description, is_test, weight), fp in zip(walk_entries, fp_list):
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

            summary = rich_summary

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

            # Incremental refresh: skip files whose content hasn't changed.
            if skip_unchanged and existing:
                old_sha = (existing.get("files") or [{}])[0].get("sha256", "")
                new_sha = fp.get("sha256", "")
                if old_sha and new_sha and old_sha == new_sha:
                    continue

            card: Dict[str, Any] = {
                "id": card_id,
                "keywords": all_keywords,
                "summary": summary,
                "description": description,
                "is_test": is_test,
                "weight": weight,
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
        # Test-file cards are down-weighted by their stored ``weight`` (default 0.5) so that
        # a source file and its test file both match the same query, the source file ranks first.
        # Tie-break by (usage_count DESC, last_verified_at DESC).
        scored: List[tuple] = []  # (-score, -usage_count, -last_verified_ts, card_dict)
        for cid, card in cards.items():
            card_terms = card_term_sets[cid]
            base_score = sum(_idf(t) for t in task_kws if t in card_terms)
            # Apply weight: test files stored with weight=0.5 are penalised.
            card_weight = float(card.get("weight", _SOURCE_FILE_WEIGHT))
            score = base_score * card_weight
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

        # --- Context transparency: collect the file paths surfaced by this arm ----------------
        # One source entry per arm (keyword/IDF), listing the pinned file paths so the
        # orchestrator can emit "Context from: docstring cards (file1.py, file2.py)".
        _source_items: List[str] = []
        for card in top_cards:
            for fe in card.get("files", []):
                fp = fe.get("path", "")
                if fp and fp not in _source_items:
                    _source_items.append(fp)
        _sources = (
            [{"adapter": "keyword", "label": "docstring cards", "items": _source_items}]
            if _source_items else []
        )

        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=list(dict.fromkeys(stale_list)),  # deduplicate, preserve order
            sources=_sources,
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

    def export_for_embedding(self) -> List[Dict[str, Any]]:
        """Return one item per card suitable for ``VectorStore.sync()``.

        Each item is::

            {
              "id": "card:<card_id>",
              "text": <description when present, else summary>,
              "payload": {
                "paths": [file paths],
                "symbols": [symbol names],
                "summary": <summary>,
                "kind": "bootstrap",
              },
              "fingerprint": <sha256 hash of the card's file fingerprints,
                             changes only when file content changes>,
            }

        The ``fingerprint`` is a hash of the stored sha256 values for every file
        pinned by the card. When files change the card is refreshed by
        ``bootstrap()`` and the fingerprint changes, causing ``sync()`` to
        re-embed only those cards (AUTO-UPDATE). Unchanged cards are skipped.

        Builds the in-memory card cache if it has not been loaded yet. Returns
        ``[]`` on any error. Never raises.
        """
        try:
            import hashlib as _hl
            # Ensure the store is populated before exporting. This is important when
            # export_for_embedding is called from a parallel thread (e.g. by the vector
            # arm of a HybridContextAssembler) before the keyword arm has had a chance
            # to call assemble() and trigger its own auto-bootstrap.
            self._maybe_auto_bootstrap()
            cards = self._load_all()
            result: List[Dict[str, Any]] = []
            for card_id, card in cards.items():
                try:
                    # --- Text: prefer docstring-rich description, fall back to summary ---
                    text = card.get("description") or card.get("summary") or ""
                    if not text:
                        continue

                    # --- Payload ---
                    paths: List[str] = [
                        fe.get("path", "") for fe in card.get("files", []) if fe.get("path")
                    ]
                    symbols: List[str] = []
                    for fe in card.get("files", []):
                        symbols.extend(fe.get("symbols", []))
                    # Deduplicate symbols preserving order.
                    seen_syms: Set[str] = set()
                    unique_syms: List[str] = []
                    for s in symbols:
                        if s not in seen_syms:
                            seen_syms.add(s)
                            unique_syms.append(s)

                    payload: Dict[str, Any] = {
                        "paths": paths,
                        "symbols": unique_syms[:30],
                        "summary": card.get("summary", ""),
                        "kind": "bootstrap",
                    }

                    # --- Fingerprint: hash of all stored file sha256 values ---
                    # If file content changes -> bootstrap() rewrites the card with new
                    # sha256 values -> fingerprint changes -> sync() re-embeds.
                    fp_parts: List[str] = []
                    for fe in card.get("files", []):
                        s = fe.get("sha256") or ""
                        if s:
                            fp_parts.append(s)
                    if fp_parts:
                        fingerprint = _hl.sha256(
                            "|".join(sorted(fp_parts)).encode("utf-8")
                        ).hexdigest()[:16]
                    else:
                        # No file shas: use a hash of the text so re-bootstrap changes it.
                        fingerprint = _hl.sha256(text.encode("utf-8")).hexdigest()[:16]

                    result.append({
                        "id": f"card:{card_id}",
                        "text": text,
                        "payload": payload,
                        "fingerprint": fingerprint,
                    })
                except Exception:  # noqa: BLE001 — skip malformed card
                    continue
            return result
        except Exception:  # noqa: BLE001
            return []

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
