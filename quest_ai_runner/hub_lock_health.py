"""Notice a blocked or available hub update and say how to resolve it.

A deployment that follows the hub's latest selections has nothing to notice. A deployment that records its selections in a lock
file (``esoi.lock.json``, a decision for its owner) is told when the hub would now select something else: ``lock_plan`` asks
esoi for the plan (``esoi.lock.status/1``), ``render_note`` turns it into text an AI agent or a person can act on, with the
remedy steps in order, and ``noted_before`` keeps the same plan from being posted twice. The loop an agent follows is in
``docs/how-to/keep-quest-ai-runner-current-with-the-hub.md`` and in esoi's own how-to. Nothing here prints a variable's value.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional, Tuple

PACKAGE = "quest_ai_runner"
DEFAULT_LOCK = "esoi.lock.json"
HOW_TO = "docs/how-to/keep-quest-ai-runner-current-with-the-hub.md"
EXIT_CURRENT, EXIT_ERROR, EXIT_SAFE, EXIT_BLOCKED = 0, 2, 3, 4   # esoi lock --status


def lock_plan(path: str = DEFAULT_LOCK, assembler=None) -> Optional[Tuple[int, Dict[str, Any]]]:
    """``(exit code, plan)`` from esoi's ``lock --status``, or None when there is no lock file (the deployment tracks the latest
    selections, so there is nothing to compare). An unreadable lock or a failure is ``(2, {"error": ...})``."""
    if not os.path.isfile(path):
        return None
    try:
        from esoi import lock as esoi_lock
        from . import hub
        return esoi_lock.status(assembler or hub.child(), path, package=PACKAGE, file=path)
    except Exception as error:  # noqa: BLE001 (a failed status is reported, never raised into a start)
        return EXIT_ERROR, {"error": str(error)}


def render_note(code: int, plan: Dict[str, Any]) -> str:
    """Plain text for a task or quest note: what would change, what blocks it, and the steps in order."""
    if code == EXIT_ERROR:
        return f"The hub lock could not be read: {plan.get('error', 'unknown error')}. Run `esoi lock --package {PACKAGE} --status`."
    if code == EXIT_CURRENT:
        return "The hub lock is current: the latest selections equal the locked ones."
    lines = [f"The hub has newer selections than this deployment's lock ({plan.get('state', 'updates available')}). "
             f"How to resolve: {HOW_TO}."]
    for update in plan.get("updates", []):
        head = f"- {update['solution']}: {update.get('old_arm')} -> {update.get('new_arm')}"
        if update.get("evidence"):
            head += f" ({update['evidence']})"
        lines.append(head + (" [BLOCKED]" if update.get("blocked") else " [safe]"))
        for step in update.get("steps", []):
            who = " (needs a person: do not invent a key)" if step.get("human") else ""
            command = f": `{step['command']}`" if step.get("command") else ""
            lines.append(f"    {step.get('order')}. {step.get('action')}{command} {step.get('detail', '')}{who}".rstrip())
    if plan.get("apply_safe"):
        lines.append(f"Apply every safe update at once: `{plan['apply_safe']}`")
    return "\n".join(lines)


def fingerprint(code: int, plan: Dict[str, Any]) -> str:
    """Stable id of what a plan asks for, so an unchanged plan is not noted again."""
    key = [code] + sorted((u.get("solution"), u.get("old_arm"), u.get("new_arm"), bool(u.get("blocked"))) for u in plan.get("updates", []))
    return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]


def noted_before(state_path: str, print_id: str, record: bool = True) -> bool:
    """True when ``print_id`` is already recorded at ``state_path``; otherwise records it (unless ``record`` is False)."""
    try:
        with open(state_path, encoding="utf-8") as fh:
            if json.load(fh).get("fingerprint") == print_id:
                return True
    except (OSError, ValueError):
        pass
    if record:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump({"fingerprint": print_id}, fh)
    return False
