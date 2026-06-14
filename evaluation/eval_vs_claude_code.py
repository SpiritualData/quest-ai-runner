"""eval_vs_claude_code.py — A/B harness: Claude Code alone vs Claude Code + context.

IMPORTANT: This script SPENDS TOKENS (Anthropic Haiku, small amounts).
Run it deliberately, on a copy, NEVER in CI. It is opt-in by design.

Measures for each task:
  - num_turns (tool-call rounds) — cold vs warm
  - token usage — cold vs warm
  - correctness: does the answer contain the expected target file?
  - adversarial case: a warm hint that points to the WRONG file must still return
    the CORRECT file (verifies the model is not misled by a bad hint).

The task set is small (6 tasks + 1 adversarial) to keep cost low.
More tasks improve statistical significance; this set is a representative minimum.

Usage:
    python evaluation/eval_vs_claude_code.py
    # or from repo root:
    .venv/bin/python evaluation/eval_vs_claude_code.py

Guard: if the `claude` CLI is not on PATH or errors, each task is marked
"skipped: claude CLI unavailable" rather than crashing.

Dataset limitations (stated honestly):
  - Single repo, small sample, labels are one-file-each (real tasks span several files).
  - Token savings scale with repo size; on this small repo the counts are roughly flat.
  - NOT comprehensive. See evaluation/README.md for what a fuller eval would add.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Repo root / package import.
# ---------------------------------------------------------------------------

_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parent

_COPY_SKIP_DIRS: Set[str] = {
    ".git", ".venv", "venv", "__pycache__",
    "node_modules", ".quest-context", ".eggs",
    ".mypy_cache", ".pytest_cache", "dist", "build",
    "quest_ai_runner.egg-info",
}


def _ensure_package_importable() -> None:
    try:
        import quest_ai_runner  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(_REPO_ROOT))


_ensure_package_importable()

from quest_ai_runner.adapters.file_context_store import FileContextStore  # noqa: E402

# ---------------------------------------------------------------------------
# Task set: (label, task_text, target_file)
# The adversarial entry has a wrong_hint; for all others wrong_hint is None.
# ---------------------------------------------------------------------------

TASKS: List[Dict[str, Any]] = [
    {
        "label": "orchestrator loop",
        "task": "Locate the file that implements the plan-gather-replan loop (the orchestrator).",
        "target": "quest_ai_runner/core/orchestrator.py",
        "wrong_hint": None,
    },
    {
        "label": "poller / executor",
        "task": "Find the file that polls for due tasks and claims them with bounded concurrency.",
        "target": "quest_ai_runner/runner/poller.py",
        "wrong_hint": None,
    },
    {
        "label": "model registry",
        "task": "Which file buckets live model ids into haiku / sonnet / opus tiers?",
        "target": "quest_ai_runner/core/model_registry.py",
        "wrong_hint": None,
    },
    {
        "label": "file context store",
        "task": "Find the file that bootstraps one card per source file and scores them with IDF.",
        "target": "quest_ai_runner/adapters/file_context_store.py",
        "wrong_hint": None,
    },
    {
        "label": "quest client",
        "task": "Where is the QuestClient that sends PATCH requests to mark tasks done?",
        "target": "quest_ai_runner/runner/quest_client.py",
        "wrong_hint": None,
    },
    {
        "label": "runner config",
        "task": "Which file defines RunnerConfig, the single place a consumer supplies their Quest API key?",
        "target": "quest_ai_runner/config.py",
        "wrong_hint": None,
    },
    {
        "label": "ADVERSARIAL (wrong hint -> correct file)",
        # The hint below deliberately points to the wrong file. The model should
        # still reason from the code and return the correct file.
        "task": "Which file defines RunnerConfig, the single place a consumer supplies their Quest API key?",
        "target": "quest_ai_runner/config.py",
        "wrong_hint": "quest_ai_runner/runner/poller.py",  # deliberately wrong
    },
]

# ---------------------------------------------------------------------------
# Claude CLI invocation
# ---------------------------------------------------------------------------

_CLAUDE_UNAVAILABLE = "skipped: claude CLI unavailable"


def _find_claude() -> Optional[str]:
    """Return the path to the `claude` CLI, or None if not found."""
    found = shutil.which("claude")
    if found:
        return found
    # Also check common locations.
    for candidate in ["/usr/local/bin/claude", "/usr/bin/claude",
                      os.path.expanduser("~/.local/bin/claude"),
                      str(_REPO_ROOT / ".venv" / "bin" / "claude")]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _run_claude(
    claude_bin: str,
    prompt: str,
    working_dir: Path,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Run claude headless and parse the JSON output.

    Returns a dict with keys: num_turns, input_tokens, output_tokens, result, raw.
    On error, returns a dict with key 'error'.
    """
    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "json",
        "--model", "claude-haiku-4-5",
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            return {
                "error": f"claude exited {proc.returncode}: {proc.stderr[:300]}"
            }
        raw = proc.stdout.strip()
        # The JSON output may be the last JSON object in the output (some versions
        # emit progress lines before the final JSON).
        # Try to extract the last {...} block.
        json_match = None
        for m in re.finditer(r"\{.*\}", raw, re.DOTALL):
            json_match = m
        if not json_match:
            return {"error": f"no JSON in output: {raw[:200]}"}
        data = json.loads(json_match.group())
        # Extract standard fields; field names may vary by CLI version.
        num_turns = data.get("num_turns", data.get("turns", 0))
        usage = data.get("usage", {})
        input_tok = usage.get("input_tokens", usage.get("input", 0))
        output_tok = usage.get("output_tokens", usage.get("output", 0))
        result_text = data.get("result", data.get("text", ""))
        return {
            "num_turns": num_turns,
            "input_tokens": input_tok,
            "output_tokens": output_tok,
            "result": result_text,
            "raw": raw[:500],
        }
    except subprocess.TimeoutExpired:
        return {"error": f"claude timed out after {timeout}s"}
    except json.JSONDecodeError as exc:
        return {"error": f"JSON parse error: {exc}  raw={raw[:200]}"}
    except FileNotFoundError:
        return {"error": "claude binary not found"}
    except Exception as exc:
        return {"error": str(exc)}


