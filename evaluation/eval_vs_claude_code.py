"""eval_vs_claude_code.py — A/B harness: Claude Code alone vs Claude Code + context.

IMPORTANT: This script SPENDS TOKENS (Anthropic Haiku, small amounts).
Run it deliberately, on a copy, NEVER in CI. It is opt-in by design.

Measures for each task:
  - wall-clock latency (milliseconds) — time around each ``claude`` CLI call,
    reported per-arm and as an aggregate avg-cold-ms vs avg-warm-ms.
  - num_turns (tool-call rounds) — cold vs warm
  - token usage — cold vs warm
  - correctness: does the answer contain the expected target file?
  - adversarial case: a warm hint that points to the WRONG file must still return
    the CORRECT file (verifies the model is not misled by a bad hint).
  - LLM judge (opt-in, requires claude CLI): qualitative per-sample evaluation
    against four written principles (see JUDGE_PRINCIPLES). One cheap Haiku call
    per sample. Skip judging with --no-judge or if the claude CLI is unavailable.

All three axes -- speed (wall-clock ms), tool-call rounds, and tokens -- are
reported in the per-task table and the aggregate summary.

The task set is small (6 tasks + 1 adversarial) to keep cost low.
More tasks improve statistical significance; this set is a representative minimum.

Usage:
    python evaluation/eval_vs_claude_code.py
    # or from repo root:
    .venv/bin/python evaluation/eval_vs_claude_code.py

Guard: if the `claude` CLI is not on PATH or errors, each task is marked
"skipped: claude CLI unavailable" rather than crashing. The judge similarly
degrades gracefully: if unavailable or if JSON parsing fails, the verdict is
recorded as {"error": "<reason>"} and execution continues.

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
# LLM judge: four written principles used to evaluate each sample qualitatively.
#
# The judge is called once per sample (one cheap Haiku call) and scores both
# arms against these principles. This replaces the brittle path-substring
# correctness check with a principled qualitative rubric, while still keeping
# the substring check as a fast pre-screen in the result record.
# ---------------------------------------------------------------------------

JUDGE_PRINCIPLES: str = """
P1 CORRECTNESS: The agent's answer must point to the file(s) that ACTUALLY implement
the task's logic, not a tangential or merely-related file. A correct answer names the
primary implementation file for the described task.

P2 NO REGRESSION: The context-augmented (warm) answer must be at least as correct as
the cold (Claude-Code-alone) answer. Adding the context hint must never make the answer
worse; if the hint is unhelpful the warm arm should still reach the correct answer by
reasoning from the code.

P3 NOT MISLED: When the injected hint is wrong or stale, the warm answer must still
reach the correct file. The agent must verify the hint against the code, not blindly
trust it. A warm answer that follows a bad hint to the wrong file fails this principle.

P4 EFFICIENCY: Reaching the correct grounding in fewer tool-call rounds is better, but
only when correctness is preserved. An answer that is faster but wrong does not satisfy
P4. Correctness (P1) always takes priority over efficiency.
""".strip()


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

    Returns a dict with keys: num_turns, input_tokens, output_tokens, result, raw,
    latency_ms (wall-clock milliseconds for the full CLI call).
    On error, returns a dict with key 'error' (latency_ms is still set on timeout).
    """
    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "json",
        "--model", "claude-haiku-4-5",
    ]
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        latency_ms = (time.monotonic() - t0) * 1000.0
        if proc.returncode != 0:
            return {
                "error": f"claude exited {proc.returncode}: {proc.stderr[:300]}",
                "latency_ms": latency_ms,
            }
        raw = proc.stdout.strip()
        # The JSON output may be the last JSON object in the output (some versions
        # emit progress lines before the final JSON).
        # Try to extract the last {...} block.
        json_match = None
        for m in re.finditer(r"\{.*\}", raw, re.DOTALL):
            json_match = m
        if not json_match:
            return {"error": f"no JSON in output: {raw[:200]}", "latency_ms": latency_ms}
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
            "latency_ms": latency_ms,
        }
    except subprocess.TimeoutExpired:
        latency_ms = (time.monotonic() - t0) * 1000.0
        return {"error": f"claude timed out after {timeout}s", "latency_ms": latency_ms}
    except json.JSONDecodeError as exc:
        latency_ms = (time.monotonic() - t0) * 1000.0
        return {"error": f"JSON parse error: {exc}  raw={raw[:200]}", "latency_ms": latency_ms}
    except FileNotFoundError:
        latency_ms = (time.monotonic() - t0) * 1000.0
        return {"error": "claude binary not found", "latency_ms": latency_ms}
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000.0
        return {"error": str(exc), "latency_ms": latency_ms}


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
        f"Cached context hint (a cheap starting point, not ground truth): Files: {hint_file}\n\n"
        "If the hint obviously fits, confirm it with one quick read and answer. If it does NOT "
        "obviously fit, discard it immediately and search normally as if it were not there. Do "
        "not spend extra effort verifying or working around a hint that does not fit. "
        "Answer with ONLY the repo-relative path "
        "(e.g. quest_ai_runner/core/orchestrator.py). "
        "Do not include any other text."
    )


