"""Live engine of the claude_code_ab arms: find the claude CLI, run it headless against a copy of this repo, and build the cold and warm prompts. Moved here from the retired standalone evaluation script so the component owns its engine."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import common

COPY_SKIP_DIRS: Set[str] = {
    ".git", ".venv", "venv", "__pycache__",
    "node_modules", ".quest-context", ".eggs",
    ".mypy_cache", ".pytest_cache", "dist", "build",
    "quest_ai_runner.egg-info",
}

def find_claude() -> Optional[str]:
    """Return the path to the `claude` CLI, or None if not found."""
    found = shutil.which("claude")
    if found:
        return found
    # Also check common locations.
    for candidate in ["/usr/local/bin/claude", "/usr/bin/claude",
                      os.path.expanduser("~/.local/bin/claude"),
                      str(common.REPO / ".venv" / "bin" / "claude")]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def run_claude(
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


def cold_prompt(task: str) -> str:
    """Prompt for the cold (no hint) arm."""
    return (
        f"{task}\n\n"
        "Use your tools to locate the relevant file in this repository. "
        "When you have identified it, answer with ONLY the repo-relative path "
        "(e.g. quest_ai_runner/core/orchestrator.py). "
        "Do not include any other text."
    )


def warm_prompt(task: str, hint_file: str) -> str:
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




def copy_repo(src: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="qar_ab_eval_"))

    def skip_names(directory: str, names: List[str]) -> Set[str]:
        return {n for n in names if n in COPY_SKIP_DIRS}

    shutil.copytree(src, dest / "repo", ignore=skip_names)
    return dest / "repo"


def runner(meter, warm):
    claude = find_claude()
    if not claude:
        raise SystemExit("the live arms need the claude CLI; use --impl idf_hint offline")
    copy = copy_repo(common.REPO)

    def locate(case):
        meter.record()
        if warm:
            from context_selection.corpus import corpus_for
            from context_selection.impls import idf_docstrings
            hint = case["wrong_hint"] or ", ".join(idf_docstrings.build(meter)(case["task"], corpus_for(common.REPO))[:3])
            prompt = warm_prompt(case["task"], hint)
        else:
            prompt = cold_prompt(case["task"])
        res = run_claude(claude, prompt, copy)
        if "error" in res:
            raise RuntimeError(res["error"])
        return {"answer": res["result"], "turns": res["num_turns"]}
    locate.cleanup = lambda: shutil.rmtree(copy.parent, ignore_errors=True)
    return locate
