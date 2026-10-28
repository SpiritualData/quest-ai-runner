"""The engine of the task_replay arms: one recorded task shape, replayed through the real deep-run path.

What is real: the Orchestrator's deep goal loop (``_run_deep``), the ``SubprocessGoalRunner`` that spawns Claude Code in a
scratch workspace, the done verifier (``_verify_goal``, one small model call through the Claude CLI), the tool registry and
metric ledger, and the iterate-by-spec arm under test (``GoalIteration``). What is faked is the Quest boundary only: the
Quest base URL is one that never resolves and the key is a placeholder, so nothing outside the scratch folder can be
reached, and every ``QAR_`` setting is scrubbed so the run starts from the library's own defaults.

Spend: the worker and the verifier both go through the Claude CLI, so on a subscription login no API dollars are spent.
Each worker spawn and each verifier call is counted on the harness meter, and the CLI's own reported cost (a list-price
figure, not a charge) is kept in the row's details.

Nothing here decides whether a task was met. The verdict is the done verifier's; the work check, where a case has one, is the
literal condition the source test asserted, read from the scratch folder afterwards.
"""
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import common

SKIP_NAMES = {".claude", ".git", ".quest-context", "__pycache__", ".pytest_cache"}
# What an arm is: the iterate-by-spec implementation, whether the metric tool is offered, and the attempt cap.
CURRENT = dict(iteration="slice-and-writeback", tool=True, max_iterations=None)
FLOOR = dict(iteration="generic-doctrine", tool=False, max_iterations=1)