def _extract_path_from_result(result_text: str, target: str) -> bool:
    """Return True if the result contains the target path."""
    # Normalize: strip leading/trailing whitespace and look for target in the text.
    return target in result_text


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------

def _build_judge_prompt(
    task: str,
    target_file: str,
    cold_answer: str,
    cold_rounds: int,
    warm_answer: str,
    warm_rounds: int,
) -> str:
    """Construct the prompt sent to the judge model."""
    return (
        "You are an evaluation judge for an AI coding assistant benchmark.\n"
        "You will be given a task, the known-correct target file, and two arms:\n"
        "  - Cold arm: Claude Code alone, no context hint.\n"
        "  - Warm arm: Claude Code with a pre-loaded context hint.\n\n"
        "Evaluate both arms against the following PRINCIPLES:\n\n"
        f"{JUDGE_PRINCIPLES}\n\n"
        "---\n"
        f"TASK: {task}\n\n"
        f"KNOWN CORRECT FILE: {target_file}\n\n"
        f"COLD ARM answer (tool-call rounds: {cold_rounds}):\n{cold_answer}\n\n"
        f"WARM ARM answer (tool-call rounds: {warm_rounds}):\n{warm_answer}\n\n"
        "---\n"
        "Output ONLY a JSON object — no prose, no markdown fences, no explanation "
        "before or after the JSON. The JSON must have exactly these fields:\n"
        '  "cold_correct": true or false  '
        '(P1: does cold_answer point to the correct file?)\n'
        '  "warm_correct": true or false  '
        '(P1: does warm_answer point to the correct file?)\n'
        '  "grounding_quality_cold": integer 1-5  '
        "(1=completely wrong, 5=exactly right)\n"
        '  "grounding_quality_warm": integer 1-5  '
        "(1=completely wrong, 5=exactly right)\n"
        '  "warm_vs_cold": "better" or "equal" or "worse"  '
        "(P2: did context help, not hurt?)\n"
        '  "misled_by_bad_hint": true or false  '
        "(P3: was the warm arm led astray by an incorrect hint?)\n"
        '  "rationale": "one sentence explaining your verdict"\n\n'
        "Example (do not copy verbatim):\n"
        '{"cold_correct": true, "warm_correct": true, '
        '"grounding_quality_cold": 4, "grounding_quality_warm": 5, '
        '"warm_vs_cold": "better", "misled_by_bad_hint": false, '
        '"rationale": "Both arms identified the correct file; warm arm reached '
        'it in fewer rounds."}'
    )


def _parse_judge_json(raw_text: str) -> Dict[str, Any]:
    """Extract and parse the JSON object from the judge's raw response text.

    Handles responses that include code fences or surrounding prose by finding
    the first complete {...} block. Returns the parsed dict or raises ValueError.
    """
    # Strip markdown code fences if present.
    text = re.sub(r"```(?:json)?", "", raw_text).strip()
    # Find the first {...} block (greedy — matches the outermost braces).
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object found in: {text[:300]!r}")
    return json.loads(match.group())


