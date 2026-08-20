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

import ast
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.adapters import AssembledContext, ContextAssemblerBase
from ._walk import effective_skip_dirs, prune_dirnames
from .card_content_render import (
    MAX_CARD_CONTENT_ITEMS as _MAX_CARD_CONTENT_ITEMS,
    MAX_CARD_REF_CHARS as _MAX_CARD_REF_CHARS,
    MAX_CARD_REF_ITEM_CHARS as _MAX_CARD_REF_ITEM_CHARS,
    MAX_CARD_REFS as _MAX_CARD_REFS,
    content_item_text as _content_item_text,
    dedupe_content as _dedupe_content,
    filter_content_by_time_range,
    mark_items_used as _mark_items_used,
    normalize_content as _normalize_content,
    rank_content_by_recency_relevance as _rank_content_by_recency_relevance,
    render_card_content,
    render_card_content_blocks,
    tokenize as _tokenize,
)
from .card_repository import CardRepository, FilesystemCardRepository, card_embed_text
from .tfdfidf_sampling import extract_terms as tfdfidf_extract_terms, select_representatives

_log = logging.getLogger("quest-ai-runner.context")

# Bootstrap ALGORITHM version. Bump this when the bootstrap/dedup logic changes in a way that
# makes previously-written cards stale (a re-index is warranted). ``config._bootstrap_if_needed``
# compares this against the stored ``bootstrap_meta.json`` version and re-bootstraps when the
# stored version is older. v2: LLM-based keyword-cluster dedup (replaced Jaccard file-overlap).
# v3: TF-DF-IDF sampling in Stage 1 & 2 (representative files + snippets instead of all paths).
# v4: per-file TF-DF-IDF term signatures from actual content stored in file entries (tfdfidf_terms).
_BOOTSTRAP_VERSION = 4

# Per-feature algorithm versions. Bump ONLY the version for the feature whose algorithm changed —
# an unrelated feature should never force a re-run. Each version is stored in the card/file-entry
# that carries the feature's output (e.g. ``tfdfidf_v`` in each file entry) so bootstrap can skip
# entries that are already current. ``bootstrap_meta.json`` records which features are fully
# migrated across ALL cards under ``feature_versions``; a feature is only marked complete there
# once every on-disk card/file-entry carries the current version for that feature.
_TFDFIDF_VERSION = 1  # stored as "tfdfidf_v" in each file entry within a card

# Name of the meta file written to cards_dir after a successful bootstrap.
_BOOTSTRAP_META_FILE = "bootstrap_meta.json"

# Sane bound on how many parent directories ``_discover_ancestor_card_dir`` will walk up looking
# for an already-indexed ancestor corpus, so a pathological mount (e.g. a very deep or looping
# filesystem namespace) can't make the upward walk hang. 12 comfortably covers any realistic
# corpus nesting depth.
_MAX_ANCESTOR_WALK_LEVELS = 12


def _run_parallel(callables: List, max_workers: int) -> List:
    """Run callables in parallel with daemon threads; return results in input order.

    Unlike ``ThreadPoolExecutor``, the worker threads are daemon threads and are NOT
    registered with Python's global ``_threads_queues``, so ``concurrent.futures``'
    atexit handler never blocks on them during program exit. A second Ctrl+C while
    bootstrap is running therefore exits cleanly instead of printing a traceback.

    Results are in the same order as ``callables``; a callable that raises yields
    ``None`` in the output list.
    """
    n = len(callables)
    if n == 0:
        return []
    if n == 1:
        try:
            return [callables[0]()]
        except Exception:  # noqa: BLE001
            return [None]

    results: List[Any] = [None] * n
    sem = threading.Semaphore(max_workers)
    done_lock = threading.Lock()
    done_count = [0]
    all_done = threading.Event()

    def _run(i: int, fn) -> None:
        try:
            results[i] = fn()
        except Exception:  # noqa: BLE001
            pass
        finally:
            sem.release()
            with done_lock:
                done_count[0] += 1
                if done_count[0] >= n:
                    all_done.set()

    for i, fn in enumerate(callables):
        sem.acquire()
        threading.Thread(target=_run, args=(i, fn), daemon=True).start()

    all_done.wait()
    return results