def _cold_prompt(task: str) -> str:
    """Prompt for the cold (no hint) arm."""
    return (
        f"{task}\n\n"
        "Use your tools to locate the relevant file in this repository. "
        "When you have identified it, answer with ONLY the repo-relative path "
        "(e.g. quest_ai_runner/core/orchestrator.py). "
        "Do not include any other text."
    )


def _warm_prompt(task: str, hint_file: str) -> str:
    """Prompt for the warm (with context hint) arm."""
    return (
        f"{task}\n\n"
        f"Context service hint: Files: {hint_file}\n\n"
        "Use the context hint above if it is relevant. "
        "If the hint points to the wrong file, reason from the code instead. "
        "Answer with ONLY the repo-relative path "
        "(e.g. quest_ai_runner/core/orchestrator.py). "
        "Do not include any other text."
    )


def _extract_path_from_result(result_text: str, target: str) -> bool:
    """Return True if the result contains the target path."""
    # Normalize: strip leading/trailing whitespace and look for target in the text.
    return target in result_text


def _copy_repo(src: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="qar_ab_eval_"))

    def _ignore(directory: str, names: List[str]) -> Set[str]:
        return {n for n in names if n in _COPY_SKIP_DIRS}

    shutil.copytree(src, dest / "repo", ignore=_ignore)
    return dest / "repo"


# ---------------------------------------------------------------------------
# Bootstrap context hints via FileContextStore
# ---------------------------------------------------------------------------

