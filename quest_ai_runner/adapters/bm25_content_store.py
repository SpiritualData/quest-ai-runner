"""BM25ContentStore -- ContextAssembler backed by BM25 over FILE CONTENT.

This assembler indexes the ACTUAL TEXT CONTENT of files in a corpus root (not
summaries or keywords) using BM25, enabling precise retrieval of exact
identifiers, rare tokens, and specific phrases that appear in the real code and
documents but are NOT captured by the dense vector layer (which only embeds
summaries/topics).

COMPLEMENTARY ROLE
------------------
This adapter fills the gap between two existing arms:

* ``FileContextStore`` (IDF over keyword summaries) -- route by summary/symbol
  overlap; does NOT read file content.
* ``VectorContextAssembler`` (dense embeddings) -- semantic orientation via
  summaries/topics; does NOT embed full file content.
* ``BM25ContentStore`` (BM25 over full content) -- exact-token search across
  the un-embedded, un-summarised text; the right arm for "find every file that
  contains XFCALLBACK_7Q2 or the string 'legacy_mode'".

AGENTIC + PARALLEL MULTI-QUERY
--------------------------------
When a ``ModelProvider`` is wired, ``assemble()`` generates ``num_queries``
diverse keyword/phrase queries from the task text (one cheap LLM call) and
runs a BM25 search for EACH query IN PARALLEL (ThreadPoolExecutor), then
deduplicates hits by file path, keeping the best score from any query.

AUTO-UPDATE
-----------
On every ``assemble()`` call the index is lazily built on first use, then
refreshed cheaply: only files whose sha256 changed since the last index are
re-tokenized and re-indexed (stat + hash pass).

OPTIONAL DEPENDENCY
-------------------
Requires ``bm25s`` (pure-Python BM25).  Install with::

    pip install 'quest-ai-runner[bm25]'

If ``bm25s`` is absent, the constructor raises ``ImportError`` with a clear
install hint so the rest of the package stays importable without the extra.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.adapters import AssembledContext, ContextAssemblerBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-use the same walk constants as FileContextStore so the two arms index
# the same corpus consistently.
# ---------------------------------------------------------------------------

_SKIP_DIRS: Set[str] = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    "dist", "build", ".eggs", ".mypy_cache", ".pytest_cache", ".quest-context",
}

_SOURCE_EXTS: Set[str] = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
    ".java", ".rb", ".md", ".sh", ".yaml", ".yml", ".toml", ".json",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".swift", ".kt", ".scala",
    ".html", ".css", ".scss", ".less", ".txt", ".rst",
}

# Max file size to read during indexing (512 KB -- same as FileContextStore).
_BOOTSTRAP_MAX_BYTES = 512 * 1024

# Maximum number of files to index (safety bound).
_MAX_FILES = 10_000

# Maximum number of parallel search workers for multi-query.
_MAX_SEARCH_WORKERS = 8

# Lines of context to include in a BM25 hit snippet.
_SNIPPET_LINES = 3


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

def _tokenize_content(text: str) -> List[str]:
    """Tokenize file content into lowercase word tokens for BM25.

    Splits on non-alphanumeric boundaries, lowercases, and drops empty tokens.
    Keeps short tokens (unlike the IDF stopword-filtered tokenizer) because
    in code even 2-letter identifiers matter.  Returns a list (not a set) so
    BM25 sees term frequency.
    """
    return [t for t in re.findall(r"[a-z0-9_]+", text.lower()) if t]


# ---------------------------------------------------------------------------
# Content fingerprint (sha256)
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    """Return the sha256 hex digest of a file, or '' on any error. Never raises."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Snippet extraction: find the best-matching lines in the content for a query
# ---------------------------------------------------------------------------

