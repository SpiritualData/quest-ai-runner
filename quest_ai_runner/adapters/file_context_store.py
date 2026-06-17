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
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.adapters import AssembledContext, ContextAssemblerBase
from ._walk import effective_skip_dirs, prune_dirnames
from .tfidf_sampling import extract_terms as tfidf_extract_terms, select_representatives

_log = logging.getLogger("quest-ai-runner.context")

# Bootstrap ALGORITHM version. Bump this when the bootstrap/dedup logic changes in a way that
# makes previously-written cards stale (a re-index is warranted). ``config._bootstrap_if_needed``
# compares this against the stored ``bootstrap_meta.json`` version and re-bootstraps when the
# stored version is older. v2: LLM-based keyword-cluster dedup (replaced Jaccard file-overlap).
# v3: TF-DF-IDF sampling in Stage 1 & 2 (representative files + snippets instead of all paths).
_BOOTSTRAP_VERSION = 3

# Name of the meta file written to cards_dir after a successful bootstrap.
_BOOTSTRAP_META_FILE = "bootstrap_meta.json"


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


def _write_bootstrap_meta(cards_dir: str, count: int) -> None:
    """Write ``bootstrap_meta.json`` atomically (temp file + replace). Never raises.

    Records the algorithm ``version`` (so a future runner can detect a stale index), the
    ``card_count`` just written, and a UTC ``completed_at`` timestamp.
    """
    try:
        Path(cards_dir).mkdir(parents=True, exist_ok=True)
        payload = {
            "version": _BOOTSTRAP_VERSION,
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

    Delegates to the shared select_representatives() function from tfidf_sampling module,
    grouping by folder and scoring by term distinctiveness.
    """
    return select_representatives(
        file_paths,
        get_terms=tfidf_extract_terms,
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
        dry_run: bool = False,
    ) -> None:
        self._cards_dir = Path(cards_dir)
        self._repo_root = Path(repo_root).resolve() if repo_root else None
        self._max_cards = max_cards_in_view
        self._auto_bootstrap = auto_bootstrap
        self._dry_run = dry_run
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

        Without a ``provider`` this is a NO-OP returning 0: topic cards require semantic
        understanding, so cards accumulate via ``record()`` instead.

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
            # Record the algorithm version so a future run can detect a stale index.
            _write_bootstrap_meta(str(self._cards_dir), self._count_cards_on_disk())
        return n

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
            and e.name != _BOOTSTRAP_META_FILE
            for e in self._cards_dir.iterdir()
        ):
            return
        try:
            self._bootstrap_inner(root=str(root))
        except Exception:  # noqa: BLE001
            pass

    def refresh_stale(self, root: Optional[str] = None, *, provider=None) -> int:
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
            return self._bootstrap_inner(root=root, provider=provider, skip_unchanged=True)
        except Exception:  # noqa: BLE001
            return 0

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
        """
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
            if file_count >= max_files:
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

        if not file_paths:
            return 0

        # Topic cards require semantic understanding. Without a provider, do nothing.
        if provider is None:
            _log.warning(
                "context index: bootstrap skipped — no model provider wired, so no semantic "
                "topic cards can be identified (cards accumulate via record() instead)"
            )
            return 0

        # --- Incremental diff: what is uncovered (in no card) vs stale (covered but changed) ---
        existing = self._load_all()
        existing_cards = list(existing.values())
        covered: Set[str] = set()
        for card in existing_cards:
            for fe in card.get("files", []):
                p = fe.get("path", "") if isinstance(fe, dict) else fe
                if p:
                    covered.add(p)

        uncovered = [p for p in file_paths if p not in covered]
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

        # On the very first bootstrap (no existing cards) everything is "uncovered".
        if not existing_cards:
            uncovered = list(file_paths)

        if not uncovered and not stale_covered:
            _log.info("context index: all files covered and up to date")
            return 0

        _log.info(
            "context index: %d new file(s) found (not in any existing card), %d stale file(s) "
            "— processing", len(uncovered), len(stale_covered),
        )

        # --- LLM: identify topic cards for the NEW (uncovered) files, deduping vs existing ---
        topic_cards: List[Dict[str, Any]] = []
        if uncovered:
            _log.info("context index: stage 2 — analyzing %d new files for topics", len(uncovered))
            topic_cards = _llm_topic_cards(
                uncovered, provider, model=model, existing_cards=existing_cards, walk_root=walk_root
            )
            _log.info("context index: identified %d topic card(s) from new files", len(topic_cards))

        # --- Stale-covered: regenerate the cards that reference any stale file ---
        # Identify the cards touching a stale file and re-run topic extraction over each card's
        # file set so its summary/keywords/files reflect the current code, keeping the card id.
        if stale_covered:
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

        if not topic_cards:
            return 0

        self._cards_dir.mkdir(parents=True, exist_ok=True)

        # --- Pass 2: fingerprint every file referenced by any topic card, in parallel ---
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
                })

            # Load existing card so we preserve usage_count / last_outcome if present.
            card_path = self._cards_dir / f"{card_id}.json"
            existing: Dict[str, Any] = {}
            if card_path.exists():
                try:
                    with open(card_path, "r", encoding="utf-8") as fh:
                        existing = json.load(fh)
                except Exception:  # noqa: BLE001
                    existing = {}

            # Incremental refresh: skip a topic whose every file's sha256 is unchanged.
            if skip_unchanged and existing:
                old_shas = {
                    fe.get("path", ""): fe.get("sha256", "")
                    for fe in existing.get("files", [])
                }
                unchanged = bool(file_dicts) and all(
                    fd["sha256"]
                    and old_shas.get(fd["path"]) == fd["sha256"]
                    for fd in file_dicts
                ) and len(old_shas) == len(file_dicts)
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
                },
                "usage_count": existing.get("usage_count", 0),
                "last_outcome": existing.get("last_outcome", "unknown"),
            }

            # Atomic write (skip if dry-run mode).
            if not self._dry_run:
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
            else:
                # In dry-run mode, count the card but don't write it.
                cards_written += 1

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

        # --- Card metadata: populate selection info for UI display and transparency ---------
        # Build metadata for each selected card so the orchestrator can emit which cards were chosen.
        card_metadata: List[Dict[str, Any]] = []
        for card in top_cards:
            files_list = [fe.get("path", "") for fe in card.get("files", [])[:3]]
            card_metadata.append({
                "id": card.get("id", ""),
                "title": card.get("summary", ""),
                "relevance_score": 0.75,  # keyword match is fairly high confidence
                "file_count": len(card.get("files", [])),
                "files": files_list,
                "adapter": "keyword",
            })

        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=list(dict.fromkeys(stale_list)),  # deduplicate, preserve order
            sources=_sources,
            card_metadata=card_metadata,
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

        # Atomic write via tmp + os.replace (skip if dry-run mode).
        if not self._dry_run:
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

    def _count_cards_on_disk(self) -> int:
        """Count card JSON files on disk (excludes the meta sidecar). Never raises."""
        try:
            if not self._cards_dir.exists():
                return 0
            return sum(
                1 for e in self._cards_dir.iterdir()
                if e.suffix == ".json" and not e.name.startswith(".")
                and e.name != _BOOTSTRAP_META_FILE
            )
        except Exception:  # noqa: BLE001
            return 0

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
                if (entry.suffix == ".json" and not entry.name.startswith(".")
                        and entry.name != _BOOTSTRAP_META_FILE):
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
            if (entry.suffix != ".json" or entry.name.startswith(".")
                    or entry.name == _BOOTSTRAP_META_FILE):
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