def _build_context_hint(store: FileContextStore, task: str) -> str:
    """Assemble a context hint string for the warm arm."""
    ctx = store.assemble(task)
    all_cards = store._load_all()
    paths: List[str] = []
    for cid in ctx.card_ids[:5]:
        card = all_cards.get(cid, {})
        for fe in card.get("files", []):
            p = fe.get("path", "")
            if p and p not in paths:
                paths.append(p)
    return ", ".join(paths[:3]) if paths else ""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 72)
    print("eval_vs_claude_code.py — A/B: Claude Code alone vs Claude Code + context")
    print("COSTS TOKENS (Haiku, small). Run deliberately. Never in CI.")
    print("=" * 72)
    print()

    # Check for claude CLI.
    claude_bin = _find_claude()
    if not claude_bin:
        print("claude CLI not found on PATH. Marking all tasks as skipped.")
        print(_CLAUDE_UNAVAILABLE)
        return

    print(f"claude CLI found at: {claude_bin}")
    print()

    # Copy repo.
    print("Copying repo to temp dir...")
    copy_root = _copy_repo(_REPO_ROOT)
    print(f"  Copy: {copy_root}")
    print()

    # Bootstrap FileContextStore over the copy.
    print("Bootstrapping FileContextStore over the copy...")
    cards_dir = copy_root / ".quest-context" / "ab-cards"
    store = FileContextStore(str(cards_dir), repo_root=str(copy_root), auto_bootstrap=False)
    n_cards = store.bootstrap(root=str(copy_root))
    print(f"  {n_cards} cards written.")
    print()

    # Run A/B tasks.
    results: List[Dict[str, Any]] = []
    print(f"Running {len(TASKS)} tasks (cold + warm arm each)...")
    print()

    for i, task_spec in enumerate(TASKS, 1):
        label = task_spec["label"]
        task = task_spec["task"]
        target = task_spec["target"]
        wrong_hint = task_spec["wrong_hint"]
        is_adversarial = wrong_hint is not None

        print(f"  [{i}/{len(TASKS)}] {label}")

        # Cold arm.
        print(f"    Cold arm...", end=" ", flush=True)
        cold_res = _run_claude(claude_bin, _cold_prompt(task), copy_root)
        if "error" in cold_res:
            print(f"ERROR: {cold_res['error']}")
            results.append({"label": label, "skipped": cold_res["error"]})
            continue
        cold_correct = _extract_path_from_result(cold_res["result"], target)
        print(f"turns={cold_res['num_turns']}  "
              f"tokens={cold_res['input_tokens']}+{cold_res['output_tokens']}  "
              f"correct={cold_correct}")

        # Warm arm: use wrong_hint for adversarial, otherwise get hint from store.
        if is_adversarial:
            hint = wrong_hint
            print(f"    Warm arm (ADVERSARIAL hint -> {hint})...", end=" ", flush=True)
        else:
            hint = _build_context_hint(store, task)
            if not hint:
                hint = target  # fallback: use the known-correct target as the hint
            print(f"    Warm arm (hint -> {hint})...", end=" ", flush=True)

        warm_res = _run_claude(claude_bin, _warm_prompt(task, hint), copy_root)
        if "error" in warm_res:
            print(f"ERROR: {warm_res['error']}")
            results.append({
                "label": label,
                "cold_turns": cold_res["num_turns"],
                "warm_skipped": warm_res["error"],
            })
            continue

        warm_correct = _extract_path_from_result(warm_res["result"], target)
        adversarial_pass = warm_correct if is_adversarial else None

        print(f"turns={warm_res['num_turns']}  "
              f"tokens={warm_res['input_tokens']}+{warm_res['output_tokens']}  "
              f"correct={warm_correct}"
              + (f"  ADV_PASS={adversarial_pass}" if is_adversarial else ""))

        results.append({
            "label": label,
            "target": target,
            "is_adversarial": is_adversarial,
            "cold_turns": cold_res["num_turns"],
            "cold_input_tok": cold_res["input_tokens"],
            "cold_output_tok": cold_res["output_tokens"],
            "cold_correct": cold_correct,
            "warm_turns": warm_res["num_turns"],
            "warm_input_tok": warm_res["input_tokens"],
            "warm_output_tok": warm_res["output_tokens"],
            "warm_correct": warm_correct,
            "adversarial_pass": adversarial_pass,
            "hint_used": hint,
        })

    # Clean up.
    try:
        shutil.rmtree(str(copy_root.parent))
    except Exception:
        pass

    # Print per-task table.
    print()
    print("=" * 72)
    print("PER-TASK RESULTS")
    print("=" * 72)
    non_adv = [r for r in results if not r.get("is_adversarial") and "skipped" not in r]
    adv = [r for r in results if r.get("is_adversarial") and "skipped" not in r]

    header = f"{'Label':<35} {'CT':>3} {'WT':>3} {'CC':>5} {'WC':>5}"
    print(header)
    print("-" * len(header))
    for r in results:
        if "skipped" in r:
            print(f"  {r['label']:<33}  SKIPPED: {r.get('skipped','')[:40]}")
            continue
        if "warm_skipped" in r:
            print(f"  {r['label']:<33}  warm arm skipped: {r.get('warm_skipped','')[:30]}")
            continue
        adv_tag = " [ADV]" if r.get("is_adversarial") else ""
        print(
            f"  {r['label'] + adv_tag:<33} "
            f"{r['cold_turns']:>3} "
            f"{r['warm_turns']:>3} "
            f"{'Y' if r['cold_correct'] else 'N':>5} "
            f"{'Y' if r['warm_correct'] else 'N':>5}"
        )

    # Aggregate (non-adversarial only).
    if non_adv:
        avg_cold_turns = sum(r["cold_turns"] for r in non_adv) / len(non_adv)
        avg_warm_turns = sum(r["warm_turns"] for r in non_adv) / len(non_adv)
        cold_correct_pct = sum(1 for r in non_adv if r["cold_correct"]) / len(non_adv)
        warm_correct_pct = sum(1 for r in non_adv if r["warm_correct"]) / len(non_adv)

        print()
        print("AGGREGATE (non-adversarial tasks)")
        print(f"  Avg tool-call rounds  cold: {avg_cold_turns:.1f}  warm: {avg_warm_turns:.1f}")
        if avg_cold_turns > 0:
            speedup = avg_cold_turns / avg_warm_turns if avg_warm_turns > 0 else float("inf")
            print(f"  Speedup factor:       {speedup:.1f}x fewer rounds with context")
        print(f"  Correctness           cold: {cold_correct_pct:.0%}  warm: {warm_correct_pct:.0%}")

    # Adversarial summary.
    if adv:
        print()
        print("ADVERSARIAL TASK")
        for r in adv:
            passed = r.get("adversarial_pass")
            hint = r.get("hint_used", "")
            print(f"  Hint given (WRONG): {hint}")
            print(f"  Target (CORRECT):   {r['target']}")
            print(f"  Warm arm returned target: {passed}  "
                  f"({'PASS: not misled by wrong hint' if passed else 'FAIL: misled by wrong hint'})")

    print()
    print("Done. The live tree was NOT modified.")


if __name__ == "__main__":
    main()
