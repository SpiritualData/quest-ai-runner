"""eval_deterministic.py — Zero-LLM, zero-API-key evaluation of FileContextStore.

Measures:
  1. Cold-start bootstrap: number of cards, time, files pinned, symbols indexed.
  2. Routing accuracy (top-1 / top-3): context service (IDF) vs blind-grep baseline.
  3. Staleness detection: precision / recall after mutating 3 files in the copy.

Everything runs on a *copy* of the repo in a temp dir — the live tree is never touched.
No LLM calls, no network, no API key required.

Run:
    python evaluation/eval_deterministic.py
    # or from repo root:
    .venv/bin/python evaluation/eval_deterministic.py
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Locate the repo root so we can add the package to sys.path if needed.
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent


def _ensure_package_importable() -> None:
    """Add the repo root to sys.path if quest_ai_runner is not yet importable."""
    try:
        import quest_ai_runner  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(_REPO_ROOT))


_ensure_package_importable()

from quest_ai_runner.adapters.file_context_store import FileContextStore, _tokenize  # noqa: E402

# ---------------------------------------------------------------------------
# Directories / extensions to skip when copying the repo.
# These mirror the store's own _SKIP_DIRS so the copy is what the store sees.
# ---------------------------------------------------------------------------

_COPY_SKIP_DIRS: Set[str] = {
    ".git", ".venv", "venv", "__pycache__",
    "node_modules", ".quest-context", ".eggs",
    ".mypy_cache", ".pytest_cache", "dist", "build",
    "quest_ai_runner.egg-info",
}

# ---------------------------------------------------------------------------
# The 15-item routing dataset.
# Each entry: (natural-language task, target repo-relative file).
# Authored from the *real* files in this repo.
# ---------------------------------------------------------------------------

ROUTING_TASKS: List[Tuple[str, str]] = [
    # orchestrator.py — the plan-gather-replan loop
    (
        "The planner loop runs plan then gathers reads then replans — where is that loop?",
        "quest_ai_runner/core/orchestrator.py",
    ),
    # adapters.py — the four Protocol interfaces
    (
        "Where are the RetrievalAdapter and ModelProvider Protocol interfaces defined?",
        "quest_ai_runner/core/adapters.py",
    ),
    # model_registry.py — tier bucketing
    (
        "How does the model registry bucket model ids into haiku sonnet opus tiers?",
        "quest_ai_runner/core/model_registry.py",
    ),
    # goal_runner.py — SubprocessGoalRunner
    (
        "SubprocessGoalRunner spawns a bounded goal-driven run — which file implements it?",
        "quest_ai_runner/core/goal_runner.py",
    ),
    # attachments.py — prepare_attachments
    (
        "prepare_attachments handles uploaded files and images in the context — where?",
        "quest_ai_runner/core/attachments.py",
    ),
    # files_adapter.py — FilesAdapter read_section/grep
    (
        "FilesAdapter greps over a configured root and reads sections — which file?",
        "quest_ai_runner/adapters/files_adapter.py",
    ),
    # file_context_store.py — the store under evaluation
    (
        "FileContextStore bootstraps one card per source file with IDF keyword scoring",
        "quest_ai_runner/adapters/file_context_store.py",
    ),
    # anthropic_provider.py — Anthropic SDK wrapper
    (
        "AnthropicProvider wraps the Anthropic SDK for plan and answer calls",
        "quest_ai_runner/adapters/anthropic_provider.py",
    ),
    # claude_cli_provider.py — CLI-based provider
    (
        "The claude CLI headless provider calls the claude command as a subprocess",
        "quest_ai_runner/adapters/claude_cli_provider.py",
    ),
    # poller.py — the poll loop
    (
        "The poller discovers due tasks and claims them with bounded concurrency",
        "quest_ai_runner/runner/poller.py",
    ),
    # executor.py — run one task through the brain
    (
        "The executor runs one claimed assistant task through the brain and reports result",
        "quest_ai_runner/runner/executor.py",
    ),
    # quest_client.py — Quest API client
    (
        "QuestClient sends PATCH requests to mark tasks done or needs_you in the Quest API",
        "quest_ai_runner/runner/quest_client.py",
    ),
    # config.py — RunnerConfig
    (
        "RunnerConfig is the single place a consumer supplies the Quest API key and adapters",
        "quest_ai_runner/config.py",
    ),
    # cli.py — console entry
    (
        "The quest-ai-runner console entry parses --once and --check flags from the command line",
        "quest_ai_runner/cli.py",
    ),
    # resources.py — bundled resource files
    (
        "Where are bundled resource files like prompt templates loaded from package data?",
        "quest_ai_runner/resources.py",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _copy_repo(src: Path) -> Path:
    """Copy src to a fresh temp dir, skipping dirs in _COPY_SKIP_DIRS.

    Returns the path to the copy root.
    """
    dest = Path(tempfile.mkdtemp(prefix="qar_eval_"))

    def _ignore(directory: str, names: List[str]) -> Set[str]:
        return {n for n in names if n in _COPY_SKIP_DIRS}

    shutil.copytree(src, dest / "repo", ignore=_ignore)
    return dest / "repo"


def _count_symbols(store: FileContextStore) -> int:
    """Count total symbols across all cards in the store."""
    total = 0
    for card in store._load_all().values():
        for fe in card.get("files", []):
            total += len(fe.get("symbols", []))
    return total


def _count_files_pinned(store: FileContextStore) -> int:
    """Count total file entries across all cards."""
    total = 0
    for card in store._load_all().values():
        total += len(card.get("files", []))
    return total


# ---------------------------------------------------------------------------
# Routing: context service (IDF assemble) vs blind-grep baseline
# ---------------------------------------------------------------------------

def _service_ranking(store: FileContextStore, task: str, top_k: int = 3) -> List[str]:
    """Return up to top_k file paths predicted by the context service for a task."""
    ctx = store.assemble(task)
    # Each card in the view covers exactly one file (bootstrap creates one card per file).
    # card_ids are in relevance order; resolve each to its pinned file path.
    all_cards = store._load_all()
    paths: List[str] = []
    for cid in ctx.card_ids[:top_k * 2]:  # over-fetch, then trim to top_k
        card = all_cards.get(cid, {})
        for fe in card.get("files", []):
            p = fe.get("path", "")
            if p and p not in paths:
                paths.append(p)
        if len(paths) >= top_k:
            break
    return paths[:top_k]


def _grep_ranking(repo_root: Path, task: str, top_k: int = 3) -> List[str]:
    """Blind-grep baseline: rank .py files by count of task query terms they contain.

    Tokenizes the task text the same way the store does, then counts how many
    tokens appear (case-insensitively) in each source file. Higher is better.
    """
    tokens = _tokenize(task)
    if not tokens:
        return []

    scores: Dict[str, int] = {}
    for dirpath, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = [d for d in dirnames if d not in _COPY_SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = Path(dirpath) / fname
            rel = str(fpath.relative_to(repo_root))
            try:
                text = fpath.read_text(encoding="utf-8", errors="replace").lower()
                score = sum(1 for t in tokens if t in text)
                if score > 0:
                    scores[rel] = score
            except OSError:
                pass

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    return [r for r, _ in ranked[:top_k]]


def _routing_accuracy(
    store: FileContextStore,
    repo_root: Path,
    tasks: List[Tuple[str, str]],
) -> Dict[str, float]:
    """Return top-1 and top-3 accuracy for both service and grep baseline."""
    svc_top1 = svc_top3 = grep_top1 = grep_top3 = 0

    for task, target in tasks:
        svc3 = _service_ranking(store, task, top_k=3)
        grep3 = _grep_ranking(repo_root, task, top_k=3)

        if svc3 and svc3[0] == target:
            svc_top1 += 1
        if target in svc3:
            svc_top3 += 1
        if grep3 and grep3[0] == target:
            grep_top1 += 1
        if target in grep3:
            grep_top3 += 1

    n = len(tasks)
    return {
        "svc_top1": svc_top1 / n,
        "svc_top3": svc_top3 / n,
        "grep_top1": grep_top1 / n,
        "grep_top3": grep_top3 / n,
    }


# ---------------------------------------------------------------------------
# Staleness detection
# ---------------------------------------------------------------------------

def _staleness_eval(store: FileContextStore, repo_root: Path) -> Dict[str, float]:
    """Mutate 3 files in the copy, then check stale_cards_for() for each path.

    The store was bootstrapped over repo_root. We now modify 3 files *in the copy*
    and verify that stale_cards_for(path) returns at least one card id for each
    mutated file (recall), while returning zero card ids for an unmutated file
    (precision).

    stale_cards_for() is the proper API for staleness queries: it checks every
    card that pins ``path`` for a sha256 mismatch, regardless of whether that card
    would score highly for any particular query. assemble() only checks the
    top-K relevance-ranked cards, so it gives lower recall when the mutated files
    don't match the query keywords.

    Precision = (mutated files detected stale) / (all files checked for staleness
                                                   where a hit was found).
    Recall    = (mutated files detected stale) / (files we mutated).
    """
    # Pick 3 Python source files that the store has cards for.
    all_cards = store._load_all()
    pinned_paths: List[str] = []
    unmutated_candidate: Optional[str] = None
    for card in all_cards.values():
        for fe in card.get("files", []):
            p = fe.get("path", "")
            if p.endswith(".py") and p not in pinned_paths:
                pinned_paths.append(p)
        if len(pinned_paths) >= 4:
            break

    if len(pinned_paths) < 3:
        print(f"  WARNING: only {len(pinned_paths)} pinned .py paths found; skipping staleness eval")
        return {"precision": 0.0, "recall": 0.0}

    mutated = pinned_paths[:3]
    # Use a 4th path (not mutated) as a precision witness if available.
    unmutated_candidate = pinned_paths[3] if len(pinned_paths) >= 4 else None

    # Append a harmless comment to each file in the repo copy.
    for rel in mutated:
        abs_path = repo_root / rel
        try:
            original = abs_path.read_text(encoding="utf-8")
            abs_path.write_text(original + "\n# eval_staleness_marker\n", encoding="utf-8")
        except OSError as exc:
            print(f"  WARNING: could not mutate {rel}: {exc}")
            return {"precision": 0.0, "recall": 0.0}

    # Use stale_cards_for() — the correct API for per-path staleness detection.
    detected_stale: Set[str] = set()
    for rel in mutated:
        card_ids = store.stale_cards_for(rel)
        if card_ids:
            detected_stale.add(rel)

    # Check that the unmutated witness file is NOT reported stale.
    false_positive = False
    if unmutated_candidate:
        fp_ids = store.stale_cards_for(unmutated_candidate)
        if fp_ids:
            false_positive = True

    mutated_set: Set[str] = set(mutated)
    true_positives = detected_stale & mutated_set
    # Precision: among files we checked where staleness was detected, how many
    # were actually mutated?
    total_positives = len(detected_stale) + (1 if false_positive else 0)
    precision = len(true_positives) / total_positives if total_positives > 0 else 1.0
    recall = len(true_positives) / len(mutated_set) if mutated_set else 0.0

    return {
        "precision": precision,
        "recall": recall,
        "mutated": mutated,
        "detected_stale": sorted(detected_stale),
        "unmutated_false_positive": false_positive,
        "unmutated_candidate": unmutated_candidate,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 64)
    print("eval_deterministic.py — FileContextStore evaluation")
    print("No LLM calls. No API key. Operates on a repo copy.")
    print("=" * 64)
    print()

    # Step 1: copy the repo.
    print("Step 1: Copy repo to temp dir (excluding .git/.venv/__pycache__/.quest-context)...")
    t0 = time.perf_counter()
    copy_root = _copy_repo(_REPO_ROOT)
    copy_ms = (time.perf_counter() - t0) * 1000
    print(f"  Copied to: {copy_root}  ({copy_ms:.0f} ms)")
    print()

    # Step 2: cold-start bootstrap.
    print("Step 2: Bootstrap FileContextStore over the copy...")
    cards_dir = copy_root / ".quest-context" / "eval-cards"
    store = FileContextStore(str(cards_dir), repo_root=str(copy_root), auto_bootstrap=False)

    t0 = time.perf_counter()
    n_cards = store.bootstrap(root=str(copy_root))
    bootstrap_ms = (time.perf_counter() - t0) * 1000

    n_files_pinned = _count_files_pinned(store)
    n_symbols = _count_symbols(store)

    print(f"  Cards written:   {n_cards}")
    print(f"  Files pinned:    {n_files_pinned}")
    print(f"  Symbols indexed: {n_symbols}")
    print(f"  Cold-start time: {bootstrap_ms:.0f} ms")
    print(f"  LLM calls:       0")
    print()

    # Step 3: routing accuracy.
    print(f"Step 3: Routing accuracy ({len(ROUTING_TASKS)} tasks)...")
    acc = _routing_accuracy(store, copy_root, ROUTING_TASKS)
    print(f"  Context service  top-1: {acc['svc_top1']:.0%}  top-3: {acc['svc_top3']:.0%}")
    print(f"  Blind-grep       top-1: {acc['grep_top1']:.0%}  top-3: {acc['grep_top3']:.0%}")
    print()
    print("  NOTE: keyword routing is NOT the claimed win for the context layer.")
    print("  The wins are: (a) fewer agent round-trips when context is cached,")
    print("  (b) deterministic zero-token staleness, (c) correctness never regresses.")
    print("  Report real numbers even if the service does not beat grep on cold routing.")
    print()

    # Step 4: staleness detection.
    print("Step 4: Staleness detection (mutate 3 files in the copy)...")
    stale_result = _staleness_eval(store, copy_root)
    precision = stale_result.get("precision", 0.0)
    recall = stale_result.get("recall", 0.0)
    mutated = stale_result.get("mutated", [])
    detected = stale_result.get("detected_stale", [])
    unmutated_fp = stale_result.get("unmutated_false_positive", False)
    unmutated_cand = stale_result.get("unmutated_candidate")
    print(f"  Files mutated:             {len(mutated)}")
    for p in mutated:
        print(f"    {p}")
    print(f"  Stale detected (per path): {len(detected)}")
    for p in detected:
        print(f"    {p}")
    if unmutated_cand:
        print(f"  Unmutated witness file:    {unmutated_cand}  -> false positive: {unmutated_fp}")
    print(f"  Precision: {precision:.2f}  Recall: {recall:.2f}")
    print(f"  LLM calls: 0")
    print()

    # Clean up.
    try:
        shutil.rmtree(str(copy_root.parent))
    except Exception:
        pass

    # Summary.
    print("=" * 64)
    print("SUMMARY")
    print("=" * 64)
    print(f"  Cold start:   {n_cards} cards, {n_files_pinned} files, "
          f"{n_symbols} symbols, {bootstrap_ms:.0f} ms, 0 LLM calls")
    print(f"  Routing:      service top-1 {acc['svc_top1']:.0%} / top-3 {acc['svc_top3']:.0%}  "
          f"vs  grep top-1 {acc['grep_top1']:.0%} / top-3 {acc['grep_top3']:.0%}")
    print(f"  Staleness:    precision {precision:.2f}, recall {recall:.2f}, 0 LLM calls")
    print()
    print("Done. The live tree was NOT modified.")


if __name__ == "__main__":
    main()
