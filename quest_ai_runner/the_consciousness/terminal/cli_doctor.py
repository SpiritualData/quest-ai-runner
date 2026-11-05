"""The `doctor` subcommand: is the hub assembly's environment complete, and is a locked deployment behind the hub?

Split out of ``cli.py`` by definition; that module re-exports ``_cmd_doctor``. Exit code: 1 when something required is unmet
(esoi's convention for ``esoi requirements --check``), else the lock status when a lock file exists (0 current, 3 safe updates,
4 blocked, 2 error), else 0. It reads names only, never a value, and calls no model.
"""
from __future__ import annotations

import json

from quest_ai_runner.the_self.assembler import hub_lock_health, hub_requirements


def _cmd_doctor(args, log) -> int:
    report = hub_requirements.requirements_report()
    lock = hub_lock_health.lock_plan(getattr(args, "lock", None) or hub_lock_health.DEFAULT_LOCK)
    code = report.exit_code or (lock[0] if lock else 0)
    note = hub_lock_health.render_note(*lock) if lock else None
    if getattr(args, "json", False):
        print(json.dumps({**report.as_json(), "exit": code, "lock": None if lock is None else {"exit": lock[0], "plan": lock[1]}}, indent=2))
    else:
        print("Hub requirements (offline, names only):")
        print("\n".join(f"  {line}" for line in report.lines()) or "  none declared")
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
