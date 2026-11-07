"""Alternatives written for a quest are kept by the quest, keyed, and the reply says which already were (a step of ESOI Application's ensure flow).

Role: a request for content with more than one version (two posts, three subject lines) is a deliverable AND something the quest tracks: each alternative is one goal, linked to the
plan solution it serves, with its hypothesis and the metric they are compared by. The model writes the versions; this module reads them back out of the reply with one cheap
structured call that sees the quest's existing goals (so an alternative already tracked there is the SAME goal, adopted by its id and updated in place, never duplicated, and a new one
is titled in the style the quest already uses), then the keyed idempotent goal tool writes each, and the receipts compose a plain tail: which were already tracked, which were
created, and what they are compared by. Nothing is decided from the words of the reply: the extraction is JSON checked against the ids it was offered; the states are the tool's own.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue

STATE_WORDS = {"created": "created", "updated": "already tracked, copy updated", "unchanged": "already tracked"}


@dataclass
class Alternative:
    title: str
    text: str
    hypothesis: str = ""
    goal_id: str = ""
    solution_id: str = ""
    metric_id: str = ""


def solutions_listing(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> List[Dict[str, str]]:
    """The quest's solutions that have a metric, as ``{solution_id, name, metric_id, metric_name}`` (what an alternative may be linked to)."""
    out = []
    for rid, row in (tables.get("solutions") or {}).items():
        for mid, metric in milestone_cue.metrics_of(tables, rid):
            out.append({"solution_id": rid, "name": str(row.get("name") or ""), "metric_id": mid, "metric_name": str(metric.get("name") or "")})
    return out


def build_prompt(cue: str, reply: str, goals: Sequence[Mapping[str, Any]], solutions: Sequence[Mapping[str, str]]) -> str:
    goal_lines = "\n".join(f"{g.get('id')} | {g.get('title')} | solution {g.get('spec_solution_id') or 'none'}" for g in goals if not g.get("completed") and not g.get("abandoned")) or "(none)"
    solution_lines = "\n".join(f"{s['solution_id']} | {s['name']} | metric {s['metric_id']} | {s['metric_name']}" for s in solutions) or "(none)"
    return ("A person asked for content in their quest's chat:\n" + str(cue).strip()[:800] + "\n\nThe reply written for them:\n" + str(reply).strip()[:5000]
            + "\n\nThe quest's open goals (id | title | linked solution):\n" + goal_lines + "\n\nThe quest's solutions (solution id | name | metric id | metric name):\n" + solution_lines
            + "\n\nIf the reply holds two or more alternatives of one thing meant to be compared (versions of a post, a headline, a message), return JSON "
              '{"alternatives":[{"title":"...","goal_id":"...","text":"the alternative in full","hypothesis":"one line","solution_id":"...","metric_id":"..."}]} with one entry per alternative. '
              "When an open goal above already tracks that alternative, give its goal_id and keep its title exactly. Otherwise leave goal_id empty and give a short stable title in the style of the "
              "quest's existing tracking goals, ending in A, B, C. Choose solution_id and metric_id from the list above (the solution these alternatives serve), or leave them empty. "
              'Write no em dashes. If the reply does not hold alternatives to compare, return {"alternatives":[]}. Reply with the JSON only.')


