"""Test stand-ins for the backend: the quest's named autopilots as it stores them, and a pass that runs each one.

The runner reads the quest's ``autopilots`` list (every setting stored on the autopilot, nothing inherited) and executes ONE ``autopilot_run`` task per
autopilot. Older tests start from quest-level settings (``mode``, ``personas``, ``routines``), so ``stored_autopilots`` writes what the backend's
migration would store for them (one ``rep-<rep_id>`` per persona, one ``routine-<name>`` per routine, else one ``quest`` entry for a quest whose mode is
on), and ``AutopilotPass`` below plays the backend's scheduler for them: it runs every autopilot once and files a task for each run that goes ahead.
"""
from typing import Any, Dict, List

INHERITED = ("mode", "planning", "cadence", "run_time", "run_timezone", "model", "env_id", "email", "reactive_replies")


def resolve(entry: Dict[str, Any], cfg: Dict[str, Any], *, legacy: bool = False, state: Dict[str, Any] = None) -> Dict[str, Any]:
    out = dict(entry)
    inherited = []
    for field in INHERITED:
        if out.get(field) is None:
            out[field] = cfg.get(field)
            inherited.append(field)
    out.setdefault("enabled", True)
    out.setdefault("works_goals", False)
    out.setdefault("days", [])
    out["inherited"] = inherited
    out["legacy"] = legacy
    out["state"] = dict(state or {})
    return out


def effective_autopilots(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    stored = cfg.get("autopilot_state") or {}
    if cfg.get("autopilots"):
        return [resolve(e, cfg, state=stored.get(e["id"])) for e in cfg["autopilots"]]
    entries: List[Dict[str, Any]] = []
    quest_state = {k: cfg[k] for k in ("last_pass_at", "run_requested_at", "miss_streak") if cfg.get(k) is not None}
    for persona in cfg.get("personas") or []:
        rep = persona.get("rep_id")
        entry = {"id": f"rep-{rep}", "name": str(rep), "purpose": "Works the quest as this rep.",
                 "rep_id": None if rep == "default_assistant" else rep, "instructions": persona.get("instructions") or "",
                 "works_goals": not persona.get("instructions_only") or not persona.get("instructions"), "days": persona.get("days") or []}
        entries.append(resolve(entry, cfg, legacy=True, state={**quest_state, **(stored.get(entry["id"]) or {})}))
    for routine in cfg.get("routines") or []:
        entry = {"id": f"routine-{routine['name']}", "name": routine["name"], "purpose": routine.get("purpose") or "", "gate": routine.get("purpose"),
                 "instructions": routine.get("instructions") or "", "works_goals": False, "days": routine.get("days") or [],
                 "run_time": routine.get("run_time"), "run_timezone": routine.get("run_timezone"), "grace_minutes": routine.get("grace_minutes"),
                 "enabled": routine.get("enabled", True)}
        last = (cfg.get("routine_runs") or {}).get(routine["name"])
        entries.append(resolve(entry, cfg, legacy=True, state={**({"last_pass_at": last} if last else {}), **(stored.get(entry["id"]) or {})}))
    if not entries and str(cfg.get("mode") or "off") != "off":
        entries.append(resolve({"id": "quest", "name": "Autopilot", "purpose": "Works the quest.", "works_goals": True}, cfg, legacy=True,
                               state={**quest_state, **(stored.get("quest") or {})}))
    return entries


def single_entry(cfg: Dict[str, Any], zone: str = None) -> Dict[str, Any]:
    """The one plain-assistant entry a quest-level settings dict stands for (its bookkeeping read from the old quest-level fields)."""
    shared = {"mode": "act", **cfg, **({"run_timezone": zone} if zone else {})}
    state = {k: cfg[k] for k in ("last_pass_at", "run_requested_at") if cfg.get(k)}
    return resolve({"id": "quest", "name": "Autopilot", "works_goals": True}, shared, legacy=True, state=state)


def due_trigger(cfg: Dict[str, Any], now=None, zone: str = None, pending: int = 0, honour_run_request: bool = True) -> str:
    """The kind of trigger that makes the quest's one autopilot due (the question ``autopilot_units.unit_trigger`` asks the hub)."""
    from quest_ai_runner.intelligence.proactivity.autopilot_units import unit_trigger
    return unit_trigger(single_entry(cfg, zone), now, pending=pending, honour_run_request=honour_run_request)


def run_pending(cfg: Dict[str, Any]) -> bool:
    from quest_ai_runner.intelligence.proactivity.autopilot_units import run_pending as pending
    return pending(single_entry(cfg))


def stored_autopilots(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """What the migration stores for the older settings: each entry carries its own mode (``reactive`` becomes ``suggest``), and ``off`` is no autopilot."""
    out = []
    for e in effective_autopilots(cfg):
        mode = str(e.get("mode") or "off")
        if mode == "off":
            continue
        e = {k: v for k, v in e.items() if k not in ("inherited", "legacy", "state", "works_goals", "gate", "email")}
        e["mode"] = "act" if mode == "act" else "suggest"
        e["instructions"] = e.get("instructions") or "Work the quest."      # an autopilot with no prompt and no routed goals is skipped, so older tests give it one
        if e.get("purpose") and e["id"].startswith("routine-"):
            e["source"] = e.pop("purpose")
        out.append(e)
    return out


from quest_ai_runner.intelligence.proactivity.autopilot import AutopilotPass as RealPass  # noqa: E402


class AutopilotPass(RealPass):
    """The real pass plus the backend's scheduler: ``run`` plans one run per autopilot of the quest the task names (every quest when it names none)
    and files each run that goes ahead as a task on the client, so a test can read what a day's runs would have worked on."""

    def run(self, task):
        from types import SimpleNamespace
        result = SimpleNamespace(created=[], skipped=[], errors=[], proposals=[], bookkeeping_warnings=[], plans=[], created_task_ids=[],
                                 summary_text=lambda: "")
        quest_ids = [str(task["goal_id"])] if task.get("goal_id") else [str(q["quest_id"]) for q in self._client.quests]
        for quest_id in quest_ids:
            cfg = (self._client.get_quest_autopilot(quest_id) or {}).get("autopilot") or {}
            for unit in cfg.get("autopilots") or []:
                plan = super().run({**task, "goal_id": quest_id, "task_kind": "autopilot_run", "autopilot_id": unit["id"]})
                result.plans.append(plan)
                label = {"quest_id": quest_id, "quest_label": "ship the thing", "autopilot": unit["id"], "autopilot_name": plan.title}
                if plan.status == "skipped":
                    result.skipped.append({**label, "reason": plan.reason})
                    continue
                kwargs = {"goal_id": quest_id, "title": plan.title, "status": "queued" if plan.status == "act" else "suggested", "task_kind": "autopilot_run",
                          "autopilot_id": unit["id"]}
                if unit.get("rep_id"):
                    kwargs["assignee_rep_id"] = unit["rep_id"]
                made = self._client.create_task(plan.text, **kwargs)
                result.created_task_ids.append(made["id"])
                result.created.append({**label, "task_id": made["id"], "title": plan.title})
        return result