def _best_snippet(content: str, query_tokens: Set[str], n_lines: int = _SNIPPET_LINES) -> str:
    """Return the ``n_lines`` lines from ``content`` that contain the most query tokens.

    Falls back to the first ``n_lines`` non-empty lines when no match is found.
    Never raises.
    """
    try:
        lines = content.splitlines()
        if not lines:
            return ""

        # Score each line by how many query tokens it contains.
        best_score = -1
        best_start = 0
        for i, line in enumerate(lines):
            ll = line.lower()
            score = sum(1 for t in query_tokens if t in ll)
            if score > best_score:
                best_score = score
                best_start = i

        # Take up to n_lines starting at best_start.
        window = lines[best_start: best_start + n_lines]
        result = "\n".join(l for l in window if l.strip())
        if result:
            return result

        # Fallback: first n_lines non-empty.
        first = [l for l in lines if l.strip()][:n_lines]
        return "\n".join(first)
    except Exception:  # noqa: BLE001
        return content[:200]


# ---------------------------------------------------------------------------
# BM25ContentStore
# ---------------------------------------------------------------------------

class BM25ContentStore(ContextAssemblerBase):
    """ContextAssembler using BM25 over the ACTUAL CONTENT of files in ``root``.

    Complements the vector arm (dense embeddings over summaries) and the IDF arm
    (keyword overlap over card summaries/symbols) by searching the full,
    un-embedded text of every file -- the right tool for exact identifiers,
    rare phrases, and any token that was never summarised.

    Parameters
    ----------
    root:
        Corpus root directory.  All source files under this tree are indexed.
    index_dir:
        Optional path to store/cache the BM25 index on disk.  If ``None`` the
        index is held in memory only and rebuilt after each process restart.
    provider:
        Optional ``ModelProvider`` for LLM-based query expansion.  When wired,
        ``assemble()`` generates ``num_queries`` diverse search queries from the
        task text and runs BM25 over each IN PARALLEL.
    query_model:
        Model tier used for query-gen (cheap; defaults to "haiku").
    num_queries:
        Number of extra LLM-generated queries (in addition to the raw task text).
    top_k:
        Number of BM25 hits to retrieve per query.
    max_in_view:
        Maximum number of hits rendered into the context_view.
    confidence_threshold:
        Minimum BM25 score for a hit to be included.  Set to ``0.0`` to keep
        all positive-scoring hits (default).

    Raises
    ------
    ImportError
        If ``bm25s`` is not installed (the ``[bm25]`` optional extra).
    """

    def __init__(
        self,
        *,
        root: str,
        index_dir: Optional[str] = None,
        provider: Any = None,
        query_model: str = "haiku",
        num_queries: int = 3,
        top_k: int = 8,
        max_in_view: int = 8,
        confidence_threshold: float = 0.0,
    ) -> None:
        # Lazy-import bm25s so the package stays importable without the extra.
        try:
            import bm25s  # noqa: F401 -- test that it is available
            self._bm25s = bm25s
        except ImportError as exc:
            raise ImportError(
                "BM25ContentStore requires the 'bm25s' package. "
                "Install it with: pip install 'quest-ai-runner[bm25]'"
            ) from exc

        self._root = Path(root).resolve()
        self._index_dir = Path(index_dir).resolve() if index_dir else None
        self._provider = provider
        self._query_model = query_model
        self._num_queries = num_queries
        self._top_k = top_k
        self._max_in_view = max_in_view
        self._confidence_threshold = confidence_threshold

        # Internal index state.  None until first build.
        self._bm25_index: Any = None  # bm25s.BM25 instance
        self._file_paths: List[str] = []          # parallel list: path strings (relative to root)
        self._file_contents: List[str] = []       # parallel list: raw file contents
        self._fingerprints: Dict[str, str] = {}   # rel_path -> sha256

    # ------------------------------------------------------------------
    # ContextAssemblerBase implementation
    # ------------------------------------------------------------------

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None
    ) -> AssembledContext:
        """Search file content with BM25 for ``task_text``.  Never raises."""
        try:
            return self._assemble_inner(task_text)
        except Exception:  # noqa: BLE001
            logger.debug("BM25ContentStore.assemble failed", exc_info=True)
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        """Best-effort no-op.  The content index is derived from files, not runs."""
        # The file index auto-updates from file changes; no run-outcome state to persist.
        pass

    # ------------------------------------------------------------------
    # Index management (lazy build + auto-update)
    # ------------------------------------------------------------------

    def _build_index(self) -> None:
        """Walk ``root``, read source files, tokenize, build a fresh BM25 index.

        Populates ``_file_paths``, ``_file_contents``, ``_fingerprints``, and
        ``_bm25_index``.  May raise; callers wrap in try/except.
        """
        paths: List[str] = []
        contents: List[str] = []
        fingerprints: Dict[str, str] = {}

        for dirpath, dirnames, filenames in os.walk(self._root):
            if len(paths) >= _MAX_FILES:
                break
            current_dir = Path(dirpath).resolve()
            # Prune skip dirs in-place.
            dirnames[:] = [
                d for d in dirnames
                if d not in _SKIP_DIRS
            ]
            for fname in filenames:
                if len(paths) >= _MAX_FILES:
                    break
                fpath = Path(dirpath) / fname
                if fpath.suffix not in _SOURCE_EXTS:
                    continue
                try:
                    if fpath.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                        continue
                except OSError:
                    continue
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    continue
                rel = str(fpath.relative_to(self._root))
                sha = _sha256_file(fpath)
                paths.append(rel)
                contents.append(content)
                fingerprints[rel] = sha

        if not paths:
            # Nothing to index -- leave index as None.
            self._file_paths = []
            self._file_contents = []
            self._fingerprints = {}
            self._bm25_index = None
            return

        # Tokenize all files.
        tokenized = [_tokenize_content(c) for c in contents]

        # Build bm25s index.
        retriever = self._bm25s.BM25()
        corpus_tokens = self._bm25s.tokenize(
            [" ".join(toks) for toks in tokenized],
            stopwords=None,
            stemmer=None,
        )
        retriever.index(corpus_tokens)

        self._file_paths = paths
        self._file_contents = contents
        self._fingerprints = fingerprints
        self._bm25_index = retriever

    def _auto_update(self) -> None:
        """Re-index files whose sha256 changed since the last build.

        This is a cheap stat + hash pass: unchanged files are skipped entirely.
        Files that changed are re-read and their token lists replaced.  The BM25
        index is then rebuilt over all (updated) token lists.

        If the index does not yet exist, delegates to ``_build_index()``.
        """
        if self._bm25_index is None or not self._file_paths:
            self._build_index()
            return

        changed = False
        new_paths: List[str] = []
        new_contents: List[str] = []
        new_fps: Dict[str, str] = {}

        # Walk root to collect the current file set.
        current_files: Dict[str, Path] = {}
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                if fpath.suffix not in _SOURCE_EXTS:
                    continue
                try:
                    if fpath.stat().st_size > _BOOTSTRAP_MAX_BYTES:
                        continue
                except OSError:
                    continue
                try:
                    rel = str(fpath.relative_to(self._root))
                except ValueError:
                    continue
                current_files[rel] = fpath

        # Build the updated path/content/fingerprint lists.
        # Preserve existing files first (reuse content when unchanged).
        existing_by_rel = dict(zip(self._file_paths, self._file_contents))

        for rel, fpath in sorted(current_files.items()):
            sha = _sha256_file(fpath)
            old_sha = self._fingerprints.get(rel, "")
            if sha and old_sha == sha and rel in existing_by_rel:
                # Unchanged: reuse cached content.
                new_paths.append(rel)
                new_contents.append(existing_by_rel[rel])
            else:
                # New or changed: re-read.
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    content = ""
                new_paths.append(rel)
                new_contents.append(content)
                changed = True
            new_fps[rel] = sha

        # Check for removed files.
        old_set = set(self._file_paths)
        new_set = set(new_paths)
        if old_set != new_set:
            changed = True

        if not changed:
            return

        # Rebuild index with updated content.
        if not new_paths:
            self._file_paths = []
            self._file_contents = []
            self._fingerprints = {}
            self._bm25_index = None
            return

        tokenized = [_tokenize_content(c) for c in new_contents]
        retriever = self._bm25s.BM25()
        corpus_tokens = self._bm25s.tokenize(
            [" ".join(toks) for toks in tokenized],
            stopwords=None,
            stemmer=None,
        )
        retriever.index(corpus_tokens)

        self._file_paths = new_paths
        self._file_contents = new_contents
        self._fingerprints = new_fps
        self._bm25_index = retriever

    # ------------------------------------------------------------------
    # Query generation (agentic, with provider)
    # ------------------------------------------------------------------

    def _generate_queries(self, task_text: str) -> List[str]:
        """Ask the LLM for ``num_queries`` keyword/phrase queries.  Never raises."""
        if self._provider is None or self._num_queries <= 0:
            return []
        try:
            prompt = (
                f"Generate {self._num_queries} short keyword or phrase queries "
                f"suitable for a BM25 full-text search over source files. "
                f"Each query should be different and help find files relevant to the task. "
                f"Output one query per line, no numbering, no extra text.\n\n"
                f"Task: {task_text}"
            )
            raw = self._provider.answer(
                [{"role": "user", "content": prompt}],
                model=self._query_model,
            )
            queries = [
                line.strip()
                for line in raw.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            return queries[: self._num_queries]
        except Exception:  # noqa: BLE001
            logger.debug("BM25ContentStore._generate_queries failed", exc_info=True)
            return []

    # ------------------------------------------------------------------
    # BM25 search (single query)
    # ------------------------------------------------------------------

    def _search_one(self, query: str) -> List[Tuple[str, float]]:
        """Run BM25 for a single query.  Returns [(rel_path, score), ...].  Never raises."""
        try:
            if self._bm25_index is None or not self._file_paths:
                return []

            query_tokens = self._bm25s.tokenize(
                [query],
                stopwords=None,
                stemmer=None,
            )
            results, scores = self._bm25_index.retrieve(
                query_tokens,
                corpus=self._file_paths,
                k=min(self._top_k, len(self._file_paths)),
            )
            # results shape: (n_queries, k), scores shape: (n_queries, k)
            hits: List[Tuple[str, float]] = []
            for path, score in zip(results[0], scores[0]):
                if isinstance(score, float) and score > 0:
                    hits.append((str(path), float(score)))
            return hits
        except Exception:  # noqa: BLE001
            logger.debug("BM25ContentStore._search_one failed for %r", query, exc_info=True)
            return []

    # ------------------------------------------------------------------
    # Internal assemble
    # ------------------------------------------------------------------

    def _assemble_inner(self, task_text: str) -> AssembledContext:
        # Ensure index is up to date (lazy build + auto-update).
        self._auto_update()

        if self._bm25_index is None:
            return AssembledContext()

        # Build query set: raw task text + LLM-generated queries.
        extra_queries = self._generate_queries(task_text)
        all_queries = [task_text] + extra_queries

        # Run BM25 for each query IN PARALLEL; dedupe by path, keep best score.
        best: Dict[str, float] = {}

        n_workers = min(_MAX_SEARCH_WORKERS, len(all_queries))
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {pool.submit(self._search_one, q): q for q in all_queries}
                for fut in concurrent.futures.as_completed(futures):
                    try:
                        for path, score in fut.result():
                            if path not in best or score > best[path]:
                                best[path] = score
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            # Fallback to serial.
            for q in all_queries:
                for path, score in self._search_one(q):
                    if path not in best or score > best[path]:
                        best[path] = score

        # Confidence gate.
        kept = [
            (path, score) for path, score in best.items()
            if score >= self._confidence_threshold
        ]
        if not kept:
            return AssembledContext()

        # Sort by score descending, cap at max_in_view.
        kept.sort(key=lambda x: x[1], reverse=True)
        kept = kept[: self._max_in_view]

        # Render context_view: path + best-matching snippet + score.
        query_tokens: Set[str] = set(_tokenize_content(task_text))
        for q in extra_queries:
            query_tokens.update(_tokenize_content(q))

        path_to_content: Dict[str, str] = dict(
            zip(self._file_paths, self._file_contents)
        )

        view_parts: List[str] = []
        card_ids: List[str] = []
        for rel_path, score in kept:
            card_ids.append(rel_path)
            content = path_to_content.get(rel_path, "")
            snippet = _best_snippet(content, query_tokens) if content else ""
            part_lines = [f"### BM25 hit: {rel_path}  (score={score:.3f})"]
            if snippet:
                part_lines.append(f"  snippet: {snippet[:300]}")
            view_parts.append("\n".join(part_lines))

        context_view = "\n\n---\n\n".join(view_parts)
        return AssembledContext(
            context_view=context_view,
            card_ids=card_ids,
            stale=[],
        )