def _read_bootstrap_meta(cards_dir: str) -> dict:
    """Read ``bootstrap_meta.json`` from ``cards_dir``. Returns {} on any error. Never raises."""
    try:
        meta_path = Path(cards_dir) / _BOOTSTRAP_META_FILE
        with open(meta_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt/unreadable: treat as no meta
        return {}


def _write_bootstrap_meta(
    cards_dir: str,
    count: int,
    *,
    feature_versions: Optional[Dict[str, int]] = None,
) -> None:
    """Write ``bootstrap_meta.json`` atomically (temp file + replace). Never raises.

    Records the global algorithm ``version``, per-feature versions (``feature_versions`` dict —
    only features fully migrated across ALL cards are included), the ``card_count``, and a UTC
    ``completed_at`` timestamp.  A feature is absent from ``feature_versions`` when its migration
    is still in progress; startup re-triggers that feature's migration until it appears.
    """
    try:
        Path(cards_dir).mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _BOOTSTRAP_VERSION,
            "feature_versions": feature_versions or {},
            "card_count": int(count),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(cards_dir), prefix=".tmp_meta_", suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            os.replace(tmp_path, str(Path(cards_dir) / _BOOTSTRAP_META_FILE))
        except Exception:  # noqa: BLE001
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception:  # noqa: BLE001 — meta is best-effort; never fail the bootstrap
        pass

# Max file paths per area-discovery chunk. Keeping each chunk small means the LLM call finishes
# quickly and many chunks can run in parallel, so total wall-clock is bounded.
_CHUNK_SIZE = 150

# Max parallel LLM workers for bootstrap (area discovery and topic extraction).
_BOOTSTRAP_WORKERS = 8

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

# RECENCY/USAGE RANKING BOOST (keyword arm). A card the user recently relied on or that was just
# updated should resurface more readily on a NEAR-TIE, without ever overriding genuine relevance.
# The boost is a small bounded multiplier applied to the RANKING score only; the confidence GATE
# always uses the un-boosted relevance score, so a recently-used-but-irrelevant card is never
# resurrected. Set the max to 0.0 (constructor arg) to disable entirely.
_RECENCY_BOOST_MAX_DEFAULT = 0.20          # at most +20% to the ranking score, never the gate
_RECENCY_BOOST_HALF_LIFE_DAYS = 30.0       # recency component half-life (matches the vector arm)
_RECENCY_BOOST_USAGE_CAP = 5.0             # usage_count saturates here (5+ uses = full usage signal)
# PER-SOURCE usage-recency debounce. One turn assembles context several times (the run-level view,
# each deep goal, a widening retry, the card updater's own selection pass) and they are all the SAME
# use. Without a debounce a single turn would rewrite each used card once per assemble and inflate
# every source's use_count. A source re-used inside this window is left alone, which bounds card
# writes (and, on an embedding-backed repository, re-embeds) to roughly one per card per turn.
_SOURCE_USAGE_MIN_INTERVAL_SECONDS = 60.0

# QUEST-FOLDER BOOST: unlike the recency/usage boost above (a soft heuristic applied only AFTER
# the confidence gate), this applies BEFORE the gate, multiplying the raw score itself. That's
# deliberate: a quest_folder_map match means the CALLER already established this run is about that
# quest (its own goal_id, not a keyword guess), so a card pinning files under the linked folder
# gets a strong nudge to clear the gate even on a thinner keyword match — while a card with ZERO
# shared keywords (score 0) still scores 0 after the boost, so unrelated folder content is never
# forced in "never worse by construction" still holds.
_QUEST_FOLDER_BOOST = 4.0

# Max length for a card summary built from docstrings/descriptions (~400 chars).
_SUMMARY_MAX_CHARS = 400

# ---------------------------------------------------------------------------
# Source-agnostic card CONTENT model (additive to the file-only ``files[]`` list).
# ---------------------------------------------------------------------------
#
# A card may carry an optional top-level ``content`` list of TYPED items. Each item is either a
# REFERENCE (resolved FRESH to current content on every use) or an LLM NOTE (synthesized text).
# Files become just ONE reference type, so a card can hold zero files and still be selectable,
# renderable, and embeddable. Item shape:
#
#   {"id": "<stable id>", "type": "file|collection|conversation|query|note",
#    "locator": {<type-specific pointer>}, "ts": <epoch float>, "why": "<short reason>",
#    "last_used_ts": <epoch float>, "use_count": <int>}
#
# ``ts`` is when the source was LEARNED. ``last_used_ts`` / ``use_count`` are PER-SOURCE USAGE
# RECENCY: when this particular source was last actually RENDERED into context, and how often. They
# are stamped at the render seam (``_bump_source_usage``), so a card can tell which of its sources
# are hot and which have gone cold, and the ranker prefers the hot ones under a render budget. Both
# default to "never used" (0.0 / 0) when absent, so cards written before they existed need no
# migration. The same treatment applies to EVERY source type, not just files (a conversation or a
# collection reference warms exactly like a file).
#
# For ``note`` the locator is ``{"text": "..."}``. References resolve through the wired
# ``reference_resolvers`` registry (see adapters/reference_resolver.py); a card's content can grow
# unbounded over time, so resolution is RECENCY-BOUNDED: items are ranked by recency (``ts``) plus
# relevance to the task, and only the top-N within a char budget are resolved. The content model,
# its recency-bound limits (``_MAX_CARD_*``), tokenizer, ranker, and the shared ``render_card_content``
# routine now live in ``card_content_render`` so BOTH retrieval arms (this keyword store AND the
# vector assembler) resolve a selected card's references identically. They are imported above.

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


def _card_display_title(card: Dict[str, Any]) -> str:
    """The card's one-line title, whatever KIND of card it is. Never raises.

    A BOOTSTRAPPED card (built from a file) carries a ``summary``; a LEARNED card (written by an
    updater from a finished run) carries ``name`` + ``description``. Every consumer that shows or
    judges a card by its title (the rendered section header, the LLM relevance filter, the updater's
    view of current cards) must read BOTH shapes, or a learned card looks untitled and gets dropped.
    """
    try:
        for key in ("summary", "name", "description"):
            v = card.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _card_covered_paths(card: Dict[str, Any], limit: int = 10) -> List[str]:
    """The file paths a card COVERS: its pinned ``files`` plus its ``file`` content REFERENCES.

    A learned card pins no files: the paths it knows live in its content locators. Anything that
    asks "which files is this card about" (the LLM relevance filter, source transparency) must see
    those too, else a card whose whole value is the paths it found reads as covering nothing.
    Pinned files come first (mtime-newest first, as before), then references, deduped. Never raises.
    """
    paths: List[str] = []
    try:
        for fe in sorted(card.get("files", []) or [],
                         key=lambda fe: fe.get("mtime", 0.0), reverse=True):
            p = fe.get("path", "")
            if p and p not in paths:
                paths.append(p)
        for item in _normalize_content(card.get("content")):
            if item.get("type") != "file":
                continue
            p = str((item.get("locator") or {}).get("path") or "").strip()
            if p and p not in paths:
                paths.append(p)
    except Exception:  # noqa: BLE001
        return paths[:limit]
    return paths[:limit]


def _managed_names(raw: Any) -> Set[str]:
    """The set of names in a card's ``managed_fields`` / ``managed_items`` declaration.

    A card written by a consumer that owns it (see ``_update_card_inner``) may declare which
    embedded fields and content items only IT may write. Anything unparseable yields an empty set,
    so a malformed declaration degrades to "nothing is managed" rather than freezing the card.
    """
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {str(x).strip() for x in raw if isinstance(x, (str, int)) and str(x).strip()}


def _trim_content_by_recency(
    content: List[Dict[str, Any]], *, max_items: int = _MAX_CARD_CONTENT_ITEMS
) -> List[Dict[str, Any]]:
    """Cap a card's stored content at ``max_items``, dropping the OLDEST (lowest ``ts``) first.

    Applied on every read-modify-write so a card never grows without bound on disk. The kept items
    are returned in their ORIGINAL order (only the oldest excess is removed), so ids stay stable.
    Never raises.
    """
    try:
        if len(content) <= max_items:
            return content
        # Find the ids of the newest ``max_items`` by ts, then keep those in original order.
        by_recency = sorted(content, key=lambda it: it.get("ts", 0.0), reverse=True)
        keep_ids = {it.get("id") for it in by_recency[:max_items]}
        return [it for it in content if it.get("id") in keep_ids]
    except Exception:  # noqa: BLE001
        return content[:max_items]


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


def _extract_json_array(text: str) -> str:
    """Pull a JSON array substring out of an LLM response. Returns "" if none found.

    Handles markdown code fences (```json ... ```), leading prose, and trailing prose by
    slicing from the first ``[`` to its matching closing ``]``. Never raises.
    """
    if not text:
        return ""
    try:
        # Strip a fenced code block first if present.
        fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        start = text.find("[")
        if start < 0:
            return ""
        # Walk to the matching closing bracket, ignoring brackets inside strings.
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return ""
    except Exception:  # noqa: BLE001
        return ""


# Prompt for stage 1: given a chunk of file paths, identify top-level areas.
_AREA_PROMPT = """You are analyzing a slice of a codebase to identify its top-level areas.

Source files (one path per line):

{file_tree}

Group these files into 2 to 8 high-level areas, modules, or subsystems. An area is a cohesive
cluster of files that work together. The number of areas should reflect what is really there.

For each area produce:
  - name: a short label (2-5 words)
  - description: one sentence describing what this area does
  - files: which of the given paths belong here (exact copies from the list above)

Respond with ONLY a JSON array, no prose, no markdown fences:
[{{"name": "...", "description": "...", "files": ["path1", "path2"]}}]
"""

# Prompt for stage 2: given an area with its files, extract topic cards.
_TOPIC_PROMPT = """You are analyzing files from the "{area_name}" area of a codebase.
{area_description}

Source files:

{file_tree}

Identify 2 to 8 specific topic cards. A topic card groups files that work together around a
specific concept or feature. A card may include files from different directories. The number of
cards should reflect what is really there in these files.

For each topic card produce:
  - id: a short unique slug (lowercase, hyphens only)
  - name: a short human-readable name (3-6 words)
  - keywords: 5 to 12 lowercase keywords someone might use to find this topic
  - summary: one sentence describing what this group of files does
  - files: which of the given paths belong here (exact copies from the list above)

A file may appear in multiple cards. Every path you list must be an exact copy from the list above.

Respond with ONLY a JSON array, no prose, no markdown fences:
[{{"id": "...", "name": "...", "keywords": ["..."], "summary": "...", "files": ["path"]}}]
"""


def _parse_raw_entries(raw: str, allowed: Set[str]) -> List[Dict[str, Any]]:
    """Parse a JSON array from ``raw`` and validate each entry. Returns [] on any failure."""
    payload = _extract_json_array(raw or "")
    if not payload:
        return []
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    out = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        out.append({k: v for k, v in entry.items()})
    return out


def _discover_areas(chunk: List[str], provider, model) -> List[Dict[str, Any]]:
    """Stage 1: ask the LLM to identify top-level areas in ``chunk``. Never raises."""
    try:
        file_tree = "\n".join(chunk)
        prompt = _AREA_PROMPT.format(file_tree=file_tree)
        raw = provider.answer([{"role": "user", "content": prompt}], model=model)
        allowed = set(chunk)
        areas = []
        for entry in _parse_raw_entries(raw, allowed):
            name = entry.get("name", "").strip()
            desc = entry.get("description", "").strip()
            files = [f for f in (entry.get("files") or []) if isinstance(f, str) and f in allowed]
            if name and files:
                areas.append({"name": name, "description": desc, "files": files})
        return areas
    except Exception:  # noqa: BLE001
        _log.exception("context index: area discovery failed for chunk")
        return []


def _extract_file_snippet(fpath: str, walk_root: Optional[Path] = None, max_bytes: int = 2048) -> str:
    """Extract a concise snippet from a file: docstring + function/class signatures.

    For each file, attempts to read first docstring, function/class definitions,
    and a few substantive lines. Returns at most max_bytes of content.
    Never raises; returns empty string on read error.
    """
    try:
        if walk_root:
            full_path = walk_root / fpath
        else:
            full_path = Path(fpath)

        if not full_path.exists() or full_path.stat().st_size > _BOOTSTRAP_MAX_BYTES:
            return ""

        with open(full_path, "r", encoding="utf-8", errors="ignore") as fh:
            content = fh.read(max_bytes)
    except Exception:  # noqa: BLE001
        return ""

    if not content.strip():
        return ""

    lines = content.split("\n")
    snippet_lines: List[str] = []

    # Extract leading docstring (""" or ''').
    in_docstring = False
    docstring_delimiter = None
    for i, line in enumerate(lines[:50]):  # Check first 50 lines
        stripped = line.strip()
        if not in_docstring and stripped.startswith('"""') or stripped.startswith("'''"):
            docstring_delimiter = '"""' if stripped.startswith('"""') else "'''"
            in_docstring = True
            snippet_lines.append(line)
            if stripped.count(docstring_delimiter) >= 2:  # Single-line docstring
                in_docstring = False
            continue
        if in_docstring and docstring_delimiter in line:
            snippet_lines.append(line)
            in_docstring = False
            continue
        if in_docstring:
            snippet_lines.append(line)

    # Extract function/class definitions (def, class, function, etc.).
    for line in lines[len(snippet_lines):]:
        stripped = line.strip()
        if stripped.startswith(("def ", "class ", "async def ", "function ", "@")):
            snippet_lines.append(line)
            if len(snippet_lines) > 20:  # Limit to ~20 key lines
                break

    # If very little extracted, include first few non-empty lines.
    if len(snippet_lines) < 5:
        for line in lines:
            if line.strip() and not line.strip().startswith("#"):
                snippet_lines.append(line)
                if len(snippet_lines) >= 8:
                    break

    return "\n".join(snippet_lines[:20])


def _summarize_snippet(fpath: str, snippet: str) -> str:
    """Summarize a file snippet based on its length.

    - Short (< 200 chars): return as-is
    - Medium (200-500 chars): keep docstring + first 2 signatures
    - Long (> 500 chars): just docstring + file name hint

    This is a heuristic; no LLM involved.
    """
    snippet_len = len(snippet)

    if snippet_len < 200:
        return f"{fpath}:\n{snippet}"

    lines = snippet.split("\n")

    if snippet_len < 500:
        # Keep docstring (first ~10 lines or until close bracket) + key definitions.
        kept: List[str] = []
        for line in lines[:15]:
            kept.append(line)
            if '"""' in line or "'''" in line:
                # Likely end of docstring
                break
        # Add first 2 function/class definitions
        defs_added = 0
        for line in lines[len(kept):]:
            if line.strip().startswith(("def ", "class ", "async def ")):
                kept.append(line)
                defs_added += 1
                if defs_added >= 2:
                    break
        return f"{fpath}:\n" + "\n".join(kept)

    # Long: just docstring (first 10 lines) + filename hint
    kept = []
    for line in lines[:10]:
        kept.append(line)
        if '"""' in line or "'''" in line:
            break
    if not kept:
        kept = lines[:5]
    return f"{fpath} (detailed content omitted, {len(snippet)} bytes):\n" + "\n".join(kept)


def _extract_topic_cards(area: Dict[str, Any], allowed: Set[str], provider, model, walk_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Stage 2: given an area dict, sample representative files, extract & summarize snippets,
    then ask the LLM for topic cards. Never raises."""
    try:
        # Sample representative files from the area (reduce token spend on large areas).
        area_files = area.get("files", [])
        sampled_files = _select_representative_files(area_files, samples_per_folder=2) if len(area_files) > 5 else area_files

        # Extract snippets + summarize by length for each sampled file.
        file_entries: List[str] = []
        for fpath in sampled_files:
            snippet = _extract_file_snippet(fpath, walk_root=walk_root)
            if snippet:
                summarized = _summarize_snippet(fpath, snippet)
                file_entries.append(summarized)
            else:
                # Fallback: just the path if snippet extraction failed
                file_entries.append(fpath)

        file_tree = "\n---\n".join(file_entries)
        prompt = _TOPIC_PROMPT.format(
            area_name=area["name"],
            area_description=area.get("description", ""),
            file_tree=file_tree,
        )
        raw = provider.answer([{"role": "user", "content": prompt}], model=model)
        cards = []
        for entry in _parse_raw_entries(raw, allowed):
            cid = (entry.get("id") or "").strip()
            name = (entry.get("name") or "").strip()
            keywords = entry.get("keywords")
            summary = (entry.get("summary") or "").strip()
            files = entry.get("files")
            if not (cid and name and summary):
                continue
            if not isinstance(keywords, list):
                continue
            if not isinstance(files, list):
                continue
            kept = [f for f in files if isinstance(f, str) and f in allowed]
            if not kept:
                continue
            cards.append({
                "id": cid,
                "name": name,
                "keywords": [str(k).strip() for k in keywords if str(k).strip()],
                "summary": summary,
                "files": list(dict.fromkeys(kept)),
            })
        return cards
    except Exception:  # noqa: BLE001
        _log.exception("context index: topic extraction failed for area %r", area.get("name"))
        return []


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """File-set Jaccard similarity. Returns 0.0 when both sets are empty."""
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


# Two cards are dedup CANDIDATES when they share at least this many keywords.
_DEDUP_MIN_SHARED_KEYWORDS = 2

# Auto-merge Jaccard threshold for the no-LLM fallback (lower than the old 0.7 so the keyword-
# only path still collapses obvious duplicates when no provider is available to judge).
_DEDUP_FALLBACK_JACCARD = 0.30


# Prompt for stage 3: given a cluster of possibly-overlapping cards, ask the LLM which to merge.
_DEDUP_PROMPT = """These topic cards may overlap. Decide which describe the same concept and \
should be merged.

{card_lines}

Return a JSON array of arrays of 1-based card numbers to group together.
Cards in the same inner array will be merged. Each card must appear exactly once.
Example: [[1,2],[3]] merges cards 1+2, keeps 3 separate.
Return ONLY the JSON array, no prose."""


class _UnionFind:
    """Tiny union-find over integer indices for transitive keyword clustering."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression.
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[rb] = ra

    def clusters(self) -> List[List[int]]:
        groups: Dict[int, List[int]] = {}
        for i in range(len(self._parent)):
            groups.setdefault(self.find(i), []).append(i)
        return list(groups.values())


def _card_keyword_set(card: Dict[str, Any]) -> Set[str]:
    """Lowercased keyword set for a card (used to find shared-keyword candidates)."""
    return {str(k).strip().lower() for k in card.get("keywords", []) if str(k).strip()}


def _keyword_clusters(cards: List[Dict[str, Any]]) -> List[List[int]]:
    """Cluster card indices that share >= _DEDUP_MIN_SHARED_KEYWORDS keywords (transitive).

    Uses union-find so a chain A~B~C clusters together even if A and C don't directly overlap.
    Returns a list of index lists (singletons included).
    """
    n = len(cards)
    uf = _UnionFind(n)
    kw_sets = [_card_keyword_set(c) for c in cards]
    for i in range(n):
        for j in range(i + 1, n):
            if len(kw_sets[i] & kw_sets[j]) >= _DEDUP_MIN_SHARED_KEYWORDS:
                uf.union(i, j)
    return uf.clusters()


def _merge_card_group(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a group of cards into one. The first card keeps its id/name/summary; keywords and
    files are unioned across the group (order-preserving)."""
    rep = {**group[0]}
    keywords = list(rep.get("keywords", []))
    files = list(rep.get("files", []))
    for card in group[1:]:
        for k in card.get("keywords", []):
            if k not in keywords:
                keywords.append(k)
        for f in card.get("files", []):
            if f not in files:
                files.append(f)
    rep["keywords"] = keywords
    rep["files"] = files
    return rep


def _parse_groupings(raw: str, n_cards: int) -> Optional[List[List[int]]]:
    """Parse an LLM dedup response into 0-based index groups, or None if invalid.

    Expects a JSON array of arrays of 1-based card numbers covering each card exactly once.
    Returns None (caller keeps the cluster unchanged) on any parse/validation failure — this is
    what keeps a mocked provider that returns non-grouping JSON from corrupting the cards.
    """
    payload = _extract_json_array(raw or "")
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, list) or not parsed:
        return None
    groups: List[List[int]] = []
    seen: Set[int] = set()
    for inner in parsed:
        if not isinstance(inner, list) or not inner:
            return None
        idxs: List[int] = []
        for num in inner:
            if not isinstance(num, int) or isinstance(num, bool):
                return None
            idx = num - 1  # 1-based -> 0-based
            if idx < 0 or idx >= n_cards or idx in seen:
                return None
            seen.add(idx)
            idxs.append(idx)
        groups.append(idxs)
    # Every card must appear exactly once.
    if len(seen) != n_cards:
        return None
    return groups


def _dedup_cluster_llm(cluster: List[Dict[str, Any]], provider, model) -> List[Dict[str, Any]]:
    """Make ONE LLM call to decide merges within a candidate cluster. Never raises.

    On any failure (no provider, bad response, parse failure) returns the cluster unchanged.
    """
    if provider is None or len(cluster) < 2:
        return cluster
    try:
        card_lines = "\n".join(
            'Card {n}: "{name}" — keywords: [{kws}] — {fc} files'.format(
                n=i + 1,
                name=c.get("name", c.get("id", "")),
                kws=", ".join(str(k) for k in c.get("keywords", [])),
                fc=len(c.get("files", [])),
            )
            for i, c in enumerate(cluster)
        )
        prompt = _DEDUP_PROMPT.format(card_lines=card_lines)
        raw = provider.answer([{"role": "user", "content": prompt}], model=model)
        groups = _parse_groupings(raw, len(cluster))
        if groups is None:
            return cluster
        return [_merge_card_group([cluster[i] for i in g]) for g in groups]
    except Exception:  # noqa: BLE001
        _log.exception("context index: LLM dedup failed for cluster, keeping it unchanged")
        return cluster


def _dedup_fallback(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """No-LLM fallback: greedily merge cards with file-overlap Jaccard >= _DEDUP_FALLBACK_JACCARD."""
    merged: List[Dict[str, Any]] = []
    for card in cards:
        files_a = set(card.get("files", []))
        matched = False
        for rep in merged:
            if _jaccard(files_a, set(rep.get("files", []))) >= _DEDUP_FALLBACK_JACCARD:
                rep["files"] = list(dict.fromkeys(rep.get("files", []) + card.get("files", [])))
                rep["keywords"] = list(
                    dict.fromkeys(rep.get("keywords", []) + card.get("keywords", []))
                )
                matched = True
                break
        if not matched:
            merged.append({**card})
    return merged


def _dedup_topic_cards(
    raw_cards: List[Dict[str, Any]],
    provider,
    model=None,
    existing_cards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Stage 3: dedup topic cards by keyword-clustering + an LLM merge decision.

    Step 1 — cluster cards (new + existing) by shared keywords (>= 2 shared keywords, transitive
             via union-find). Singletons pass through unchanged.
    Step 2 — for each cluster of 2+ NEW cards (no existing card in it), one LLM call decides the
             merge groupings; on parse failure the cluster is kept unchanged.
    Step 3 — for any cluster that contains an EXISTING card, merge the new cards INTO the existing
             one (keeping the existing card's id) rather than emitting a new card with a new id.

    When ``provider is None`` falls back to a keyword-union merge (Jaccard >= 30%).

    Returns the deduplicated list of NEW/updated cards to write. Existing-only clusters (no new
    card) contribute nothing new to write here unless a new card merged into them.
    """
    if not raw_cards:
        return []

    existing_cards = existing_cards or []

    if provider is None:
        # No LLM: keyword-union fallback over the new cards only. (Existing-card dedup needs the
        # LLM's judgment; without it we keep new cards distinct and let id-collision upsert handle
        # exact-id matches downstream.)
        return _dedup_fallback(raw_cards)

    # Combine new + existing for clustering so a new card that duplicates an existing one lands in
    # the same cluster. Tag each with its origin so we can route the result correctly.
    combined: List[Dict[str, Any]] = list(raw_cards) + list(existing_cards)
    is_existing = [False] * len(raw_cards) + [True] * len(existing_cards)

    clusters = _keyword_clusters(combined)
    out: List[Dict[str, Any]] = []
    for cluster_idx in clusters:
        has_existing = any(is_existing[i] for i in cluster_idx)
        new_members = [combined[i] for i in cluster_idx if not is_existing[i]]

        if not new_members:
            # Cluster of only existing cards — nothing new to write.
            continue

        if has_existing:
            # Merge the new cards INTO the first existing card in the cluster, preserving its id.
            existing_member = next(combined[i] for i in cluster_idx if is_existing[i])
            merged = _merge_card_group([existing_member] + new_members)
            out.append(merged)
            continue

        if len(new_members) == 1:
            out.append({**new_members[0]})
            continue

        # Pure-new cluster of 2+: let the LLM decide the merge groupings.
        out.extend(_dedup_cluster_llm(new_members, provider, model))

    # Guard against an existing id colliding with a fresh id we keep: dedup by id, first wins.
    seen_ids: Set[str] = set()
    deduped: List[Dict[str, Any]] = []
    for card in out:
        cid = card.get("id")
        if cid in seen_ids:
            continue
        seen_ids.add(cid)
        deduped.append(card)
    return deduped


def _select_representative_files(file_paths: List[str], samples_per_folder: int = 3) -> List[str]:
    """Select representative files per folder using shared TF-DF-IDF heuristic.

    Delegates to the shared select_representatives() function from tfdfidf_sampling module,
    grouping by folder and scoring by term distinctiveness.
    """
    return select_representatives(
        file_paths,
        get_terms=tfdfidf_extract_terms,
        samples_per_group=samples_per_folder,
        get_group=lambda fpath: str(Path(fpath).parent) or ".",
    )


def _llm_topic_cards(file_paths, provider, model=None, existing_cards=None, walk_root=None):
    """3-stage parallel LLM fan-out to identify semantic topic cards.

    Stage 1 -- chunk the path list and run area-discovery calls in parallel (each chunk
    is small so each call is fast). Stage 2 -- for each discovered area, sample representative
    files, extract snippets, summarize by length, then run a topic-extraction call in parallel.
    Stage 3 -- dedup via keyword-clustering + an LLM merge decision (``_dedup_topic_cards``),
    also folding new cards into duplicate ``existing_cards``.

    ``walk_root`` (optional) is used in Stage 2 to read file snippets for context. When absent,
    Stage 2 falls back to file paths only.

    Never raises. Returns [] on any unrecoverable error.
    """
    if provider is None or not file_paths:
        return []
    try:
        allowed = set(file_paths)
        paths = list(file_paths)

        # Stage 1: select representative files per folder using TF-DF-IDF to reduce token spend,
        # then chunk and discover areas in parallel. This heuristic selects files distinctive
        # within each folder (high within-folder term frequency, penalizing corpus-generic terms),
        # so the LLM sees diverse structure without processing all paths.
        representative_paths = _select_representative_files(paths, samples_per_folder=3)
        chunks = [representative_paths[i:i + _CHUNK_SIZE] for i in range(0, len(representative_paths), _CHUNK_SIZE)]
        _log.info(
            "context index: bootstrap stage 1 — %d representative file(s) from %d total file(s) "
            "across %d chunk(s)",
            len(representative_paths), len(paths), len(chunks),
        )
        areas: List[Dict[str, Any]] = []
        for result in _run_parallel(
            [lambda c=chunk: _discover_areas(c, provider, model) for chunk in chunks],
            max_workers=_BOOTSTRAP_WORKERS,
        ):
            if result:
                areas.extend(result)

        if not areas:
            _log.warning("context index: bootstrap stage 1 returned no areas")
            return []
        _log.info("context index: bootstrap stage 2 — extracting topics from %d area(s)", len(areas))

        # Stage 2: extract topic cards per area in parallel (with sampled snippets).
        raw_cards: List[Dict[str, Any]] = []
        walk_root_path = Path(walk_root).resolve() if walk_root else None
        for result in _run_parallel(
            [lambda a=area: _extract_topic_cards(a, allowed, provider, model, walk_root=walk_root_path) for area in areas],
            max_workers=_BOOTSTRAP_WORKERS,
        ):
            if result:
                raw_cards.extend(result)

        if not raw_cards:
            _log.warning("context index: bootstrap stage 2 returned no topic cards")
            return []

        # Stage 3: dedup via keyword-clustering + LLM merge decision (folding into existing cards).
        merged = _dedup_topic_cards(raw_cards, provider, model, existing_cards=existing_cards)
        _log.info(
            "context index: bootstrap stage 3 — deduped %d raw cards into %d unique cards",
            len(raw_cards), len(merged),
        )
        return merged

    except Exception as exc:  # noqa: BLE001
        _log.warning("context index: LLM topic identification failed: %s", exc, exc_info=True)
        return []


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
    ) -> None:
        self._cards_dir = Path(cards_dir)
        # Card PERSISTENCE is pluggable behind a CardRepository. Default: per-card JSON files under
        # cards_dir (byte-for-byte the prior behavior). A consumer may inject a database-backed repo.
        self._repo: CardRepository = card_repository or FilesystemCardRepository(cards_dir)
        self._repo_root = Path(repo_root).resolve() if repo_root else None
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
        # Dirty flag: set after any write so next _load_all() reloads from the repo.
        self._cache_dirty: bool = False
        # The repository revision (``repo.revision()``) captured at the last cache load. For the
        # filesystem repo this is the (max_child_mtime, file_count) stamp; for another repo it is
        # whatever opaque change-stamp that repo returns. _load_all() reloads when it changes. The
        # initial sentinel never matches a real revision, so the first _load_all() always loads.
        self._cache_dir_stamp: Any = (0.0, 0)

    def register_reference_resolver(
        self, ref_type: str, resolver: Any, *, override: bool = False
    ) -> None:
        """Wire a resolver for content ``type`` ``ref_type`` AFTER construction. Best-effort.

        Lets a caller add a resolver for a resolvable adapter that is only built later than this store
        (e.g. an adapter whose own construction depends on this store). ``resolver`` is either a
        ReferenceResolver object (has ``resolve(locator, *, max_chars)``) or a bare callable with that
        same signature -- e.g. an adapter's ``resolve_reference`` bound method. By default an existing
        wired resolver for the type is KEPT (so a consumer-injected one always wins); pass
        ``override=True`` to replace it. A callable is adapted to the ReferenceResolver surface. Never
        raises."""
        try:
            if not isinstance(ref_type, str) or not ref_type or resolver is None:
                return
            if ref_type in self._resolvers and not override:
                return
            from .reference_resolver import coerce_resolver
            coerced = coerce_resolver(resolver)
            if coerced is not None:
                self._resolvers[ref_type] = coerced
        except Exception:  # noqa: BLE001 — resolver registration must never break a run
            pass

    def _recency_boost_factor(self, card: Dict[str, Any]) -> float:
        """A small bounded multiplier (1.0 .. 1.0+max) that nudges a recently-used / recently-updated
        card up the RANKING, used ONLY after the confidence gate has passed on the un-boosted score.

        Blends two real signals, each normalized to 0..1: usage (``usage_count`` saturating at
        ``_RECENCY_BOOST_USAGE_CAP``) and recency (the newest content ``ts``, decayed by a 30-day
        half-life). With no usage and no timestamped content the factor is exactly 1.0 (no effect),
        so cards without history rank purely on relevance. Returns 1.0 when the boost is disabled
        (max == 0.0). Never raises.
        """
        if self._recency_boost_max <= 0.0:
            return 1.0
        try:
            usage = float(card.get("usage_count", 0) or 0.0)
            usage_norm = (min(usage, _RECENCY_BOOST_USAGE_CAP) / _RECENCY_BOOST_USAGE_CAP
                          if _RECENCY_BOOST_USAGE_CAP > 0 else 0.0)
            content = card.get("content") or []
            ts_values = [float(it.get("ts") or 0.0) for it in content if isinstance(it, dict)]
            newest = max(ts_values) if ts_values else 0.0
            recency_norm = 0.0
            if newest > 0.0:
                now = datetime.now(timezone.utc).timestamp()
                age_days = max(0.0, (now - newest) / 86400.0)
                recency_norm = 0.5 ** (age_days / _RECENCY_BOOST_HALF_LIFE_DAYS)
            signal = 0.5 * usage_norm + 0.5 * recency_norm   # 0..1
            return 1.0 + self._recency_boost_max * signal
        except Exception:  # noqa: BLE001
            return 1.0

    @staticmethod
    def _card_pins_folder(card: Dict[str, Any], folder_prefix: str) -> bool:
        """True if any file this card pins falls under ``folder_prefix`` (a repo-root-relative
        POSIX path, no trailing slash)."""
        prefix_with_slash = folder_prefix.rstrip("/") + "/"
        for fe in card.get("files", []):
            p = fe.get("path", "")
            if p and (p == folder_prefix or p.startswith(prefix_with_slash)):
                return True
        return False

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
            return self._assemble_inner(task_text, meta=meta)
        except Exception:  # noqa: BLE001
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Upsert a card for this task and write it atomically. Never raises.

        Beyond re-pinning files (``outcome["files"]``), ``record`` can also append source-agnostic
        CONTENT: pass ``outcome["content"]`` as a list of content items (or single dicts) to append
        them to the card's ``content`` list (recency-trimmed on write). This generalizes ``record``
        so a run can accumulate notes / collection refs / conversation refs, not just files.
        """
        try:
            self._record_inner(task_text, outcome)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # Card-update API: read-modify-write a card's source-agnostic content.
    # ------------------------------------------------------------------

    def add_content(self, card_id: str, item: Dict[str, Any]) -> bool:
        """Append ONE content item to ``card_id`` (creating the card if absent). Never raises.

        Safe read-modify-write: loads the card, normalizes + appends the item, applies the recency
        trim, and persists atomically. Returns True on a successful write, False otherwise. The item
        is normalized (type defaults to ``note``, ``ts`` coerced, ``id`` synthesized when missing),
        so a caller may pass a partial dict. An async LLM updater will use this; the API + tests are
        built now, the updater later.
        """
        try:
            return self._update_card_inner(card_id, add=[item])
        except Exception:  # noqa: BLE001
            return False

    def update_content(self, card_id: str, item_id: str, new_item: Dict[str, Any]) -> bool:
        """Correct/replace the content item ``item_id`` on ``card_id`` with ``new_item``. Never raises.

        Read-modify-write: the matching item is replaced in place (keeping ``item_id`` unless
        ``new_item`` supplies its own ``id``); if no item matches, ``new_item`` is appended instead.
        Returns True on a successful write. This is how a correction lands without rewriting the
        whole card.
        """
        try:
            return self._update_card_inner(card_id, replace=[(item_id, new_item)])
        except Exception:  # noqa: BLE001
            return False

    def remove_content(self, card_id: str, item_id: str) -> bool:
        """Remove the content item ``item_id`` from ``card_id``. Never raises.

        Read-modify-write: drops the matching item and persists. Returns True on a successful write
        (including when the id was already absent and the card was simply re-saved unchanged).
        """
        try:
            return self._update_card_inner(card_id, remove=[item_id])
        except Exception:  # noqa: BLE001
            return False

    def update_card(
        self,
        card_id: str,
        *,
        add: Optional[List[Dict[str, Any]]] = None,
        replace: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        remove: Optional[List[str]] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Apply a batch of edits to ``card_id`` in ONE read-modify-write. Never raises.

        ``fields`` sets embedded card fields (``name``/``description``/``summary``); ``add`` appends
        content items, ``replace`` is a list of ``(item_id, new_item)`` corrections, and ``remove`` is
        a list of item ids to drop. All are applied to the same loaded card, the recency trim runs
        once, and the card is written atomically. Returns True on success. Editing ``name``/
        ``description``/``summary`` re-fingerprints the card so the vector store re-embeds it.
        """
        try:
            return self._update_card_inner(card_id, add=add, replace=replace, remove=remove,
                                           fields=fields)
        except Exception:  # noqa: BLE001
            return False

    def get_card(self, card_id: str) -> Optional[Dict[str, Any]]:
        """Return the RAW stored card dict for ``card_id`` (or None if absent). Never raises.

        The read seam for any consumer that needs a card's own fields (its ``keywords`` / ``name`` /
        ``summary`` / ``description`` topic terms, its content items) without going through the
        rendered-string ``render_card`` path. Reads through the persistence boundary (never a stale
        in-memory copy), so it reflects concurrent writes from other agents. Used by the cross-session
        recall path (``ClaudeConversationsAdapter``) to pull the active card's topic terms.
        """
        try:
            loaded = self._repo.read(card_id)
            return loaded if isinstance(loaded, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def find_similar_card(
        self, text: str, *, user_id: Optional[str] = None, min_score: float
    ) -> Optional[str]:
        """Return the id of an existing card whose embedding is cosine-similar to ``text`` at or above
        ``min_score``, restricted to ``user_id``'s scope, or ``None``. Never raises.

        This is the OPTIONAL semantic-dedup capability the orchestrator's post-deep card updater
        probes (by duck-typing) before creating a near-duplicate card. It DELEGATES to the persistence
        repository's own ``find_similar_card`` when present (e.g. ``QdrantCardRepository``, which
        embeds every card on write and can vector-search them) -- reusing the SAME embedding + vector
        search the card vector arm uses, never a second embedding path. When the repo has NO
        embeddings (the default ``FilesystemCardRepository``), this returns ``None``, so a keyword-only
        store cleanly degrades to create-as-before with no fuzzy/string fallback. Detected by
        duck-typing (``callable``), never an isinstance check, so any repo exposing the method
        participates.
        """
        try:
            finder = getattr(self._repo, "find_similar_card", None)
            if not callable(finder):
                return None
            if not (text or "").strip():
                return None
            result = finder(text, user_id=user_id, min_score=min_score)
            return result if isinstance(result, str) and result.strip() else None
        except Exception:  # noqa: BLE001 — semantic dedup is best-effort
            return None

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
        provider=None,
        model: Optional[str] = None,
    ) -> int:
        """Seed the cards store by walking a source tree. Never raises. Returns cards written.

        Topic cards are SEMANTIC, not structural: a single card can span files from completely
        separate directories. An LLM (the wired ``provider``) analyses the corpus file list and
        identifies the natural topics; the number of cards reflects the actual structure of the
        codebase, never a preset range. Each card captures:

        - ``id``       -- a short slug for the topic (from the LLM).
        - ``keywords`` -- 5-15 search keywords for the topic (from the LLM).
        - ``summary``  -- a one-sentence description of the topic (from the LLM).
        - ``files``    -- every file the topic references, fingerprinted (sha256/mtime/git_sha).
        - ``provenance.created_by_task`` == "bootstrap".

        Before any LLM work, corpora that already have their OWN completed bootstrap are reused
        wholesale in BOTH directions (see ``reuse_nested_cards`` on the constructor): descendant
        sub-corpora (this root is the wider one) and ancestor corpora (this root is the narrower
        one, nested inside an already-indexed wider tree) are both discovered, and their cards are
        imported with rewritten paths and a namespaced id, excluding their files from LLM
        discovery. This makes a corpus root's bootstrap free for any subtree already covered by an
        indexed QAR instance either above or below it.

        Without a ``provider`` this is a NO-OP returning 0 UNLESS reuse (either direction) produced
        cards: topic cards for genuinely new content require semantic understanding, so those
        accumulate via ``record()`` instead, but reused cards need no LLM at all.

        On success (``n > 0``) a ``bootstrap_meta.json`` sidecar is written recording the
        algorithm version + card count, so a later run can detect a stale index and re-build.

        In dry-run mode, the bootstrap runs through all the same steps but does not write
        cards to disk. Token counts are still tracked in the provider.
        """
        try:
            n = self._bootstrap_inner(root=root, provider=provider, model=model)
        except Exception:  # noqa: BLE001
            return 0
        if n > 0 and not self._dry_run:
            _write_bootstrap_meta(
                str(self._cards_dir),
                self._count_cards_on_disk(),
                feature_versions=self._completed_feature_versions(),
            )
        return n

    def _completed_feature_versions(self) -> Dict[str, int]:
        """Return the subset of per-feature versions that are complete across ALL on-disk cards.

        A feature is included only when every file entry in every card carries the current version
        for that feature.  Missing or outdated entries mean the feature migration is still in
        progress — the feature is omitted so the next startup retries it.  Never raises.
        """
        try:
            all_cards = self._load_all()
            tfdfidf_done = all(
                fe.get("tfdfidf_v", 0) >= _TFDFIDF_VERSION
                for card in all_cards.values()
                for fe in card.get("files", [])
                if isinstance(fe, dict) and fe.get("path")
            )
            return {"tfdfidf": _TFDFIDF_VERSION} if tfdfidf_done else {}
        except Exception:  # noqa: BLE001
            return {}

    def close(self) -> None:
        """Stop this store's background indexing and refuse to start any more.

        Idempotent and safe to call from any thread. An in-flight ``bootstrap()`` / ``refresh_stale()``
        notices at its next checkpoint and returns what it has written so far (cards already written
        are kept: bootstrap is incremental and resumes next start). No new ``git hash-object``
        subprocess is spawned after this returns.

        Call it when a store's owner goes away (a consumer rebuilding its orchestrator, a CLI
        exiting, a test finishing). ``config.shutdown_background_index()`` closes every store it
        started a thread for and joins those threads, which is the usual way to reach this.
        """
        self._closed.set()

    def is_closed(self) -> bool:
        """True once ``close()`` has been called. Background indexing checks this at each checkpoint."""
        return self._closed.is_set()

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
        # Only bootstrap when there are no existing cards. Ask the repository (not the filesystem)
        # so a non-filesystem repo with cards already in it does NOT re-bootstrap.
        if self._repo.load_all():
            return
        try:
            self._bootstrap_inner(root=str(root))
        except Exception:  # noqa: BLE001
            pass

    def refresh_stale(self, root: Optional[str] = None, *, provider=None, model: Optional[str] = None) -> int:
        """Re-index only files whose content changed since the last bootstrap.

        Existing cards are refreshed purely by a fingerprint check, so no LLM re-call is needed
        (``provider`` defaults to None and is threaded through unchanged). A card whose every
        pinned file's sha256 still matches is skipped; a card with any changed/missing file is
        rewritten with fresh fingerprints.

        Designed to be called from a background thread at startup so the context
        index stays warm without blocking the caller.  Never raises; returns the
        number of cards written (0 = everything was already up to date).
        """
        try:
            return self._bootstrap_inner(root=root, provider=provider, model=model, skip_unchanged=True)
        except Exception:  # noqa: BLE001
            return 0

    def _discover_nested_card_dirs(self, walk_root: Path, skip_dirs: Set[str]) -> List[Path]:
        """Find sub-corpora under ``walk_root`` that already have their OWN completed bootstrap.

        A directory qualifies when ``<dir>/.quest-context/bootstrap_meta.json`` exists — the
        signal a DIFFERENT (usually narrower-scoped) ``FileContextStore`` already indexed it, e.g.
        a ``product/`` corpus nested under a wider ``~/hq`` corpus. Recursion stops at each match:
        a nested store is trusted to have already reused whatever is bootstrapped beneath IT (if it
        implements the same reuse), so importing its cards transitively covers its own subtree too.
        Never raises; returns [] on any failure.
        """
        found: List[Path] = []
        try:
            walk_root_resolved = walk_root.resolve()
            cards_dir_resolved = self._cards_dir.resolve()
            for dirpath, dirnames, _filenames in os.walk(walk_root):
                current_dir = Path(dirpath).resolve()
                if current_dir == walk_root_resolved:
                    prune_dirnames(dirnames, current=current_dir, base_skip=skip_dirs)
                    continue
                if current_dir == cards_dir_resolved:
                    dirnames[:] = []
                    continue
                if (current_dir / ".quest-context" / _BOOTSTRAP_META_FILE).is_file():
                    found.append(current_dir)
                    dirnames[:] = []  # trust it transitively; don't look for nested-within-nested
                    continue
                prune_dirnames(dirnames, current=current_dir, base_skip=skip_dirs)
        except Exception:  # noqa: BLE001
            pass
        return found

    def _import_nested_cards(self, nested_root: Path, walk_root: Path) -> List[Dict[str, Any]]:
        """Reuse a nested corpus's already-bootstrapped cards instead of re-discovering them.

        Pure filesystem reuse: reads the nested store's on-disk cards directly (no LLM call), then
        rewrites each file path from nested-root-relative to THIS store's walk-root-relative and
        namespaces the card id by the nested root's path so ids from unrelated sub-corpora never
        collide. Each returned dict carries ``imported_from`` (the nested root, relative to
        ``walk_root``) so ``_bootstrap_inner`` can both write it into the card's provenance and
        prune it later if the nested store stops offering it. Never raises; returns [] on failure.
        """
        try:
            rel_root = nested_root.relative_to(walk_root)
        except ValueError:
            return []
        prefix = rel_root.as_posix()
        id_prefix = _path_slug(prefix)
        try:
            nested_cards = FilesystemCardRepository(str(nested_root / ".quest-context")).load_all()
        except Exception:  # noqa: BLE001
            return []
        imported: List[Dict[str, Any]] = []
        for card in nested_cards.values():
            try:
                files = [
                    (rel_root / fe["path"]).as_posix()
                    for fe in card.get("files", [])
                    if isinstance(fe, dict) and fe.get("path")
                ]
                if not files:
                    continue
                cid = str(card.get("id") or "")
                if not cid:
                    continue
                imported.append({
                    "id": f"{id_prefix}--{cid}"[:200],
                    "name": card.get("name", ""),
                    "keywords": card.get("keywords", []),
                    "summary": card.get("summary", ""),
                    "description": card.get("description") or card.get("summary", ""),
                    "files": files,
                    "imported_from": prefix,
                })
            except Exception:  # noqa: BLE001
                continue
        return imported

    def _discover_ancestor_card_dir(self, walk_root: Path) -> Optional[Path]:
        """Find the nearest ANCESTOR of ``walk_root`` that already has its own completed bootstrap.

        Mirror image of ``_discover_nested_card_dirs``: that one walks DOWN into ``walk_root``'s
        descendants; this one walks UP through its parents. Covers the case where THIS store's
        corpus root is a narrower subfolder of an already-indexed wider corpus (e.g. bootstrapping
        a dissertation folder several levels below ``~/hq`` when ``~/hq`` itself already has a
        completed ``.quest-context`` covering it) -- without this, a narrower root can never see an
        ancestor's index, since an ancestor is by definition not inside ``walk_root``.

        Walks up from ``walk_root``'s parent, checking each ancestor for
        ``<ancestor>/.quest-context/bootstrap_meta.json``, and returns the FIRST (nearest) match --
        trusting it transitively, same as the downward case trusts a nested match to have already
        reused whatever is beneath IT. Bounded by ``_MAX_ANCESTOR_WALK_LEVELS`` and stops at the
        filesystem root either way. Never raises; returns ``None`` on any failure or no match.
        """
        try:
            current = walk_root.resolve().parent
            for _ in range(_MAX_ANCESTOR_WALK_LEVELS):
                if (current / ".quest-context" / _BOOTSTRAP_META_FILE).is_file():
                    return current
                parent = current.parent
                if parent == current:  # reached filesystem root
                    break
                current = parent
        except Exception:  # noqa: BLE001
            pass
        return None

    def _import_ancestor_cards(self, ancestor_root: Path, walk_root: Path) -> List[Dict[str, Any]]:
        """Reuse an ANCESTOR corpus's already-bootstrapped cards, filtered down to ``walk_root``.

        Mirror image of ``_import_nested_cards`` for the upward direction. An ancestor's cards can
        reference files anywhere across its much wider tree, most of which are irrelevant to this
        narrower root, so a card is only imported when at least one of its files falls under
        ``walk_root`` -- and when imported, its ``files`` list is TRIMMED to just that in-scope
        subset (paths rewritten from ancestor-root-relative to walk-root-relative, the reverse
        rewrite direction from the nested/downward case) so a file the ancestor's card references
        outside this narrower corpus is never treated as "covered" here.

        ``imported_from`` is the ancestor's path relative to ``walk_root`` (e.g. ``".."`` or
        ``"../.."``), so it round-trips through the same provenance/pruning logic in
        ``_bootstrap_inner`` that the downward case already uses. Never raises; returns [] on any
        failure.
        """
        try:
            walk_root_resolved = walk_root.resolve()
            ancestor_resolved = ancestor_root.resolve()
            depth = len(walk_root_resolved.relative_to(ancestor_resolved).parts)
            if depth < 1:
                return []
        except Exception:  # noqa: BLE001
            return []
        imported_from = "/".join([".."] * depth)
        id_prefix = _path_slug(imported_from)
        try:
            ancestor_cards = FilesystemCardRepository(str(ancestor_root / ".quest-context")).load_all()
        except Exception:  # noqa: BLE001
            return []
        imported: List[Dict[str, Any]] = []
        for card in ancestor_cards.values():
            try:
                files: List[str] = []
                for fe in card.get("files", []):
                    if not isinstance(fe, dict) or not fe.get("path"):
                        continue
                    try:
                        abs_path = (ancestor_root / fe["path"]).resolve()
                        rel = abs_path.relative_to(walk_root_resolved)
                    except ValueError:
                        continue  # this file is outside walk_root; drop it, keep the rest of the card
                    files.append(rel.as_posix())
                if not files:
                    continue
                cid = str(card.get("id") or "")
                if not cid:
                    continue
                imported.append({
                    "id": f"{id_prefix}--{cid}"[:200],
                    "name": card.get("name", ""),
                    "keywords": card.get("keywords", []),
                    "summary": card.get("summary", ""),
                    "description": card.get("description") or card.get("summary", ""),
                    "files": files,
                    "imported_from": imported_from,
                })
            except Exception:  # noqa: BLE001
                continue
        return imported

    def _bootstrap_inner(
        self,
        root: Optional[str] = None,
        *,
        provider=None,
        model: Optional[str] = None,
        max_files: int = 10000,
        max_cards: int = 5000,
        skip_unchanged: bool = False,
    ) -> int:
        """Actual bootstrap logic. May raise; callers wrap in try/except.

        Topic cards are semantic: the LLM (``provider``) decides what the topics are and which
        files belong to each, so a card can span unrelated directories. The pass is INCREMENTAL:

          1. Walk the corpus (with the existing skip logic) into a flat list of qualifying
             source file paths. No symbol extraction or fingerprinting yet.
          2. Diff against the existing cards: ``uncovered`` files are referenced by NO card;
             ``stale_covered`` files are referenced by a card but their sha256 has changed. Only
             these drive LLM work. If both are empty the store is up to date (return 0).
          3. Run the 3-stage LLM fan-out over just the uncovered files, deduping against the
             existing cards. Separately regenerate the cards that reference stale files.
          4. Fingerprint every referenced file in parallel, build the final JSON, apply the
             ``skip_unchanged`` check, and write atomically. Write ``bootstrap_meta.json`` at the
             end when any card was written.

        With no ``provider`` no cards can be identified, so this returns 0 (a no-op).

        CANCELLATION: ``close()`` stops the pass at the next checkpoint (entry, each walked
        directory, before the LLM fan-out, before the fingerprint pass) and returns 0 rather than
        keep walking and shelling out to git for a store nobody owns any more.
        """
        if self._closed.is_set():
            return 0
        walk_root = Path(root).resolve() if root else self._repo_root
        if walk_root is None or not walk_root.is_dir():
            return 0

        # Resolve the cards_dir so we can skip it if it's inside the walk root.
        cards_dir_resolved = self._cards_dir.resolve()
        skip_dirs = effective_skip_dirs(walk_root)

        # --- Pass 1: walk the tree and collect qualifying source file paths (flat list) ---
        file_paths: List[str] = []
        file_count = 0
        for dirpath, dirnames, filenames in os.walk(walk_root):
            if file_count >= max_files or self._closed.is_set():
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
                if file_count >= max_files:
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
                file_paths.append(str(fpath.relative_to(walk_root)))

        if not file_paths or self._closed.is_set():
            return 0

        existing = self._load_all()
        existing_cards = list(existing.values())

        # --- Nested-store reuse: a sub-corpus already bootstrapped by its OWN QAR instance is
        # reused wholesale instead of re-discovered here. Pure filesystem reuse (no LLM call, no
        # provider needed), so this runs even when this store has none wired. A card previously
        # imported from a nested store that no longer offers it (renamed/deleted/de-bootstrapped)
        # is pruned so imports never drift from their source.
        imported_cards: List[Dict[str, Any]] = []
        imported_covered: Set[str] = set()
        if self._reuse_nested_cards:
            for nested_root in self._discover_nested_card_dirs(walk_root, skip_dirs):
                for ic in self._import_nested_cards(nested_root, walk_root):
                    imported_cards.append(ic)
                    imported_covered.update(ic.get("files", []))
            # Mirror image: also look UPWARD for an already-indexed ANCESTOR corpus (this root is
            # the narrower one, e.g. bootstrapping a subfolder of an already-indexed wide corpus).
            # Both directions can contribute at once (an indexed ancestor above AND an indexed
            # descendant below), so this is not an elif.
            ancestor_root = self._discover_ancestor_card_dir(walk_root)
            if ancestor_root is not None:
                for ic in self._import_ancestor_cards(ancestor_root, walk_root):
                    imported_cards.append(ic)
                    imported_covered.update(ic.get("files", []))
            if imported_cards:
                _log.info(
                    "context index: reusing %d card(s) from %d corpus root(s) (nested and/or "
                    "ancestor), covering %d file(s) with zero LLM calls",
                    len(imported_cards),
                    len({ic["imported_from"] for ic in imported_cards}),
                    len(imported_covered),
                )
            imported_ids_now = {ic["id"] for ic in imported_cards}
            for card in existing_cards:
                prov = card.get("provenance")
                cid = card.get("id")
                if (
                    isinstance(prov, dict)
                    and prov.get("imported_from")
                    and cid
                    and cid not in imported_ids_now
                ):
                    try:
                        self._repo.delete(cid)
                    except Exception:  # noqa: BLE001
                        pass

        # Topic cards for genuinely NEW content require semantic understanding. Without a
        # provider, only cards reused from a nested store (above) can be produced.
        if provider is None and not imported_cards:
            _log.warning(
                "context index: bootstrap skipped — no model provider wired, so no semantic "
                "topic cards can be identified (cards accumulate via record() instead)"
            )
            return 0

        # --- Incremental diff: what is uncovered (in no card) vs stale (covered but changed) ---
        covered: Set[str] = set()
        for card in existing_cards:
            for fe in card.get("files", []):
                p = fe.get("path", "") if isinstance(fe, dict) else fe
                if p:
                    covered.add(p)

        uncovered = [p for p in file_paths if p not in covered and p not in imported_covered]
        # A covered file is stale when its current sha256 differs from any card's stored sha.
        stale_covered: List[str] = []
        if covered:
            walked = set(file_paths)
            for card in existing_cards:
                for fe in card.get("files", []):
                    if not isinstance(fe, dict):
                        continue
                    p = fe.get("path", "")
                    if not p or p not in walked:
                        continue
                    stored = fe.get("sha256", "")
                    if not stored:
                        continue
                    fp = self._fingerprint(p)
                    cur = fp.get("sha256", "")
                    if cur and cur != stored and p not in stale_covered:
                        stale_covered.append(p)

        # On the very first bootstrap (no existing cards) everything not covered by an import
        # is "uncovered".
        if not existing_cards:
            uncovered = [p for p in file_paths if p not in imported_covered]

        # --- Feature migration: cards whose file entries have an outdated per-feature version ---
        # These cards already have correct LLM-generated content; only the cheap computed fields
        # (e.g. tfdfidf_terms) need refreshing.  They are added to topic_cards WITHOUT triggering
        # LLM calls — stage 4b recomputes the field, stage 5 writes the updated card.
        # The per-entry "tfdfidf_v" field IS the checkpoint: entries already at _TFDFIDF_VERSION
        # are skipped automatically, so an interrupted migration resumes on the next startup.
        tfdfidf_migration_cards: List[Dict[str, Any]] = []
        for card in existing_cards:
            cid = card.get("id", "")
            if not cid:
                continue
            card_files = [fe for fe in card.get("files", []) if isinstance(fe, dict) and fe.get("path")]
            if any(fe.get("tfdfidf_v", 0) < _TFDFIDF_VERSION for fe in card_files):
                tfdfidf_migration_cards.append({
                    "id": cid,
                    "name": card.get("name", ""),
                    "keywords": card.get("keywords", []),
                    "summary": card.get("summary", ""),
                    "description": card.get("description", ""),
                    "files": [fe["path"] for fe in card_files],
                })
        if tfdfidf_migration_cards:
            _log.info(
                "context index: %d card(s) have file entries below tfdfidf v%d — will update",
                len(tfdfidf_migration_cards), _TFDFIDF_VERSION,
            )

        if not uncovered and not stale_covered and not tfdfidf_migration_cards and not imported_cards:
            _log.info("context index: all files covered and up to date")
            return 0

        _log.info(
            "context index: %d new file(s) found (not in any existing card), %d stale file(s) "
            "— processing", len(uncovered), len(stale_covered),
        )

        # --- LLM: identify topic cards for the NEW (uncovered) files, deduping vs existing ---
        topic_cards: List[Dict[str, Any]] = []
        if uncovered and provider is not None:
            _log.info("context index: stage 2 — analyzing %d new files for topics", len(uncovered))
            topic_cards = _llm_topic_cards(
                uncovered, provider, model=model, existing_cards=existing_cards, walk_root=walk_root
            )
            _log.info("context index: identified %d topic card(s) from new files", len(topic_cards))

        # Merge nested-imported cards now (before stale-covered regen below): a file re-imported
        # unchanged from a nested store can also show up in ``stale_covered`` versus THIS store's
        # own last-written fingerprint of it, and the regen loop already skips any id present in
        # ``topic_cards`` — this avoids a wasted LLM regen call on content that isn't actually new,
        # just imported. Different corpus roots never legitimately share a topic, so no LLM dedup
        # is needed here.
        if imported_cards:
            queued_ids = {tc.get("id") for tc in topic_cards}
            for ic in imported_cards:
                if ic.get("id") not in queued_ids:
                    topic_cards.append(ic)

        # --- Stale-covered: regenerate the cards that reference any stale file ---
        # Identify the cards touching a stale file and re-run topic extraction over each card's
        # file set so its summary/keywords/files reflect the current code, keeping the card id.
        if stale_covered and provider is not None:
            _log.info("context index: stage 3 — regenerating %d stale card(s)", len(stale_covered))
            stale_set = set(stale_covered)
            regen_ids = {
                card.get("id")
                for card in existing_cards
                for fe in card.get("files", [])
                if isinstance(fe, dict) and fe.get("path", "") in stale_set
            }
            existing_ids_new = {tc.get("id") for tc in topic_cards}
            for card in existing_cards:
                cid = card.get("id")
                if cid not in regen_ids or cid in existing_ids_new:
                    continue
                files = [
                    fe.get("path", "") for fe in card.get("files", [])
                    if isinstance(fe, dict) and fe.get("path", "")
                ]
                files = [f for f in files if f]
                if not files:
                    continue
                area = {
                    "name": card.get("name", cid),
                    "description": card.get("summary", ""),
                    "files": files,
                }
                regenerated = _extract_topic_cards(area, set(files), provider, model, walk_root=walk_root)
                if regenerated:
                    # Keep the original card id on the first regenerated card so we upsert in place.
                    regenerated[0]["id"] = cid
                    topic_cards.extend(regenerated)
                else:
                    # Extraction failed: keep the card but re-pin (rebuild from its own fields).
                    topic_cards.append({
                        "id": cid,
                        "name": card.get("name", ""),
                        "keywords": card.get("keywords", []),
                        "summary": card.get("summary", ""),
                        "files": files,
                    })

        # Merge tfdfidf migration cards (don't add duplicates already queued for LLM regen).
        if tfdfidf_migration_cards:
            queued_ids = {tc.get("id") for tc in topic_cards}
            for mc in tfdfidf_migration_cards:
                if mc.get("id") not in queued_ids:
                    topic_cards.append(mc)

        if not topic_cards:
            return 0

        self._cards_dir.mkdir(parents=True, exist_ok=True)

        # --- Pass 2: fingerprint every file referenced by any topic card, in parallel ---
        # Checkpoint: the fingerprint pass is the git-subprocess-heavy one, so a closed store stops
        # before it rather than fanning out ``git hash-object`` calls it will never use.
        if self._closed.is_set():
            return 0
        referenced: List[str] = []
        seen: Set[str] = set()
        for tc in topic_cards:
            for rel in tc.get("files", []):
                if rel not in seen:
                    seen.add(rel)
                    referenced.append(rel)

        fp_map: Dict[str, Dict[str, Any]] = {rel: {} for rel in referenced}
        if referenced:
            _log.info("context index: stage 4 — fingerprinting %d file(s)", len(referenced))
            n_workers = min(8, len(referenced))
            fp_results = _run_parallel(
                [lambda r=rel: (r, self._fingerprint(r)) for rel in referenced],
                max_workers=n_workers,
            )
            for item in fp_results:
                if item is not None:
                    rel, fp = item
                    fp_map[rel] = fp or {}

        # --- Pass 2b: compute per-file TF-DF-IDF term signatures from actual content ---
        # Read each referenced file's content, compute corpus-level DF, then compute the
        # top-15 most distinctive terms per file (high TF in this file, rare across corpus).
        # These are stored as ``tfdfidf_terms`` in each file entry so ``_card_term_weights``
        # can use actual content signal instead of relying only on LLM-assigned keywords.
        tfdfidf_terms_map: Dict[str, List[str]] = {}
        if referenced and walk_root is not None:
            _log.info("context index: stage 4b — computing TF-DF-IDF term signatures for %d file(s)", len(referenced))

            def _read_file_terms(rel: str) -> tuple:
                try:
                    p = walk_root / rel
                    if p.exists() and p.stat().st_size <= _BOOTSTRAP_MAX_BYTES:
                        text = p.read_text(encoding="utf-8", errors="replace")
                        return rel, _tokenize(text)
                except Exception:  # noqa: BLE001
                    pass
                return rel, set()

            n_workers_content = min(8, len(referenced))
            content_results = _run_parallel(
                [lambda r=rel: _read_file_terms(r) for rel in referenced],
                max_workers=n_workers_content,
            )

            # Build corpus DF (how many files contain each term)
            file_term_sets: Dict[str, Set[str]] = {}
            for item in content_results:
                if item is not None:
                    rel, terms = item
                    file_term_sets[rel] = terms
            corpus_df_content: Dict[str, int] = {}
            for terms in file_term_sets.values():
                for t in terms:
                    corpus_df_content[t] = corpus_df_content.get(t, 0) + 1
            N_corpus = max(len(file_term_sets), 1)

            # Compute top-15 distinctive terms per file: IDF = log((N+1)/(df+1))+1, TF=presence
            for rel, terms in file_term_sets.items():
                if not terms:
                    tfdfidf_terms_map[rel] = []
                    continue
                term_scores = {
                    t: math.log((N_corpus + 1) / (corpus_df_content.get(t, 0) + 1)) + 1
                    for t in terms
                }
                top = sorted(term_scores, key=term_scores.__getitem__, reverse=True)[:15]
                tfdfidf_terms_map[rel] = top

        # --- Pass 3: build and write one card per topic ---
        _log.info("context index: stage 5 — writing %d card(s)", len(topic_cards))
        cards_written = 0
        for tc in topic_cards:
            if cards_written >= max_cards:
                break

            card_id = tc["id"]
            rels = tc.get("files", [])

            file_dicts: List[Dict[str, Any]] = []
            for rel in rels:
                fp = fp_map.get(rel, {})
                file_dicts.append({
                    "path": rel,
                    "sha256": fp.get("sha256", ""),
                    "mtime": fp.get("mtime", 0.0),
                    "git_sha": fp.get("git_sha", ""),
                    "why": "",
                    "symbols": [],
                    "tfdfidf_terms": tfdfidf_terms_map.get(rel, []),
                    "tfdfidf_v": _TFDFIDF_VERSION,
                })

            # Load existing card so we preserve usage_count / last_outcome if present.
            loaded_existing = self._repo.read(card_id)
            existing: Dict[str, Any] = loaded_existing if isinstance(loaded_existing, dict) else {}

            # Incremental refresh: skip a topic only if every file's sha256 is unchanged AND
            # every file entry already carries the current per-feature versions.
            if skip_unchanged and existing:
                old_file_data = {
                    fe.get("path", ""): fe
                    for fe in existing.get("files", [])
                    if isinstance(fe, dict)
                }
                unchanged = bool(file_dicts) and all(
                    fd["sha256"]
                    and old_file_data.get(fd["path"], {}).get("sha256", "") == fd["sha256"]
                    and old_file_data.get(fd["path"], {}).get("tfdfidf_v", 0) >= _TFDFIDF_VERSION
                    for fd in file_dicts
                ) and len(old_file_data) == len(file_dicts)
                if unchanged:
                    continue

            card: Dict[str, Any] = {
                "id": card_id,
                "name": tc.get("name", ""),
                "keywords": tc.get("keywords", []),
                "summary": tc.get("summary", ""),
                "description": tc.get("summary", ""),
                "files": file_dicts,
                "conventions": [],
                "provenance": {
                    "created_by_task": "bootstrap",
                    "model": "",
                    "created_at": "",
                    "last_verified_at": "",
                    **({"imported_from": tc["imported_from"]} if tc.get("imported_from") else {}),
                },
                "usage_count": existing.get("usage_count", 0),
                "last_outcome": existing.get("last_outcome", "unknown"),
            }

            # Persist via the repository (skip if dry-run mode).
            if not self._dry_run:
                if self._repo.write(card_id, card):
                    cards_written += 1
            else:
                # In dry-run mode, count the card but don't write it.
                cards_written += 1

        # Invalidate cache after all writes.
        self._cache_dirty = True
        return cards_written

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _card_term_weights(self, card: Dict[str, Any]) -> Dict[str, float]:
        """Build a term -> max-weight map for field-weighted scoring.

        Each term's weight is the MAXIMUM across all sources it appears in:
          keywords (LLM-tagged)    3.0  -- most intentional signal
          summary + filename stem  2.0  -- filename is a title for the file
          symbols (fn/class names) 1.0  -- meaningful but uncurated
          directory components     0.5  -- structural noise
          file extension           0.0  -- dropped (.py/.ts adds no signal)

        Max (not sum) prevents inflating scores when a keyword term also appears
        in the summary -- the keyword weight already captures that signal.

        A LEARNED card (one an updater wrote from a finished run, whose knowledge is typed content
        REFERENCES rather than bootstrapped file entries) carries ``name``/``description`` instead of
        ``keywords``/``summary``, and its file paths live in content locators, not in ``files``.
        Those fields are indexed here at the same intent-weighted levels, so such a card is scored on
        the same footing as a bootstrapped one instead of being systematically under-scored and
        filtered out below the confidence gate (which made a run's learned references unreachable to
        the next run).
        """
        weights: Dict[str, float] = {}

        def _add(text: str, w: float) -> None:
            for t in _tokenize(text):
                if w > weights.get(t, 0.0):
                    weights[t] = w

        for kw in card.get("keywords", []):
            _add(kw, 3.0)
        # ``name`` is as intentional a signal as an LLM keyword (someone named the topic); the
        # ``description`` sits at summary level. Both are no-ops on a card that has neither.
        _add(card.get("name", ""), 3.0)
        _add(card.get("summary", ""), 2.0)
        _add(card.get("description", ""), 2.0)

        for fe in card.get("files", []):
            path = fe.get("path", "")
            if path:
                p = Path(path)
                # Filename stem (e.g. "ChatWindow" from ChatWindow.tsx) → title-level signal
                _add(re.sub(r"[_\-]", " ", p.stem), 2.0)
                # Directory components → structural noise
                for part in p.parent.parts:
                    _add(re.sub(r"[_\-]", " ", part), 0.5)
            # TF-DF-IDF terms: top-15 distinctive terms from actual file content.
            # Weight 2.5 — above filename (2.0) and symbols (1.0), below LLM keywords (3.0).
            # These are only present in cards bootstrapped at v4+; older cards degrade gracefully.
            for term in fe.get("tfdfidf_terms", []):
                _add(term, 2.5)
            for sym in fe.get("symbols", []):
                _add(sym, 1.0)

        # Source-agnostic CONTENT items: tokenize each item's TARGET (the locator: a file path, a
        # collection name/id), its ``why``, and any note text, so a card with ZERO files (a pure
        # reference/note card, e.g. one a deep run's future context produced) is selectable by IDF
        # and vectors. Weight 2.0 — title-level signal, on par with the summary, below keywords.
        # A file reference's PATH is part of that item text (``locator_label``), so "where does the
        # relay config live" reaches a card that points at ``config/relay.toml`` even though the
        # card pins no files of its own. Structural path components (``src``, ``lib``) are common
        # across cards, so IDF damps them on its own; no extra down-weighting is needed.
        for item in _normalize_content(card.get("content")):
            _add(_content_item_text(item), 2.0)

        return weights

    def _card_searchable_terms(self, card: Dict[str, Any]) -> Set[str]:
        """Return the set of all terms in a card (for DF computation)."""
        return set(self._card_term_weights(card).keys())

    def _fallback_file_search(self, task_kws: Set[str]) -> AssembledContext:
        """When no cards score above threshold, grep the raw file corpus for query keywords.

        This gives the brain relevant file snippets even when the card index is cold or
        misses novel component names, class names, or camelCase identifiers.  Returns an
        empty AssembledContext when no files are reachable or no hits are found.
        """
        if self._repo_root is None or not self._repo_root.is_dir():
            return AssembledContext()

        kw_list = sorted(task_kws, key=len, reverse=True)[:12]
        if not kw_list:
            return AssembledContext()

        pattern = "|".join(re.escape(k) for k in kw_list)
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            return AssembledContext()

        skip_dirs = effective_skip_dirs(self._repo_root)
        hits_by_file: Dict[str, List[str]] = {}
        try:
            for dirpath, dirnames, filenames in os.walk(self._repo_root):
                prune_dirnames(dirnames, current=Path(dirpath), base_skip=skip_dirs)
                for fn in filenames:
                    if fn.startswith("."):
                        continue
                    fpath = Path(dirpath) / fn
                    if fpath.suffix not in _SOURCE_EXTS:
                        continue
                    try:
                        if fpath.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                            continue
                        text = fpath.read_text(encoding="utf-8", errors="replace")
                        matching = [ln.rstrip() for ln in text.splitlines() if rx.search(ln)]
                        if matching:
                            rel = str(fpath.relative_to(self._repo_root))
                            hits_by_file[rel] = matching[:6]
                    except OSError:
                        pass
                    if len(hits_by_file) >= 20:
                        break
                if len(hits_by_file) >= 20:
                    break
        except Exception:  # noqa: BLE001
            return AssembledContext()

        if not hits_by_file:
            return AssembledContext()

        _log.info("context index: no cards matched; fallback file search found %d file(s)",
                  len(hits_by_file))
        parts = [
            "No context cards matched this query. Relevant lines found by direct file search:\n"
        ]
        for rel, lines in sorted(hits_by_file.items())[:12]:
            parts.append(f"**{rel}**")
            for ln in lines:
                snippet = ln[:200].rstrip() + ("…" if len(ln) > 200 else "")
                parts.append(f"  {snippet}")
            parts.append("")
        return AssembledContext(
            context_view="\n".join(parts),
            sources=[{"adapter": "fallback_grep", "label": "direct file search",
                      "items": list(hits_by_file.keys())}],
        )

    def _assemble_inner(self, task_text: str, *,
                        meta: Optional[Dict[str, Any]] = None) -> AssembledContext:
        task_kws = _tokenize(task_text)
        if not task_kws:
            return AssembledContext()

        # QUEST-FOLDER SCOPING: when this run's meta carries a goal/quest id that quest_folder_map
        # maps to a folder, resolve it ONCE to the repo-root-relative prefix cards are matched
        # against. goal_id is checked FIRST, mirroring the poller's _quest_folder_for: a personal
        # "goal is the hub" task carries its id there (often with no quest_id at all). None (the
        # common case: no map, or no meta, or neither id is mapped) is a no-op.
        folder_prefix: Optional[str] = None
        if meta and self._quest_folder_map:
            for _id_key in ("goal_id", "quest_id"):
                _id = meta.get(_id_key)
                if _id and str(_id) in self._quest_folder_map:
                    folder_prefix = self._quest_folder_map[str(_id)]
                    break

        # Candidate pool for the keyword arm. When the repository exposes NATIVE text search
        # (a Qdrant-backed repo, say), let it serve the candidates directly instead of scanning
        # every card in memory; the store still applies its own IDF ranking / confidence gate /
        # recency over that smaller pool below, so selection behavior is unchanged in kind. Detected
        # by duck-typing (never isinstance): a repo without ``search_cards`` (e.g. the default
        # filesystem repo) falls through to the full in-app IDF over ``_load_all()``, byte-for-byte
        # as before. A repo whose ``search_cards`` returns ``None`` also falls back.
        cards = self._repo_text_search_candidates(task_text)
        if cards is None:
            cards = self._load_all()
        if not cards:
            return AssembledContext()

        # ---- Field-weighted TF-IDF scoring ----
        # Each card has a term->weight map (keywords=3, summary+filename=2,
        # symbols=1, dir components=0.5, extensions dropped). DF is computed
        # from presence (a term counts once per card regardless of weight) so
        # rare terms still get high IDF. Score = sum(field_weight * IDF).
        card_weight_maps: Dict[str, Dict[str, float]] = {
            cid: self._card_term_weights(c) for cid, c in cards.items()
        }
        N = len(cards)

        # Compute document frequency per term (presence-based across all cards).
        df: Dict[str, int] = {}
        for tw in card_weight_maps.values():
            for term in tw:
                df[term] = df.get(term, 0) + 1

        # IDF(term) = log((N+1)/(df+1)) + 1  (smooth, always >= 1).
        def _idf(term: str) -> float:
            return math.log((N + 1) / (df.get(term, 0) + 1)) + 1.0

        # Score each card: sum of field_weight * IDF for each query term present.
        # Test-file cards are down-weighted by their stored ``weight`` (default 0.5).
        # Tie-break by (usage_count DESC, last_verified_at DESC).
        scored: List[tuple] = []  # (-score, -usage_count, -last_verified_ts, card_dict)
        idf_score_map: Dict[str, float] = {}  # card_id -> raw score (for relevance display)
        for cid, card in cards.items():
            tw = card_weight_maps[cid]
            base_score = sum(tw[t] * _idf(t) for t in task_kws if t in tw)
            # Apply test-file penalty.
            card_weight = float(card.get("weight", _SOURCE_FILE_WEIGHT))
            score = base_score * card_weight
            # QUEST-FOLDER BOOST (pre-gate; see _QUEST_FOLDER_BOOST for why this differs from the
            # post-gate recency/usage boost below).
            if folder_prefix and self._card_pins_folder(card, folder_prefix):
                score *= _QUEST_FOLDER_BOOST
            # CONFIDENCE GATE: only a match that clears the threshold is injected. A weak match
            # contributes NOTHING, so an uncertain query yields an empty context view and the run
            # falls back to plain Claude Code (never worse). This is what makes the layer dominate:
            # it adds a grounding only when confident, and otherwise equals the baseline.
            if score >= self._confidence_threshold:
                idf_score_map[cid] = score
                usage = card.get("usage_count", 0)
                verified_at = card.get("provenance", {}).get("last_verified_at", "") or ""
                # Gate on the un-boosted relevance score above; rank by a recency/usage-boosted
                # score so a card the user just relied on wins a near-tie (bounded, never resurrects
                # an irrelevant card because the gate already passed on relevance alone).
                rank_score = score * self._recency_boost_factor(card)
                scored.append((-rank_score, -usage, -len(verified_at), verified_at, card))

        if not scored:
            return self._fallback_file_search(task_kws)

        # Sort: primary descending score, then tie-break descending usage_count,
        # then tie-break by presence of a verified_at string (longer = more recent).
        scored.sort(key=lambda x: (x[0], x[1], x[2]))
        # Take up to 2x max_cards as IDF candidates so the LLM filter has enough to work with.
        idf_candidates = [x[4] for x in scored[: self._max_cards * 2]]

        # ---- LLM relevance filter (when a provider is wired) ----
        # IDF finds cards that share keywords with the task.  The LLM filter culls
        # cards that share keywords but are semantically unrelated to the task.
        # Also ranks the files within each kept card by relevance and returns only
        # the relevant ones.
        llm_meta_map: Dict[str, Any] = {}  # card_id -> CardMetadata from LLM filter
        if self._provider is not None and idf_candidates:
            try:
                from ..core.card_filter import filter_cards_by_relevance
                # The filter judges a card by its TITLE and the FILES it covers. A learned
                # reference card has neither a bootstrapped ``summary`` nor ``files`` entries: its
                # title is its ``name``/``description`` and the files it covers are its file
                # REFERENCES. Feeding those in is what stops the filter from seeing an untitled,
                # zero-file card and culling the very card that carries the run's hard-won paths.
                candidate_dicts = [
                    {
                        "id": c.get("id", ""),
                        "title": _card_display_title(c),
                        "files": _card_covered_paths(c),
                        "adapter": "keyword",
                    }
                    for c in idf_candidates
                ]
                filtered = filter_cards_by_relevance(
                    task_text, candidate_dicts, model_provider=self._provider,
                    model=self._filter_model,
                )
                for cm in filtered:
                    llm_meta_map[cm.id] = cm
                # Keep only LLM-approved cards, in LLM relevance order.
                idf_candidates = [
                    c for c in idf_candidates if c.get("id", "") in llm_meta_map
                ]
                idf_candidates.sort(
                    key=lambda c: llm_meta_map[c.get("id", "")].relevance_score,
                    reverse=True,
                )
            except Exception:  # noqa: BLE001
                _log.debug("LLM card filter failed, using IDF ranking", exc_info=True)

        top_cards = idf_candidates[: self._max_cards]

        # QUERY-AWARE TIME FILTER (spec v3 work package C, item level): when the caller's meta
        # carries a ``time_range`` (the shape ``parse_goal_condition_reply`` emits, threaded in by
        # the orchestrator as ``meta["time_range"]``), apply it as a HARD filter over each selected
        # card's CONTENT ITEMS by their ``ts``: an item carrying a real timestamp outside the range
        # is dropped before rendering, while an item with NO timestamp is always kept (absence of a
        # timestamp must never hide content). A card whose items are ALL filtered out is dropped
        # from the result entirely; when the filter would empty EVERY selected card, it degrades to
        # the unfiltered selection with an explicit note line (never a silent empty). Filtered
        # cards are shallow COPIES so the in-memory cache's card dicts are never mutated. Absent
        # meta / no ``time_range`` key: byte-for-byte today's behavior.
        time_filter_note = ""
        time_range = (meta or {}).get("time_range")
        if time_range and top_cards:
            time_filtered_cards: List[Dict[str, Any]] = []
            for card in top_cards:
                content = _normalize_content(card.get("content"))
                if not content:
                    # No dated content to filter on (a file-only card): keep it as-is.
                    time_filtered_cards.append(card)
                    continue
                kept_items = filter_content_by_time_range(content, time_range)
                if not kept_items:
                    continue  # every item fell outside the range: drop the whole card
                if len(kept_items) != len(content):
                    card = {**card, "content": kept_items}
                time_filtered_cards.append(card)
            if time_filtered_cards:
                top_cards = time_filtered_cards
            else:
                time_filter_note = (
                    "(Note: no card content matched the requested time range. "
                    "Showing context without the time filter.)"
                )

        # Render each card, checking file freshness.
        # Collect every (card_index, file_entry) pair that needs a fingerprint check, then
        # compute all sha256 reads concurrently -- sha256 I/O releases the GIL so threads
        # help whenever a card set pins many files.  Order is preserved; a failed
        # fingerprint yields an empty dict (same as today, never raises).
        #
        # Build the flat list of (card_idx, fe) pairs to fingerprint.
        # Files are sorted by mtime descending (most recently modified first) before
        # fingerprinting so both the freshness check and the rendered view order matches
        # the user expectation of "most recent sources first".
        fp_jobs: List[tuple] = []  # (card_idx, fe_idx, fpath)
        for ci, card in enumerate(top_cards):
            sorted_files = sorted(
                card.get("files", []),
                key=lambda fe: fe.get("mtime", 0.0),
                reverse=True,
            )
            card["_sorted_files"] = sorted_files  # stash for render phase below
            for fi, fe in enumerate(sorted_files):
                fp_jobs.append((ci, fi, fe.get("path", "")))

        # Dispatch fingerprint reads in parallel.
        fp_results: Dict[tuple, Dict[str, Any]] = {}  # (ci, fi) -> fp dict
        if fp_jobs:
            n_workers = min(8, len(fp_jobs))
            raw = _run_parallel(
                [lambda ci=ci, fi=fi, fp=fpath: ((ci, fi), self._fingerprint(fp))
                 for ci, fi, fpath in fp_jobs],
                max_workers=n_workers,
            )
            for item in raw:
                if item is not None:
                    key, fp = item
                    fp_results[key] = fp or {}

        view_parts: List[str] = []
        card_ids: List[str] = []
        stale_list: List[str] = []

        for ci, card in enumerate(top_cards):
            card_id = card.get("id", "")
            card_ids.append(card_id)
            # A learned card has no ``summary``: its title is its name/description (see
            # _card_display_title). Without this it rendered as "(no summary)" and the worker was
            # handed a headless block of references.
            summary = _card_display_title(card) or "(no summary)"
            # Use the pre-sorted file list (mtime descending = most recently modified first).
            files = card.get("_sorted_files", card.get("files", []))

            # When the LLM filter ran, restrict displayed files to the LLM-ranked set.
            llm_cm = llm_meta_map.get(card_id)
            if llm_cm is not None and llm_cm.files:
                llm_path_set = set(llm_cm.files)
                # Keep LLM-selected files in mtime order; then append remaining files
                # so nothing is dropped from the staleness check.
                llm_files = [fe for fe in files if fe.get("path", "") in llm_path_set]
                other_files = [fe for fe in files if fe.get("path", "") not in llm_path_set]
                files = llm_files + other_files

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

            # Source-agnostic CONTENT (references resolved fresh + LLM notes), recency-bounded.
            content_lines = self._render_card_content(card, task_kws)

            file_block = (
                "\n".join(file_lines) if file_lines
                else ("  (no pinned files)" if not content_lines else "")
            )
            conventions = card.get("conventions", [])
            conv_block = (
                "\n".join(f"  * {c}" for c in conventions[:10]) if conventions else ""
            )
            part = f"### Card: {card_id}\n{summary}"
            if file_lines or not content_lines:
                part += f"\n\nFiles:\n{file_block}"
            if content_lines:
                part += "\n\nContent:\n" + "\n".join(content_lines)
            if conv_block:
                part += f"\n\nConventions:\n{conv_block}"

            # Surface other source files in the same directories that weren't sampled
            # into this card during bootstrap, so the context engine can decide whether
            # to pull them in.
            if self._repo_root is not None:
                pinned_paths: Set[str] = {fe.get("path", "") for fe in card.get("files", [])}
                dirs_to_scan: Set[str] = set()
                for fp in pinned_paths:
                    if fp:
                        parent = str(Path(fp).parent)
                        dirs_to_scan.add(parent)
                sibling_paths: List[str] = []
                for d in sorted(dirs_to_scan):
                    dir_abs = self._repo_root / d
                    if dir_abs.is_dir():
                        try:
                            for entry in sorted(dir_abs.iterdir()):
                                if (entry.is_file()
                                        and entry.suffix in _SOURCE_EXTS
                                        and not entry.name.startswith(".")):
                                    rel = str(entry.relative_to(self._repo_root))
                                    if rel not in pinned_paths:
                                        sibling_paths.append(rel)
                        except OSError:
                            pass
                if sibling_paths:
                    shown = sibling_paths[:12]
                    more = len(sibling_paths) - len(shown)
                    sibling_lines = "\n".join(f"  - {p}" for p in shown)
                    if more:
                        sibling_lines += f"\n  - … and {more} more"
                    part += f"\n\nOther files in the same directories (not sampled into this card):\n{sibling_lines}"

            view_parts.append(part)

        context_view = "\n\n---\n\n".join(view_parts)
        if time_filter_note:
            # Time-filter degrade (see above): everything the filter touched was emptied, so the
            # UNFILTERED selection renders with an explicit note leading it, never a silent empty.
            context_view = (
                time_filter_note + "\n\n" + context_view if context_view else time_filter_note
            )

        # --- Context transparency: collect the file paths surfaced by this arm ----------------
        # One source entry per arm (keyword/IDF), listing the pinned file paths (mtime-sorted,
        # LLM-filtered when available) so the orchestrator can emit them for the user.
        _source_items: List[str] = []
        for card in top_cards:
            card_id = card.get("id", "")
            llm_cm = llm_meta_map.get(card_id)
            if llm_cm is not None and llm_cm.files:
                # LLM-ranked files only, in LLM relevance order
                for fp in llm_cm.files:
                    if fp and fp not in _source_items:
                        _source_items.append(fp)
            else:
                for fe in card.get("_sorted_files", card.get("files", [])):
                    fp = fe.get("path", "")
                    if fp and fp not in _source_items:
                        _source_items.append(fp)
        _sources = (
            [{"adapter": "keyword", "label": "docstring cards", "items": _source_items}]
            if _source_items else []
        )

        # --- Card metadata: populate selection info for UI display and transparency ---------
        # Build metadata for each selected card so the orchestrator can emit which cards were chosen.
        # Use the LLM-judged relevance score when available; otherwise use the normalized IDF score.
        _max_idf = max(idf_score_map.values()) if idf_score_map else 1.0
        card_metadata: List[Dict[str, Any]] = []
        for ci, card in enumerate(top_cards):
            card_id = card.get("id", "")
            llm_cm = llm_meta_map.get(card_id)
            # LLM-ranked files (already relevance-ordered), else mtime-sorted files
            if llm_cm is not None and llm_cm.files:
                display_files = llm_cm.files[:3]
                relevance_score = llm_cm.relevance_score
            else:
                # Pinned files first, then the card's file REFERENCES: a learned card pins no
                # files, so without the references it would report covering nothing.
                display_files = _card_covered_paths(card, limit=3)
                relevance_score = idf_score_map.get(card_id, 0.0) / _max_idf
            # Structured content ITEMS (id/type/why/locator/text/preview/pointer_eligible) resolved
            # FRESH, the same blocks that fed this card's view lines. The consolidating filter selects
            # from these, and the deep preamble materializes paste-vs-pointer from their locators.
            item_blocks = render_card_content_blocks(
                card,
                self._resolvers,
                task_kws=task_kws,
                max_refs=self._max_card_refs,
                max_ref_chars=self._max_card_ref_chars,
            )
            card_metadata.append({
                "id": card_id,
                "title": _card_display_title(card),
                "relevance_score": relevance_score,
                "file_count": len(card.get("files", [])),
                "files": display_files,
                "adapter": "keyword",
                "items": item_blocks,
                # OPTIONAL card taxonomy fields, passed through verbatim when the card carries them
                # (a card is a plain dict; a consumer is free to type and version its own cards).
                # ``card_type`` lets a consumer tell TOPIC cards apart from its other card kinds
                # (permissions, settings, derived doc cards) when it threads ideas by card, and
                # ``lifecycle`` says whether the work behind a card is still open. Absent on a card
                # that does not use them, so nothing changes for a consumer that never sets them.
                "card_type": card.get("card_type", ""),
                "lifecycle": card.get("lifecycle", ""),
                # The VERBATIM rendered section this card contributed to context_view (the whole
                # ``### Card: ...`` block: summary + Files listing + Content + Conventions). The hybrid
                # consolidator rebuilds from this so a keyword card's file listings are never lost when
                # consolidation engages (they are NOT content items). top_cards and view_parts are built
                # in lockstep above, so view_parts[ci] is this card's section.
                "rendered_section": view_parts[ci] if ci < len(view_parts) else "",
            })

        # --- PER-SOURCE USAGE RECENCY: bump what was actually USED, not merely what was held ------
        # The card-level ``usage_count`` (record()) only ever said THIS CARD was used. It could not
        # say WHICH of the card's sources carried the value, so a card that accumulated sources had
        # no way to let the dead ones sink. Here, at the seam where sources are actually RESOLVED
        # AND RENDERED into the context view, each source that made it in is stamped with
        # ``last_used_ts`` + ``use_count``. Sources merely listed on a card but not rendered stay
        # cold. Deliberately AFTER the render above: this turn's bytes are computed from the
        # PRE-bump values, so the same card rendered twice in a row is byte-identical (the recency
        # data never enters the rendered text, and every rendered item is re-warmed by the same
        # amount, which cannot reorder them). Best-effort: never raises, never blocks assembly.
        try:
            self._bump_source_usage(top_cards, card_metadata, llm_meta_map)
        except Exception:  # noqa: BLE001 — usage bookkeeping must never break assembly
            _log.debug("per-source usage bump failed", exc_info=True)

        # Clean up the temp sort key we stashed on card dicts (they're in-memory cache copies).
        for card in top_cards:
            card.pop("_sorted_files", None)

        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=list(dict.fromkeys(stale_list)),  # deduplicate, preserve order
            sources=_sources,
            card_metadata=card_metadata,
        )

    def _bump_source_usage(
        self,
        top_cards: List[Dict[str, Any]],
        card_metadata: List[Dict[str, Any]],
        llm_meta_map: Dict[str, Any],
    ) -> None:
        """Stamp per-source usage recency on every source this assembly actually rendered.

        For each selected card:
          * its CONTENT items that RESOLVED into rendered blocks (the ids in ``card_metadata``'s
            ``items``) get ``last_used_ts = now`` and ``use_count += 1``;
          * its FILE entries that were rendered as this task's relevant files get the same. When the
            LLM relevance filter ran, "relevant" is exactly the set it selected; without a filter
            every listed file is rendered, so every listed file counts as used.

        Type-agnostic: a content item is bumped by ID, so a conversation, collection, query, or note
        reference is warmed exactly like a file. Never raises. Writes nothing in dry-run.
        """
        if self._dry_run:
            return
        now = time.time()
        meta_by_id = {cm.get("id"): cm for cm in card_metadata if isinstance(cm, dict)}
        for card in top_cards:
            card_id = card.get("id", "")
            if not card_id:
                continue
            cm = meta_by_id.get(card_id) or {}
            used_item_ids = {
                str(b.get("id")) for b in (cm.get("items") or [])
                if isinstance(b, dict) and b.get("id")
            }
            llm_cm = llm_meta_map.get(card_id)
            if llm_cm is not None and getattr(llm_cm, "files", None):
                used_paths = {fp for fp in llm_cm.files if fp}
            else:
                used_paths = {
                    fe.get("path", "")
                    for fe in card.get("_sorted_files", card.get("files", []))
                    if fe.get("path")
                }
            if not used_item_ids and not used_paths:
                continue
            try:
                self._mark_sources_used_inner(card_id, used_item_ids, used_paths, now)
            except Exception:  # noqa: BLE001 — one card's bookkeeping never blocks the rest
                _log.debug("usage bump failed for card %s", card_id, exc_info=True)

    def _mark_sources_used_inner(
        self, card_id: str, item_ids: Set[str], file_paths: Set[str], now: float
    ) -> bool:
        """Read-modify-write ONE card's per-source usage fields. Returns True when it wrote.

        Reads the card through the persistence boundary (so it never writes back a stale in-memory
        copy), stamps the used content items and file entries, and persists only when something
        actually changed. A card whose sources predate the fields simply gains them here: no
        migration pass, no rewrite storm (a card is only ever touched when it is USED).
        """
        loaded = self._repo.read(card_id)
        if not isinstance(loaded, dict):
            return False
        card = loaded
        changed = False
        if item_ids:
            content = _normalize_content(card.get("content"))
            if content and _mark_items_used(content, item_ids, now=now,
                                            min_interval=_SOURCE_USAGE_MIN_INTERVAL_SECONDS):
                card["content"] = content
                changed = True
        if file_paths:
            for fe in card.get("files", []) or []:
                if not isinstance(fe, dict) or fe.get("path") not in file_paths:
                    continue
                try:
                    prev = float(fe.get("last_used_ts") or 0.0)
                except (TypeError, ValueError):
                    prev = 0.0
                if (now - prev) < _SOURCE_USAGE_MIN_INTERVAL_SECONDS:
                    continue  # same use, already stamped moments ago (see mark_items_used)
                fe["last_used_ts"] = now
                try:
                    fe["use_count"] = int(fe.get("use_count") or 0) + 1
                except (TypeError, ValueError):
                    fe["use_count"] = 1
                changed = True
        if not changed:
            return False
        self._write_card_atomic(card_id, card)
        return True

    def mark_sources_used(
        self,
        card_id: str,
        *,
        item_ids: Optional[List[str]] = None,
        file_paths: Optional[List[str]] = None,
        now: Optional[float] = None,
    ) -> bool:
        """Public: stamp per-source usage recency on ``card_id``'s named sources. Never raises.

        The assembly path bumps automatically (see ``_bump_source_usage``); this is the seam for any
        OTHER consumer or assembler arm that renders a card's sources into context and wants the
        same heat signal recorded. Returns True when the card was written.
        """
        try:
            return self._mark_sources_used_inner(
                card_id, set(item_ids or []), set(file_paths or []),
                float(now) if now is not None else time.time(),
            )
        except Exception:  # noqa: BLE001
            return False

    def _write_card_atomic(self, card_id: str, card: Dict[str, Any]) -> None:
        """Persist ``card`` under ``card_id`` via the repository and mark the cache dirty.

        Shared by record() and the card-update API. Respects ``dry_run`` (writes nothing). Raises
        when the repository reports the write failed, so the public caller's try/except records the
        failure; callers above are all wrapped (never raises out of the public surface).
        """
        if self._dry_run:
            return
        if not self._repo.write(card_id, card):
            raise OSError(f"card repository write failed for {card_id!r}")
        self._cache_dirty = True

    def _update_card_inner(
        self,
        card_id: str,
        *,
        add: Optional[List[Dict[str, Any]]] = None,
        replace: Optional[List[Tuple[str, Dict[str, Any]]]] = None,
        remove: Optional[List[str]] = None,
        fields: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Read-modify-write a card's ``content`` and embedded ``fields``: apply field edits +
        add/replace/remove, trim, persist.

        Loads the card from disk (creating a minimal one if absent), applies ``fields`` (the embedded
        ``name``/``description``/``summary``), normalizes its existing content, applies removals, then
        replacements (in place, append if the id is unknown), then additions, applies the recency
        trim, and writes atomically. Returns True on a successful write. Changing ``name``/
        ``description``/``summary`` changes the card's embedding text, so ``export_for_embedding``
        re-fingerprints it and ``VectorStore.sync()`` re-embeds it on the next sync.

        CONSUMER-MANAGED CARDS. A card MAY declare parts of itself as owned by whoever writes it
        (a consumer that derives the card from its own source of truth and rewrites it whenever that
        source changes, e.g. one card per record in the consumer's database):

          * ``managed_fields``: list of embedded field names (``name``/``description``/``summary``)
            this API must NOT edit. An edit to a managed field is dropped; the rest still applies.
          * ``managed_items``: list of content-item ids this API must NOT remove or replace.

        Additions are never blocked, so a learning updater keeps accruing notes onto a managed card
        (which is the point of putting them on the same card) while the consumer-owned digest and its
        live reference stay exactly as the consumer wrote them. A card that declares neither key
        behaves exactly as before.
        """
        loaded = self._repo.read(card_id)
        card: Dict[str, Any] = loaded if isinstance(loaded, dict) else {}
        card.setdefault("id", card_id)

        # The card's own declaration of what its WRITER owns (see the docstring). Read from the
        # loaded card, so only a card that opted in is protected and every other card is untouched.
        managed_fields = _managed_names(card.get("managed_fields"))
        managed_items = _managed_names(card.get("managed_items"))

        # 0) embedded field edits (name/description/summary). These are part of the embedding text,
        # so changing them re-fingerprints the card and triggers re-embedding on the next sync.
        # A field the card declares as consumer-managed is skipped: the consumer rewrites it from its
        # own source of truth, so an edit here would be overwritten on the next sync anyway (and in
        # the meantime would leave the card describing something its live reference contradicts).
        if fields:
            for _k in ("name", "description", "summary"):
                if _k in fields and fields[_k] is not None and _k not in managed_fields:
                    card[_k] = fields[_k]
        # A card with no ``summary`` is invisible to everything that reads one (the keyword index,
        # the relevance filter, the rendered header). An updater supplies ``name``/``description``,
        # so seed the summary from them the first time. Never OVERWRITES an existing summary (a
        # bootstrapped card keeps its own), so this only fills a gap.
        if not (isinstance(card.get("summary"), str) and card["summary"].strip()):
            seed = card.get("description") or card.get("name") or ""
            if isinstance(seed, str) and seed.strip():
                card["summary"] = seed.strip()

        content = _normalize_content(card.get("content"))

        # 1) removals (a consumer-managed item is never removable through this API)
        if remove:
            remove_set = {r for r in remove if r and r not in managed_items}
            content = [it for it in content if it.get("id") not in remove_set]

        # 2) replacements (correct in place; append when the id is unknown)
        for item_id, new_raw in (replace or []):
            if item_id in managed_items:
                continue
            normalized = _normalize_content([new_raw])
            if not normalized:
                continue
            new_item = normalized[0]
            # Keep the targeted id unless the caller explicitly supplied a different one.
            if not (isinstance(new_raw, dict) and new_raw.get("id")):
                new_item["id"] = item_id
            replaced = False
            for i, it in enumerate(content):
                if it.get("id") == item_id:
                    content[i] = new_item
                    replaced = True
                    break
            if not replaced:
                content.append(new_item)

        # 3) additions
        if add:
            content.extend(_normalize_content(add))

        # 3b) collapse duplicate references (same collection id / file path / note) into one merged
        # item, keeping the existing item's stable id and the newest ts + freshest why, so re-adding
        # a reference across deep runs never bloats the card. Existing content precedes additions, so
        # an existing item keeps its id while picking up the newer ts/why from the re-add.
        content = _dedupe_content(content)

        # 4) recency trim + persist
        card["content"] = _trim_content_by_recency(content)
        self._write_card_atomic(card_id, card)
        return not self._dry_run

    def _record_inner(self, task_text: str, outcome: Dict[str, Any]) -> None:
        card_id = _card_slug(task_text)

        # Load existing card or start fresh.
        loaded = self._repo.read(card_id)
        card: Dict[str, Any] = loaded if isinstance(loaded, dict) else {}

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

        # Generalized: append any source-agnostic CONTENT items the outcome carries (notes,
        # collection/conversation refs) to the card's content list, then recency-trim. A single
        # dict is accepted as well as a list. This keeps record()'s file-pinning intact while
        # letting a run accumulate non-file content on the same card.
        raw_content = outcome.get("content")
        if raw_content:
            if isinstance(raw_content, dict):
                raw_content = [raw_content]
            existing_content = _normalize_content(card.get("content"))
            existing_content.extend(_normalize_content(raw_content))
            # Collapse duplicate references (same collection id / file path / note) before trimming,
            # so re-recording the same source on a card merges instead of accumulating copies.
            existing_content = _dedupe_content(existing_content)
            card["content"] = _trim_content_by_recency(existing_content)

        # Persist via the repository (skip if dry-run mode). Re-raise on failure so the outer
        # try/except in record() catches it; the cache is invalidated so assemble() sees the card.
        if not self._dry_run:
            if not self._repo.write(card_id, card):
                raise OSError(f"card repository write failed for {card_id!r}")
            self._cache_dirty = True

    def _read_file_fresh(self, path: str, max_chars: int = _MAX_CARD_REF_ITEM_CHARS) -> str:
        """Read a file's CURRENT content fresh, capped at ``max_chars``. Never raises; "" on failure.

        This is the fresh-read path the built-in ``file`` ReferenceResolver uses: a ``file`` content
        item is never a stale snapshot, it re-reads the live file every time the card is used.
        Resolves a relative path against ``repo_root`` (the same convention as ``_fingerprint``), so
        a path pinned by a run reads the real file regardless of the process cwd. Returns the head of
        the file (up to ``max_chars``), with a trailing ellipsis when truncated.
        """
        try:
            p = Path(path)
            if not p.is_absolute() and self._repo_root is not None:
                p = self._repo_root / path
            if not p.exists() or not p.is_file():
                return ""
            try:
                if p.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                    # Big file: read only the head we need rather than the whole thing.
                    with open(p, "r", encoding="utf-8", errors="replace") as fh:
                        text = fh.read(max_chars + 1)
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
            if len(text) > max_chars:
                text = text[: max_chars - 1].rstrip() + "…"
            return text
        except Exception:  # noqa: BLE001
            return ""

    def _render_card_content(self, card: Dict[str, Any], task_kws: Set[str]) -> List[str]:
        """Render a card's source-agnostic ``content`` items into context lines. Never raises.

        Thin wrapper over the SHARED ``render_card_content`` routine (in ``card_content_render``) so
        this keyword arm and the vector arm resolve a selected card's references identically. Passes
        this store's wired resolver registry and recency-bound limits.
        """
        return render_card_content(
            card,
            self._resolvers,
            task_kws=task_kws,
            max_refs=self._max_card_refs,
            max_ref_chars=self._max_card_ref_chars,
        )

    def render_card(self, card_id: str, *, meta: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Render ONE card by id for the brain's mid-loop ``{"card": <id>}`` read. Never raises.

        Reads the raw card through the persistence boundary (``CardRepository.read``) and renders its
        references FRESH with the SAME ``render_card_content`` routine the turn-start selection uses,
        so a directly-fetched card reads identically to a selected one. Returns None when the card is
        absent or has no renderable content (the brain turns None into a NAMED "no such card"
        observation). ``meta`` is accepted for interface parity and currently unused here.
        """
        try:
            card = self._repo.read(card_id)
            if not isinstance(card, dict):
                return None
            title = (card.get("name") or card.get("summary") or card_id or "").strip()
            body = "\n".join(self._render_card_content(card, set())).strip()
            if not body:
                return None
            return f"[{card_id}] {title}\n{body}" if title else body
        except Exception:  # noqa: BLE001 — a card fetch must never raise into the loop
            return None

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

        # Git blob SHA: best-effort, optional, fully wrapped. Skipped once the store is closed: this
        # is the one call in the index that leaves the process (``git hash-object``), so a background
        # pass that outlived its owner must not still be spawning it.
        if self._repo_root is not None and result["sha256"] and not self._closed.is_set():
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

    def _count_cards_on_disk(self) -> int:
        """Count the cards the repository holds (used for the bootstrap-meta card_count).

        Asks the repository rather than scanning the filesystem, so a non-filesystem repo reports
        the right count. Never raises.
        """
        try:
            return len(self._repo.load_all())
        except Exception:  # noqa: BLE001
            return 0

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
                    # --- Text: the SHARED card embed-text helper (``card_embed_text``) — the topic
                    # NAME, the docstring-rich description (fall back to summary), the card's
                    # source-agnostic CONTENT (note text + each item's ``why``), then keywords — so a
                    # pure note/collection card with NO description/summary is still embeddable and the
                    # topic name is part of the embedded vector. Using the shared helper guarantees the
                    # vector arm's seed/sync text and an embedding repo's write-time text NEVER drift. ---
                    text = card_embed_text(card)
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

                    # --- Fingerprint: hash of all stored file sha256 values AND the embedded text.
                    # Including the text means the card re-embeds when EITHER its files change OR its
                    # embedded name/description/content change (the async updater rewrites those, and
                    # the vector store must re-embed on the next sync) -- not only on file changes. ---
                    fp_parts: List[str] = []
                    for fe in card.get("files", []):
                        s = fe.get("sha256") or ""
                        if s:
                            fp_parts.append(s)
                    fp_basis = "|".join(sorted(fp_parts)) + "\x00" + text
                    fingerprint = _hl.sha256(fp_basis.encode("utf-8")).hexdigest()[:16]

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

    def _repo_text_search_candidates(
        self, task_text: str
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Candidate cards from the repository's NATIVE text search, or ``None`` to fall back.

        Returns ``None`` (so the caller scans ``_load_all()`` with in-app IDF, today's behavior)
        when the repository has no native text search OR its search returns ``None`` OR anything
        goes wrong. Returns ``{card_id: card_dict}`` when the repo served a native-search candidate
        pool for the keyword arm. Detected by duck-typing (``hasattr``), never an isinstance check,
        so any repo that exposes a ``search_cards(query, *, limit)`` method participates. Never
        raises.
        """
        search = getattr(self._repo, "search_cards", None)
        if not callable(search):
            return None
        try:
            # Ask for a generous pool: the keyword arm keeps up to ``_max_cards * 2`` IDF
            # candidates, so a higher limit lets the repo's text search supply enough rows for the
            # store's IDF ranking / confidence gate / recency to choose among.
            limit = max(self._max_cards * 4, 32)
            result = search(task_text, limit=limit)
            if result is None:
                return None
            return result if isinstance(result, dict) else None
        except Exception:  # noqa: BLE001 — native search is best-effort; fall back to in-app IDF
            _log.debug("repo search_cards failed; falling back to in-app IDF", exc_info=True)
            return None

    def _load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all cards via the repository. Returns {card_id: card_dict}.

        Uses an in-memory cache to avoid re-fetching every card on every call.
        Invalidates the cache when:
        - ``_cache_dirty`` is set (after any local write via ``record()`` or
          ``bootstrap()``), OR
        - the repository's ``revision()`` change-stamp changed (external write).
        """
        current_stamp = self._repo.revision()
        need_reload = (
            self._cache is None
            or self._cache_dirty
            or current_stamp != self._cache_dir_stamp
        )
        if not need_reload:
            return self._cache  # type: ignore[return-value]

        # Reload from the repository.
        self._cache_dirty = False
        self._cache_dir_stamp = current_stamp
        self._cache = self._repo.load_all()
        return self._cache