def parse(answer: str, goal_ids: Sequence[str], solutions: Sequence[Mapping[str, str]]) -> List[Alternative]:
    """The alternatives in the model's JSON, with every id checked against what it was offered (an id it was not offered is dropped, never trusted)."""
    text = str(answer or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except ValueError:
        return []
    valid = {(s["solution_id"], s["metric_id"]) for s in solutions}
    solution_ids = {s["solution_id"] for s in solutions}
    out: List[Alternative] = []
    for item in (data.get("alternatives") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip() or not str(item.get("text") or "").strip():
            continue
        solution, metric = str(item.get("solution_id") or ""), str(item.get("metric_id") or "")
        if solution not in solution_ids:
            solution, metric = "", ""
        elif metric and (solution, metric) not in valid:
            metric = ""
        goal = str(item.get("goal_id") or "")
        out.append(Alternative(str(item["title"]).strip(), str(item["text"]).strip(), str(item.get("hypothesis") or "").strip(), goal if goal in goal_ids else "", solution, metric))
    return out if len(out) >= 2 else []


def goal_arguments(quest_id: str, alt: Alternative, metric_name: str) -> Dict[str, Any]:
    description = alt.text + (f"\nHypothesis: {alt.hypothesis}" if alt.hypothesis else "") + (f"\nCompared by: {metric_name}" if metric_name else "")
    args: Dict[str, Any] = {"quest_id": quest_id, "title": alt.title, "description": description, "user_asked_for_this_field": True}
    if alt.solution_id:
        args["spec_solution_id"] = alt.solution_id
    if alt.metric_id:
        args["spec_metric_id"] = alt.metric_id
    if alt.goal_id:
        args["adopt_goal_id"] = alt.goal_id
    return args


def compose_tail(results: Sequence[Mapping[str, Any]], metric_name: str) -> str:
    """The plain tail: one line per alternative saying whether the quest already tracked it, and what they are compared by. No ids."""
    lines = ["On the quest:"]
    for r in results:
        lines.append(f"- {r['title']} ({STATE_WORDS.get(r['state'], r['state'])})" + (f": {r['hypothesis']}" if r.get("hypothesis") else ""))
    if metric_name:
        lines.append(f"Compared by: {metric_name}.")
    return "\n".join(lines)


def run(registry: Any, quest_id: str, cue: str, reply: str, decide: Callable[[str], str]) -> str:
    """The tail to add to the reply, or "" when the reply holds no alternatives to compare. Raises ``milestone_cue.LaneGap`` when the quest's goals or records cannot be read or a write fails."""
    if registry is None or not quest_id or registry.get("create_or_update_quest_goal") is None:
        return ""
    tables, _ = milestone_cue.read_through(registry, quest_id)
    goals = milestone_cue.goals_through(registry, quest_id)
    solutions = solutions_listing(tables)
    alts = parse(decide(build_prompt(cue, reply, goals, solutions)), {str(g.get("id")) for g in goals}, solutions)
    if not alts:
        return ""
    from quest_ai_runner.the_world.tools.tools import ToolContext
    names = {(s["solution_id"], s["metric_id"]): s["metric_name"] for s in solutions}
    results = []
    for alt in alts:
        result = registry.invoke("create_or_update_quest_goal", goal_arguments(quest_id, alt, names.get((alt.solution_id, alt.metric_id), "")), ToolContext(quest_id=quest_id))
        if not result.ok:
            raise milestone_cue.LaneGap(f"{alt.title!r} could not be recorded: {str(result.text).strip()[:200]}")
        results.append({"title": alt.title, "hypothesis": alt.hypothesis, "state": str((result.data or {}).get("state") or "created")})
    metric = next((names[(a.solution_id, a.metric_id)] for a in alts if (a.solution_id, a.metric_id) in names), "")
    return compose_tail(results, metric)


def status_line(registry: Any, quest_id: str) -> str:
    """What the quest already holds for this kind of request, in plain words, for message 1: how many open goals sit under the plan solution most goals are linked to, and the metric
    it moves ("The quest already tracks 3 open goals under Acquire paying subscribers, which moves Paid subscribers."). "" when the quest has no linked goals or cannot be read."""
    tables, _ = milestone_cue.read_through(registry, quest_id)
    goals = [g for g in milestone_cue.goals_through(registry, quest_id) if not g.get("completed") and not g.get("abandoned") and g.get("spec_solution_id")]
    if not goals:
        return ""
    counts: Dict[str, int] = {}
    for g in goals:
        counts[str(g["spec_solution_id"])] = counts.get(str(g["spec_solution_id"]), 0) + 1
    solution_id = max(counts, key=lambda k: counts[k])
    name = str(((tables.get("solutions") or {}).get(solution_id) or {}).get("name") or "")
    metric = next((str(m.get("name") or "") for _, m in milestone_cue.metrics_of(tables, solution_id)), "")
    if not name:
        return ""
    n = counts[solution_id]
    return f"The quest already tracks {n} open goal{'s' if n != 1 else ''} under {name}" + (f", which moves {metric}" if metric else "") + "."
