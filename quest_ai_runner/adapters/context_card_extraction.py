"""Reading files into cards: slugs, symbols, docstrings, summaries, area and topic extraction.

Split out of ``file_context_store.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from pathlib import Path

from hubofhubs.recall.card_content_render import (
    MAX_CARD_CONTENT_ITEMS as _MAX_CARD_CONTENT_ITEMS,
    normalize_content as _normalize_content,
    tokenize as _tokenize,
)
from .context_bootstrap_support import (
    _BOOTSTRAP_MAX_BYTES,
    _extract_json_array,
    _log,
    _select_representative_files,
)


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


def _read_head(file_path: Path, max_bytes: int) -> str:
    """Read at most ``max_bytes`` from the FRONT of a file. Never raises; "" on any error.

    For extractors that only ever look at the top of a file (a heading, a leading comment block).
    ``Path.read_text()`` is the wrong tool for those no matter how the result is sliced afterwards:
    it materialises the whole file as one str, and a following ``.splitlines()`` materialises it a
    SECOND time as a list of str objects, each with its own ~49 bytes of object overhead.

    INCIDENT (2026-09-12). A corpus root contained data dumps a walker has no reason to exclude by
    name -- ``.json`` files of 762MB, 581MB, 486MB. Carding them called ``_extract_leading_comment``,
    which read each one whole and split it, **to inspect ``lines[:20]``**. A single 762MB file cost
    well over a gigabyte of resident memory; a handful of them took the runner to 7.6GB anon-rss and
    the kernel OOM-killed it mid-run. Because the runner is killed rather than raising, the task it
    had claimed stayed marked in-progress with nobody running it, and was only reaped as "failed"
    two hours later -- and failed rows are never mailed, so the day's work vanished with no error
    anywhere. Reading a bounded prefix is what makes the cost independent of file size.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(max_bytes)
    except Exception:  # noqa: BLE001
        return ""


def _read_whole_if_small(file_path: Path) -> str:
    """The whole file, but only when it is small enough to card. Never raises; "" on any error.

    For the AST extractors, which genuinely need a complete, parseable source text and cannot work
    from a prefix (a truncated module is a SyntaxError). The size gate is ``_BOOTSTRAP_MAX_BYTES``
    -- the SAME limit ``_extract_file_snippet`` already applies -- so "too big to card" means one
    thing across this module instead of depending on which extractor happened to be called.
    """
    try:
        if file_path.stat().st_size > _BOOTSTRAP_MAX_BYTES:
            return ""
        return file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _extract_symbols(file_path: Path, max_symbols: int = 30) -> List[str]:
    """Extract top-level symbol names from a source file. Never raises.

    For ``.py`` files uses the stdlib ``ast`` module to collect top-level and
    class-level ``FunctionDef``, ``AsyncFunctionDef``, and ``ClassDef`` names.
    For other languages applies a lightweight regex set.  Returns at most
    ``max_symbols`` names (first encountered wins).
    """
    symbols: List[str] = []
    try:
        text = _read_whole_if_small(file_path)
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
        text = _read_whole_if_small(file_path)
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
        # Heading + first paragraph live at the top; 16KB is far more than enough.
        lines = _read_head(file_path, 16 * 1024).splitlines()
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
        # Only lines[:20] are ever inspected below, so read a prefix, not the file.
        lines = _read_head(file_path, 8 * 1024).splitlines()
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


# Prompt for stage 1: given a chunk of file paths, identify top-level areas.
_AREA_PROMPT = """You are analyzing a slice of a codebase to identify its top-level areas.

Source files (one path per line):

{file_tree}

Group these files into 2 to 8 high-level areas, modules, or subsystems. An area is a cohesive
cluster of files that work together. The number of areas should reflect what is really there.

For each area produce:
  - name: a short label (2-5 words)
  - description: one sentence describing what this area does
  - files: the NUMBERS of the paths that belong here, from the numbered list above

Respond with ONLY a JSON array, no prose, no markdown fences:
[{{"name": "...", "description": "...", "files": [1, 4, 7]}}]
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
  - files: the NUMBERS of the paths that belong here, from the numbered list above

A file may appear in multiple cards.

Respond with ONLY a JSON array, no prose, no markdown fences:
[{{"id": "...", "name": "...", "keywords": ["..."], "summary": "...", "files": [1, 4]}}]
"""


def _numbered_tree(paths: List[str]) -> str:
    """The file list as ``N. path`` lines, so a card can name files by NUMBER instead of copying
    the path back.

    WHY, measured: asking the model to echo each path verbatim spends generated tokens re-emitting
    strings the caller already holds. Stage 1 alone was ~32,550 output tokens of nothing but paths
    (14 chunks x 150 paths), and stage 2 pays it again for every card it proposes -- including the
    52% of cards that dedup then discards. An index costs two or three tokens where a path costs
    fifteen, and the grouping decision is identical, so this is pure waste removed rather than
    quality traded away.
    """
    return "\n".join(f"{i + 1}. {p}" for i, p in enumerate(paths))


def _resolve_file_refs(refs: Any, ordered: List[str], allowed: Set[str]) -> List[str]:
    """Turn a card's ``files`` value into real paths.

    Accepts NUMBERS (the current contract, 1-based into ``ordered``) and also literal path strings,
    because a model will occasionally answer with the path anyway and there is no reason to throw
    away an otherwise good card over the form of one field. Unknown numbers and paths outside
    ``allowed`` are dropped rather than invented.
    """
    out: List[str] = []
    for ref in refs if isinstance(refs, (list, tuple)) else []:
        path = ""
        if isinstance(ref, bool):
            continue
        if isinstance(ref, int):
            if 1 <= ref <= len(ordered):
                path = ordered[ref - 1]
        elif isinstance(ref, str):
            token = ref.strip()
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(ordered):
                    path = ordered[idx - 1]
            elif not allowed or token in allowed:
                path = token
        if path and path not in out:
            out.append(path)
    return out


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
        ordered = list(chunk)
        prompt = _AREA_PROMPT.format(file_tree=_numbered_tree(ordered))
        raw = provider.answer([{"role": "user", "content": prompt}], model=model)
        allowed = set(chunk)
        areas = []
        for entry in _parse_raw_entries(raw, allowed):
            name = entry.get("name", "").strip()
            desc = entry.get("description", "").strip()
            files = _resolve_file_refs(entry.get("files"), ordered, allowed)
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
        ordered: List[str] = []
        for fpath in sampled_files:
            ordered.append(fpath)
            snippet = _extract_file_snippet(fpath, walk_root=walk_root)
            if snippet:
                summarized = _summarize_snippet(fpath, snippet)
                file_entries.append(f"{len(ordered)}. {summarized}")
            else:
                # Fallback: just the path if snippet extraction failed
                file_entries.append(f"{len(ordered)}. {fpath}")

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
            kept = _resolve_file_refs(files, ordered, allowed)
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
