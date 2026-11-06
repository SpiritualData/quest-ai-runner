"""The `doctor` subcommand: is the hub assembly's environment complete, and is a locked deployment behind the hub?

Split out of ``cli.py`` by definition; that module re-exports ``_cmd_doctor``. Exit code: 1 when something required is unmet or the installed console script is stale
(esoi's convention for ``esoi requirements --check``), else the lock status when a lock file exists (0 current, 3 safe updates,
4 blocked, 2 error), else 0. It reads names only, never a value, and calls no model.
"""
from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

from quest_ai_runner.the_self.assembler import hub_lock_health, hub_requirements

CONSOLE_SCRIPT = "quest-ai-runner"
SCRIPT_IMPORT = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+(\w+)", re.MULTILINE)


def module_exists(name: str) -> bool:
    """True when ``name`` can be found without importing it (a missing parent package counts as missing)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def installed_checkout(dist) -> str:
    """The editable checkout the distribution points at, or a placeholder when it is not an editable install."""
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        direct = {}
    url = direct.get("url", "")
    return url[len("file://"):] if url.startswith("file://") else "<checkout>"


def stale_console_script_problems(script_dir: Path | None = None) -> list[str]:
    """Problems with the installed ``quest-ai-runner`` command, each ending with the fix; empty when it is healthy.

    Two failures crash-loop a lane after a release moves a module: the generated entry script imports a module that no
    longer exists, or the ``console_scripts`` entry point no longer resolves. Both are cured by reinstalling editable
    with ``--no-deps`` into the same environment. Reads files only, imports nothing from the entry script.
    """
    try:
        dist = importlib.metadata.distribution("quest-ai-runner")
    except importlib.metadata.PackageNotFoundError:
        return []
    fix = f"fix: {sys.executable} -m pip install -e {installed_checkout(dist)} --no-deps"
    problems = []
    for entry in dist.entry_points:
        if entry.group != "console_scripts" or entry.name != CONSOLE_SCRIPT:
            continue
        module, _, attribute = entry.value.partition(":")
        if not module_exists(module):
            problems.append(f"entry point {entry.value} names a module that does not exist; {fix}")
        elif attribute and not hasattr(importlib.import_module(module), attribute.split(".")[0]):
            problems.append(f"entry point {entry.value} does not resolve; {fix}")
    found = (script_dir / CONSOLE_SCRIPT) if script_dir else shutil.which(CONSOLE_SCRIPT, path=str(Path(sys.executable).parent))
    if found and Path(found).is_file():
        for module, name in SCRIPT_IMPORT.findall(Path(found).read_text(errors="replace")):
            if not module_exists(module) or not hasattr(importlib.import_module(module), name):
                problems.append(f"installed script {found} imports {module}.{name}, which no longer exists; {fix}")
    return problems


def _cmd_doctor(args, log) -> int:
    report = hub_requirements.requirements_report()
    lock = hub_lock_health.lock_plan(getattr(args, "lock", None) or hub_lock_health.DEFAULT_LOCK)
    stale = stale_console_script_problems()
    code = report.exit_code or (1 if stale else 0) or (lock[0] if lock else 0)
    note = hub_lock_health.render_note(*lock) if lock else None
    if getattr(args, "json", False):
        print(json.dumps({**report.as_json(), "exit": code, "console_script_problems": stale, "lock": None if lock is None else {"exit": lock[0], "plan": lock[1]}}, indent=2))
    else:
        print("Hub requirements (offline, names only):")
        print("\n".join(f"  {line}" for line in report.lines()) or "  none declared")
        print("Console script: " + ("ok" if not stale else "STALE"))
        print("\n".join(f"  {line}" for line in stale))
        print("Hub lock: " + (note if note else "no lock file, following the hub's latest selections"))
    quest_id = getattr(args, "note_quest", None)
    if quest_id and lock and lock[0] in (hub_lock_health.EXIT_SAFE, hub_lock_health.EXIT_BLOCKED):
        state = getattr(args, "state", None) or "qar_hub_update_state.json"
        if hub_lock_health.noted_before(state, hub_lock_health.fingerprint(*lock)):
            log.info("hub update already noted on the quest")
        else:
            from quest_ai_runner import load_client
            load_client().add_quest_note(quest_id, note)
            log.info("hub update noted on the quest")
    return code