def materialize(case: Dict[str, Any], root: Path) -> None:
    """Write the case's workspace: its files (text, or text after generated numbered lines) and, when it names one, the
    scaffold repository esoi itself creates (the fixture of the tests that use ``esoi.scaffold``)."""
    spec = case["workspace"]
    if spec.get("scaffold"):
        from esoi import scaffold
        scaffold.init(root, spec["scaffold"], say=lambda *_: None)
    for rel, entry in (spec.get("files") or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        text = entry if isinstance(entry, str) else entry["text"]
        lead = "" if isinstance(entry, str) or not entry.get("numbered_lines") else (
            "\n".join(entry["numbered_lines"]["template"].format(i=i) for i in range(1, entry["numbered_lines"]["count"] + 1))
            + "\n\n")
        target.write_text(lead + text, encoding="utf-8")


def snapshot(root: Path) -> Dict[str, str]:
    """Every file under the folder, by relative path, with its text (binary files by size): what the work is compared with."""
    seen: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_dir() or any(part in SKIP_NAMES for part in rel.parts):
            continue
        try:
            seen[rel.as_posix()] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            seen[rel.as_posix()] = f"<{path.stat().st_size} bytes>"
    return seen


def work_check(check: Optional[Dict[str, Any]], before: Dict[str, str], after: Dict[str, str]) -> Optional[bool]:
    """The case's own condition on the scratch folder after the run, or None when the case states none."""
    if not check:
        return None
    kind, path = check["kind"], check.get("path", "")
    text = after.get(path)
    if kind == "unchanged":
        return text is not None and text == before.get(path)
    if text is None:
        return False
    if kind == "regex":
        return bool(re.search(check["pattern"], text, re.M)) and not any(
            re.search(p, text, re.M) for p in check.get("absent", []))
    raise ValueError(f"unknown work check {kind!r}")


class VerdictLog:
    """Wraps the orchestrator's done verifier to keep every verdict it returns, in order. It changes nothing it returns."""

    def __init__(self, orch: Any):
        self.verdicts: List[Dict[str, Any]] = []
        original = orch._verify_goal

        def logged(*args: Any, **kwargs: Any):
            verdict, error = original(*args, **kwargs)
            self.verdicts.append({"met": None if verdict is None else bool(verdict.get("met")), "error": error,
                                  "reason": str((verdict or {}).get("reason") or "")[:240]})
            return verdict, error
        orch._verify_goal = logged


def find_claude() -> Optional[str]:
    from claude_code_ab import live_support
    return live_support.find_claude()


def scratch_env(scratch: Path, claude: str) -> Dict[str, str]:
    """The settings of one replay, on top of the scrubbed environment: a Quest base that cannot resolve, the Claude CLI as the
    model backend (the subscription), and every ledger inside the scratch folder."""
    return {"QUEST_BASE_URL": common.OFFLINE_QUEST_URL, "QUEST_API_KEY": "offline-eval", "QAR_STANDARD_TOOLS": "1",
            "QAR_MODEL_BACKEND": "claude_cli", "QAR_CLAUDE_PATH": claude, "QAR_CLAUDE_LIVE_MODELS": "0",
            "QAR_CORPUS_ROOT": str(scratch / "corpus"), "QAR_CONVERSATION_SEARCH": "false",
            "QUEST_AI_RECENT_CONTEXT_DISABLED": "1", "QUEST_AI_ANTICIPATION": "off",
            "QAR_METRIC_LEDGER_DIR": str(scratch / "metrics"), "QAR_TOOL_LEDGER_DIR": str(scratch / "tools"),
            "QUEST_AI_CARDS_BACKEND": "files", "QUEST_AI_CARDS_DIR": str(scratch / "cards"),
            "QUEST_AI_DOCS_CARDS_DIR": str(scratch / "docs_cards"), "QAR_DEEP_REVIEW_INTERVAL_SECONDS": "0", "QAR_DEEP_INSTRUCTIONS_FILES": ""}


def build_orchestrator(arm: Dict[str, Any], workspace: Path, claude: str, timeout: float, runner_factory: Optional[Callable] = None):
    """The real orchestrator for one replay: a Claude Code worker in ``workspace`` and the arm's iterate-by-spec."""
    from quest_ai_runner.cli import _config_from_env
    from quest_ai_runner.config import build_orchestrator as build
    from quest_ai_runner.core.goal_runner import SubprocessConfig, SubprocessGoalRunner
    from quest_ai_runner.core.resolve import resolve
    cfg = _config_from_env()
    cfg.selections = {**(cfg.selections or {}), "iterate-by-spec": arm["iteration"]}
    worker_cfg = SubprocessConfig(working_dir=str(workspace), claude_path=claude, timeout_seconds=timeout,
                                  review_interval_seconds=0, extra_path_dirs=[str(common.REPO / ".venv" / "bin")])
    cfg.deep_runner = (runner_factory or SubprocessGoalRunner)(worker_cfg)
    orch = build(cfg)
    orch.goal_iteration = resolve("iterate-by-spec", overrides=cfg.selections)()
    if arm["max_iterations"] is not None:
        orch.cfg.deep_goal_max_iterations = arm["max_iterations"]
    registry = orch.tools
    for name in list(registry.names()):
        if name != "record_metric_result" or not arm["tool"]:
            registry.unregister(name)
    return orch


def count_spawns(runner: Any, meter: Any, spawned: List[Dict[str, Any]]) -> None:
    """Counts each worker spawn on the meter, and keeps what the worker reported (tokens, the CLI's list-price cost)."""
    original = runner.run_goal

    def counted(*args: Any, **kwargs: Any):
        meter.record()
        done = original(*args, **kwargs)
        spawned.append({"tokens": getattr(done, "tokens", 0) or 0, "cost": getattr(done, "cost_usd", 0.0) or 0.0,
                        "met": bool(getattr(done, "met", False)), "resumed": bool(kwargs.get("resume_session_id")),
                        "error": getattr(done, "error", None)})
        return done
    runner.run_goal = counted


def replay(case: Dict[str, Any], arm: Dict[str, Any], meter: Any, *, timeout: float = 420.0,
           runner_factory: Optional[Callable] = None, provider: Any = None, claude: Optional[str] = None) -> Dict[str, Any]:
    """Replay one case with one arm. Returns the facts the scorer reads; never raises for a worker that failed."""
    from quest_ai_runner.core.orchestrator import PlanDecision
    claude = claude or find_claude()
    if not claude:
        raise SystemExit("task_replay needs the claude CLI on the path")
    scratch = Path(tempfile.mkdtemp(prefix="qar_replay_"))
    cost_before = meter.cost_usd
    workspace = scratch / "work"
    workspace.mkdir()
    (scratch / "corpus").mkdir()
    try:
        materialize(case, workspace)
        before = snapshot(workspace)
        with common.hermetic_env(scratch_env(scratch, claude)):
            orch = build_orchestrator(arm, workspace, claude, timeout, runner_factory)
            if provider is not None:
                orch.provider = provider
            meter.wrap(orch.provider)
            log = VerdictLog(orch)
            spawned: List[Dict[str, Any]] = []
            count_spawns(orch.deep_runner, meter, spawned)
            if orch.deep_runner_ladder and orch.deep_runner_ladder[0] is not orch.deep_runner:
                count_spawns(orch.deep_runner_ladder[0], meter, spawned)
            plan = PlanDecision(action="deep", goal=case["goal"], deep_brief=case.get("brief") or case["goal"])
            result = orch._run_deep(plan, case["goal"], "sonnet", ctx_meta={"task_id": f"replay-{case['id']}"},
                                    working_dir_override=str(workspace))
            done = result.deep_results[0] if result.deep_results else None
        after = snapshot(workspace)
        # The CLI reports a list-price figure for each call; on a subscription login nothing is charged, so it is kept in the
        # row's details and the meter's dollars are put back to what was spent.
        verifier_list = meter.cost_usd - cost_before
        meter.cost_usd = cost_before
        changed = sorted(p for p in after if after[p] != before.get(p))
        created = sorted(p for p in after if p not in before)
        return dict(verdicts=log.verdicts, spawns=spawned, met=bool(done and done.met), changed=changed, created=created,
                    work_ok=work_check(case.get("work_check"), before, after),
                    metric_results=list(getattr(done, "metric_results", None) or []),
                    metric_missing=bool(getattr(done, "metric_missing", False)),
                    cli_list_cost_usd=round(sum(s["cost"] for s in spawned) + verifier_list, 4), error=getattr(done, "error", None))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
