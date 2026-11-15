"""Which autopilot a goal belongs to today: one judgment per goal, across the autopilots on duty.

Goals follow scope. Every autopilot has a scope (its purpose sentence and its prompt) and works the quest's goals that fit it. A goal belongs to ONE
autopilot a day (no double work); a goal the judge assigns to nobody is left alone; a goal the person's plan handed to a rep (``ai_handling == "rep"``)
stays with an on-duty autopilot of that rep, with no judgment. Eligible goals are the current period's incomplete ones that the person marked "AI
helps with this goal".

Words only RANK the candidates; the decision is a fast-tier LLM judgment (``judge``: prompt text to text, built from the lane's model provider by
``make_judge``, never a hardcoded model), and a failed or unusable judgment leaves the goals unassigned rather than guessing. Never hand the model a
whole collection: the eligible goals are bounded to ``MAX_GOALS_JUDGED`` per quest per day, soonest deadline first.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from quest_ai_runner.intelligence.proactivity.autopilot_defaults import _goal_name
from quest_ai_runner.intelligence.proactivity.autopilot_periods import _current_period_groups, _ordered_goals
from quest_ai_runner.intelligence.proactivity.autopilot_units import label_of, scope_of
from quest_ai_runner.self.integrity.prompts.prompt_specs import PromptSpec, register, render_prompt

log = logging.getLogger("quest-ai-runner.autopilot")

MAX_GOALS_JUDGED = 25

ROUTING_PROMPT = """You are routing a quest's goals to its AI autopilots for today. Each autopilot has a scope: what it is for and the standing prompt it works to.

Autopilots on duty today (choose only from these ids):
{autopilots}

Goals the person marked for AI help (choose for each one):
{goals}

Rules:
- A goal goes to the ONE autopilot whose scope it fits best, judged by what the goal needs done, not by shared words. Two autopilots never work the same goal.
- If no autopilot's scope covers the goal, assign it to none: it is left alone for the person. Do not stretch a scope to fit.
- Give a one-line reason for each assignment, without em dashes.

Answer with one JSON object only: {{"assignments": [{{"goal": "<goal id>", "autopilot": "<autopilot id or null>", "reason": "<short>"}}]}}"""

ROUTING_SPEC = register(PromptSpec(
    id="quest-autopilot.route-goals", solution="quest-autopilot", metric="metric-quest-autopilot-goal-routing",
    tier="fast", cache_layer="none", template=ROUTING_PROMPT, inputs=("autopilots", "goals"),
    output_schema={"assignments": "list of {goal, autopilot or null, reason}"},
))


def eligible_goals(goals_payload: Mapping[str, Any], now: datetime) -> List[Dict[str, Any]]:
    """The current period's incomplete goals the person marked for AI help, soonest deadline first, bounded."""
    seen: set = set()
    found: List[Dict[str, Any]] = []
    for scope, _period, goals in _current_period_groups(dict(goals_payload), now):
        for g in _ordered_goals([g for g in goals if not g.get("completed") and (g.get("ai_help") or g.get("ai_handling") == "rep")], scope, now):
            key = str(g.get("id") or "")
            if key and key not in seen:
                seen.add(key)
                found.append(g)
    return found[:MAX_GOALS_JUDGED]


def goal_line(goal: Mapping[str, Any]) -> str:
    deadline = str(goal.get("deadline") or "").strip()
    return _goal_name(dict(goal)) + (f" (due {deadline})" if deadline else "")


def _parse(raw: str) -> List[Dict[str, Any]]:
    found = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not found:
        return []
    try:
        data = json.loads(found.group(0))
    except (ValueError, TypeError):
        return []
    rows = data.get("assignments") if isinstance(data, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def route_goals(goals_payload: Mapping[str, Any], on_duty: Sequence[Mapping[str, Any]], judge: Optional[Callable[[str], str]],
                now: datetime) -> List[Dict[str, Any]]:
    """One row per eligible goal: ``{goal_id, goal_title, autopilot_id (or None), reason, line}``. A goal whose ``autopilot_id`` is None is left alone.

    ``on_duty`` is the autopilots working today in list order, which only breaks ties (a goal a rep holds goes to the first on-duty autopilot of that rep).
    Every eligible goal appears once, so the day's record shows the goals left alone as well as the ones given out. A failed or missing judgment gives the
    goals the judge would have placed to nobody."""
    goals = eligible_goals(goals_payload, now)
    if not goals:
        return []
    ids = {str(u["id"]) for u in on_duty}
    rows: Dict[str, Dict[str, Any]] = {
        str(g["id"]): {"goal_id": str(g["id"]), "goal_title": _goal_name(dict(g)), "autopilot_id": None, "reason": "", "line": goal_line(g)} for g in goals}
    open_goals: List[Dict[str, Any]] = []
    for g in goals:
        rep = str(g.get("ai_handling_rep_id") or "")
        if g.get("ai_handling") == "rep":
            mine = next((u for u in on_duty if rep and str(u.get("rep_id") or "") == rep), None)
            rows[str(g["id"])]["autopilot_id"] = str(mine["id"]) if mine is not None else None
            rows[str(g["id"])]["reason"] = "held by the rep" if mine is not None else "held by a rep with no autopilot on duty today"
        else:
            open_goals.append(g)
    if not open_goals or not on_duty or judge is None:
        return list(rows.values())
    listing = "\n".join(f"- {u['id']}: {label_of(u)}. {scope_of(u)[:600]}" for u in on_duty)
    goal_rows = "\n".join(f"- {g['id']}: {goal_line(g)}" for g in open_goals)
    try:
        answered = _parse(judge(render_prompt(ROUTING_SPEC.id, autopilots=listing, goals=goal_rows)))
    except Exception:  # noqa: BLE001 -- a failed judgment leaves the goals unassigned, never guessed
        log.info("autopilot: goal routing judgment failed; leaving the goals unassigned", exc_info=True)
        return list(rows.values())
    judged = {str(g["id"]) for g in open_goals}
    for row in answered:
        goal_id, chosen = str(row.get("goal") or ""), row.get("autopilot")
        if goal_id in judged and rows[goal_id]["autopilot_id"] is None and chosen not in (None, "", "null") and str(chosen) in ids:
            rows[goal_id]["autopilot_id"] = str(chosen)
            rows[goal_id]["reason"] = str(row.get("reason") or "")[:300]
            log.info("autopilot: goal %s routed to %s (%s)", goal_id, chosen, rows[goal_id]["reason"][:160])
    return list(rows.values())