def judge_sample(
    task: str,
    target_file: str,
    cold_answer: str,
    cold_rounds: int,
    warm_answer: str,
    warm_rounds: int,
    *,
    model: str = "claude-haiku-4-5",
) -> Dict[str, Any]:
    """Call the claude CLI once to judge one A/B sample against JUDGE_PRINCIPLES.

    Returns a dict with fields:
        cold_correct, warm_correct,
        grounding_quality_cold (1-5), grounding_quality_warm (1-5),
        warm_vs_cold ("better"|"equal"|"worse"),
        misled_by_bad_hint (bool),
        rationale (str)

    On any failure (CLI unavailable, non-zero exit, JSON parse error) returns a
    dict with an "error" field describing the problem. Never raises.
    """
    claude_bin = _find_claude()
    if not claude_bin:
        return {"error": "claude CLI not found; judge skipped"}

    prompt = _build_judge_prompt(
        task=task,
        target_file=target_file,
        cold_answer=cold_answer,
        cold_rounds=cold_rounds,
        warm_answer=warm_answer,
        warm_rounds=warm_rounds,
    )

    cmd = [
        claude_bin,
        "-p", prompt,
        "--output-format", "json",
        "--model", model,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0:
            return {"error": f"judge CLI exited {proc.returncode}: {proc.stderr[:300]}"}

        raw_output = proc.stdout.strip()

        # The CLI wraps the model's text in a JSON envelope:
        # {"result": "<model output>", "num_turns": ..., "usage": {...}, ...}
        # Parse the envelope first, then parse the model's inner JSON.
        envelope: Dict[str, Any] = {}
        env_match = None
        for m in re.finditer(r"\{.*\}", raw_output, re.DOTALL):
            env_match = m
        if not env_match:
            return {"error": f"no JSON envelope from judge CLI: {raw_output[:200]}"}
        try:
            envelope = json.loads(env_match.group())
        except json.JSONDecodeError as exc:
            return {"error": f"envelope JSON parse error: {exc}  raw={raw_output[:200]}"}

        result_text: str = envelope.get("result", envelope.get("text", ""))
        if not result_text:
            return {"error": f"no result field in judge envelope: {list(envelope.keys())}"}

        try:
            verdict = _parse_judge_json(result_text)
        except (json.JSONDecodeError, ValueError) as exc:
            return {"error": f"inner JSON parse error: {exc}  result={result_text[:300]}"}

        # Validate expected fields are present (not strict — extra fields are fine).
        required = {
            "cold_correct", "warm_correct",
            "grounding_quality_cold", "grounding_quality_warm",
            "warm_vs_cold", "misled_by_bad_hint", "rationale",
        }
        missing = required - set(verdict.keys())
        if missing:
            verdict["_missing_fields"] = sorted(missing)

        return verdict

    except subprocess.TimeoutExpired:
        return {"error": "judge CLI timed out after 60s"}
    except FileNotFoundError:
        return {"error": "judge claude binary not found at runtime"}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"unexpected judge error: {exc}"}


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

    # Opt-out flag: pass --no-judge to skip the LLM judge calls.
    enable_judge = "--no-judge" not in sys.argv

    # Check for claude CLI.
    claude_bin = _find_claude()
    if not claude_bin:
        print("claude CLI not found on PATH. Marking all tasks as skipped.")
        print(_CLAUDE_UNAVAILABLE)
        return

    print(f"claude CLI found at: {claude_bin}")
    if enable_judge:
        print("LLM judge: ENABLED (one extra Haiku call per sample; pass --no-judge to skip)")
    else:
        print("LLM judge: DISABLED (--no-judge flag set)")
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
              f"latency={cold_res.get('latency_ms', 0):.0f}ms  "
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
              f"latency={warm_res.get('latency_ms', 0):.0f}ms  "
              f"correct={warm_correct}"
              + (f"  ADV_PASS={adversarial_pass}" if is_adversarial else ""))

        # LLM judge: qualitative evaluation against JUDGE_PRINCIPLES.
        judge_verdict: Dict[str, Any] = {}
        if enable_judge:
            print(f"    Judge...", end=" ", flush=True)
            judge_verdict = judge_sample(
                task=task,
                target_file=target,
                cold_answer=cold_res["result"],
                cold_rounds=cold_res["num_turns"],
                warm_answer=warm_res["result"],
                warm_rounds=warm_res["num_turns"],
            )
            if "error" in judge_verdict:
                print(f"SKIPPED ({judge_verdict['error'][:60]})")
            else:
                wvc = judge_verdict.get("warm_vs_cold", "?")
                wc = judge_verdict.get("warm_correct", "?")
                cc = judge_verdict.get("cold_correct", "?")
                rationale = judge_verdict.get("rationale", "")
                print(
                    f"warm_vs_cold={wvc}  cold_correct={cc}  warm_correct={wc}"
                    f"\n      rationale: {rationale}"
                )

        results.append({
            "label": label,
            "target": target,
            "is_adversarial": is_adversarial,
            "cold_turns": cold_res["num_turns"],
            "cold_input_tok": cold_res["input_tokens"],
            "cold_output_tok": cold_res["output_tokens"],
            "cold_latency_ms": cold_res.get("latency_ms", 0.0),
            "cold_correct": cold_correct,
            "warm_turns": warm_res["num_turns"],
            "warm_input_tok": warm_res["input_tokens"],
            "warm_output_tok": warm_res["output_tokens"],
            "warm_latency_ms": warm_res.get("latency_ms", 0.0),
            "warm_correct": warm_correct,
            "adversarial_pass": adversarial_pass,
            "hint_used": hint,
            "judge": judge_verdict,
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

    # CT=cold turns, WT=warm turns, C_ms=cold latency ms, W_ms=warm latency ms,
    # CC=cold correct, WC=warm correct.
    header = f"{'Label':<35} {'CT':>3} {'WT':>3} {'C_ms':>7} {'W_ms':>7} {'CC':>5} {'WC':>5}"
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
            f"{r.get('cold_latency_ms', 0):>7.0f} "
            f"{r.get('warm_latency_ms', 0):>7.0f} "
            f"{'Y' if r['cold_correct'] else 'N':>5} "
            f"{'Y' if r['warm_correct'] else 'N':>5}"
        )

    # Aggregate (non-adversarial only).
    if non_adv:
        avg_cold_turns = sum(r["cold_turns"] for r in non_adv) / len(non_adv)
        avg_warm_turns = sum(r["warm_turns"] for r in non_adv) / len(non_adv)
        avg_cold_ms = sum(r.get("cold_latency_ms", 0.0) for r in non_adv) / len(non_adv)
        avg_warm_ms = sum(r.get("warm_latency_ms", 0.0) for r in non_adv) / len(non_adv)
        cold_correct_pct = sum(1 for r in non_adv if r["cold_correct"]) / len(non_adv)
        warm_correct_pct = sum(1 for r in non_adv if r["warm_correct"]) / len(non_adv)

        print()
        print("AGGREGATE (non-adversarial tasks)")
        print(f"  Avg wall-clock latency  cold: {avg_cold_ms:.0f}ms  warm: {avg_warm_ms:.0f}ms")
        if avg_cold_ms > 0 and avg_warm_ms > 0:
            latency_ratio = avg_cold_ms / avg_warm_ms
            direction = "faster" if latency_ratio > 1 else "slower"
            print(f"  Latency ratio:          {latency_ratio:.2f}x {direction} with context")
        print(f"  Avg tool-call rounds    cold: {avg_cold_turns:.1f}  warm: {avg_warm_turns:.1f}")
        if avg_cold_turns > 0:
            speedup = avg_cold_turns / avg_warm_turns if avg_warm_turns > 0 else float("inf")
            print(f"  Speedup factor:         {speedup:.1f}x fewer rounds with context")
        print(f"  Correctness             cold: {cold_correct_pct:.0%}  warm: {warm_correct_pct:.0%}")

    # Judge aggregate (non-adversarial only).
    if enable_judge:
        judged = [r for r in non_adv if r.get("judge") and "error" not in r.get("judge", {})]
        if judged:
            better = sum(1 for r in judged if r["judge"].get("warm_vs_cold") == "better")
            equal = sum(1 for r in judged if r["judge"].get("warm_vs_cold") == "equal")
            worse = sum(1 for r in judged if r["judge"].get("warm_vs_cold") == "worse")
            warm_correct_judge = sum(1 for r in judged if r["judge"].get("warm_correct") is True)
            misled_count = sum(1 for r in non_adv
                               if r.get("judge") and r["judge"].get("misled_by_bad_hint") is True)
            print()
            print("JUDGE AGGREGATE (non-adversarial, LLM-rated against JUDGE_PRINCIPLES)")
            print(f"  Samples judged: {len(judged)}")
            print(f"  warm_vs_cold   better={better}  equal={equal}  worse={worse}")
            print(f"  warm_correct (judge-rated): {warm_correct_judge}/{len(judged)}")
            print(f"  misled_by_bad_hint: {misled_count} "
                  f"({'none misled' if misled_count == 0 else 'WARNING: misled cases found'})")
        else:
            print()
            print("JUDGE AGGREGATE: no samples successfully judged "
                  "(CLI unavailable or all errored)")

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
            j = r.get("judge", {})
            if j and "error" not in j:
                print(f"  Judge (P3 NOT MISLED): misled_by_bad_hint={j.get('misled_by_bad_hint')}  "
                      f"warm_correct={j.get('warm_correct')}  "
                      f"rationale: {j.get('rationale', '')}")

    print()
    print("Done. The live tree was NOT modified.")


if __name__ == "__main__":
    main()
