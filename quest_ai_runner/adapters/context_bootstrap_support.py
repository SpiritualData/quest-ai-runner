"""Bootstrap bookkeeping: parallel runs, the bootstrap and discovery files, worker and file-count limits, JSON and file sampling helpers.

Split out of ``file_context_store.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from typing import Any, Dict, List, Optional, Set
from pathlib import Path
from datetime import datetime, timezone

from .card_store_identity import identity_of
from ..core.file_modes import match_umask
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


# {rel_path: mtime} of files a topic-discovery pass has already been shown. Discovery samples
# representative files per folder, so most files it sees never end up pinned by any card; without
# this record they counted as "new" forever and every refresh re-ran paid LLM discovery on them
# (20,376 files on one corpus, at every chat start). A file drops out of this record, and becomes
# new again, as soon as its mtime changes.
_DISCOVERED_FILE = "index-state/discovered_files.json"


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
    root: Optional[str] = None,
) -> None:
    """Write ``bootstrap_meta.json`` atomically (temp file + replace). Never raises.

    The store's IDENTITY keys (``store_id``, recorded ``root``, ``access``, ``mounts``, ...) are
    carried through from the previous file untouched; a store with no id is given one here
    (``card_store_identity.identity_of``). ``root`` is recorded only when none is recorded yet: a
    bootstrap never moves a store's root, ``context move-root`` does.

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
        payload.update(identity_of(_read_bootstrap_meta(cards_dir), root=root))
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(cards_dir), prefix=".tmp_meta_", suffix=".json")
        match_umask(tmp_fd)  # mkstemp makes 0600; another account sharing the corpus must read it
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
#
# WHY THIS IS NOT A PLAIN CONSTANT. Each worker holds one ModelProvider call open, and for a
# subprocess-backed provider (the keyless `claude` CLI) that is one CHILD PROCESS with its own
# heap, not one thread. Eight workers is therefore eight concurrent CLI processes, which makes
# this a MEMORY figure rather than a concurrency one. On a box already carrying other work it is
# what takes a runner from comfortable to OOM-killed part-way through indexing -- and a poll
# runner killed here leaves the task it had claimed marked in-progress with nobody running it,
# so the work is not merely slow, it is silently lost until something reaps the row.
#
# So this reads the SAME budget the orchestrator's own fan-out reads (``QAR_MAX_PARALLEL``)
# before falling back to 8: a deployment that has already said "this box fits N concurrent model
# calls" has said it about this fan-out too, and having one stage quietly ignore that is how the
# limit gets set, believed, and then exceeded anyway. ``QAR_BOOTSTRAP_WORKERS`` overrides just
# this stage, for a deployment that genuinely wants the two to differ.
_BOOTSTRAP_WORKERS_DEFAULT = 8


def _bootstrap_workers() -> int:
    """How many bootstrap chunks may hold a model call open at once (always >= 1)."""
    for var in ("QAR_BOOTSTRAP_WORKERS", "QAR_MAX_PARALLEL"):
        raw = os.getenv(var, "").strip()
        if not raw:
            continue
        try:
            return max(1, int(raw))
        except ValueError:
            _log.warning("context index: %s=%r is not an integer — ignoring it", var, raw)
    return _BOOTSTRAP_WORKERS_DEFAULT


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


# Whether a bootstrap/refresh removes cards the corpus no longer indexes. Default ON; set
# ``QAR_PRUNE_DEAD_CARDS=0`` for a deployment that would rather keep a stale index than lose a
# card. See ``_file_entry_is_dead`` for what "no longer indexes" means, precisely.
# Optional ceilings on ONE bootstrap pass. Both default to NO LIMIT.
#
# They used to be hardcoded at 10,000 files and 5,000 cards, applied SILENTLY: a corpus of 81,455
# indexable files was walked until the 10,000th and then simply stopped, and the run reported
# success. Nothing in the output distinguished "indexed your corpus" from "indexed the first
# eighth of it", so the store looked complete and the missing files were never revisited. A limit
# nobody is told about is indistinguishable from a bug, and this one hid behind a healthy-looking
# summary for as long as the store existed.
#
# So: no ceiling unless a deployment asks for one, and when one IS set and actually bites, it says
# so at WARNING with the number it stopped at.
def _bootstrap_max_files() -> Optional[int]:
    return _optional_positive_int_env("QAR_BOOTSTRAP_MAX_FILES")


def _bootstrap_max_cards() -> Optional[int]:
    return _optional_positive_int_env("QAR_BOOTSTRAP_MAX_CARDS")


def _optional_positive_int_env(name: str) -> Optional[int]:
    """A positive int from the environment, or None for "no limit". Never raises."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        _log.warning("context index: %s=%r is not an integer — ignoring it (no limit)", name, raw)
        return None
    return value if value > 0 else None


# Max file size to fingerprint/parse during bootstrap (512 KB).
_BOOTSTRAP_MAX_BYTES = 512 * 1024


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


def _jaccard(a: Set[str], b: Set[str]) -> float:
    """File-set Jaccard similarity. Returns 0.0 when both sets are empty."""
    if not a and not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union)


# Two cards are dedup CANDIDATES when they share at least this many keywords.
# Ceiling on a merged card's keyword list. The prompt asks for 5-12 per card; merging unions
# them, and repeated merges compound without this.
_MAX_MERGED_KEYWORDS = 24


def _card_file_paths(card: Dict[str, Any]) -> List[str]:
    """A card's file entries as plain path strings, whichever shape they are stored in.

    Two shapes exist and they MEET here. A freshly identified card carries plain paths (that is
    what the model returns); a card loaded from disk carries dicts (``{"path", "sha256", ...}``),
    because that is what gets persisted. Dedup combines new cards with existing ones -- that is the
    whole point of it -- so any merge across that boundary produces a list holding both, and the
    first place downstream that puts a file entry in a ``set`` raises ``TypeError: unhashable type:
    'dict'``. That killed two consecutive full bootstraps after all their model work was done and
    reported "Cards created: 0"; it only bites on a RE-bootstrap, because a first run has no
    existing cards to merge with.
    """
    out: List[str] = []
    for entry in card.get("files", []) or []:
        path = entry.get("path", "") if isinstance(entry, dict) else entry
        if path:
            out.append(str(path))
    return out


def _merge_card_group(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a group of cards into one. The first card keeps its id/name/summary; keywords and
    files are unioned across the group (order-preserving, and normalised to path strings)."""
    rep = {**group[0]}
    keywords = list(rep.get("keywords", []))
    files = _card_file_paths(rep)
    for card in group[1:]:
        for k in card.get("keywords", []):
            if k not in keywords:
                keywords.append(k)
        for f in _card_file_paths(card):
            if f not in files:
                files.append(f)
    # A merged card's keyword list was unioned without any bound, and merges compound: one real
    # store held a card with 2,174 keywords where the prompt asks for 5 to 12. That is not a
    # richer card, it is a card that matches almost every query, and it crowds out the cards that
    # genuinely answer one. The earliest keywords come from the representative card of the group,
    # so truncating keeps the most on-topic ones and drops the accumulated tail. FILES are never
    # truncated: a card must keep pointing at everything it covers.
    rep["keywords"] = keywords[:_MAX_MERGED_KEYWORDS]
    rep["files"] = files
    return rep


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
