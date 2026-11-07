"""Alternatives written for a quest are kept by the quest, keyed, and the reply says which already were (a step of ESOI Application's ensure flow).

Role: a request for content with more than one version (two posts, three subject lines) is a deliverable AND something the quest tracks: each alternative is one goal, linked to the
plan solution it serves, with its hypothesis and the metric they are compared by. The model writes the versions; this module reads them back out of the reply with one cheap
structured call that sees the quest's existing goals (so an alternative already tracked there is the SAME goal, adopted by its id and updated in place, never duplicated, and a new one
is titled in the style the quest already uses), then the keyed idempotent goal tool writes each, and the receipts compose a plain tail: which were already tracked, which were
created, and what they are compared by. Nothing is decided from the words of the reply: the extraction is JSON checked against the ids it was offered; the states are the tool's own.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue

log = logging.getLogger("quest-ai-runner.alternatives")
STATE_WORDS = {"created": "created", "updated": "already tracked, copy updated", "unchanged": "already tracked"}


@dataclass
class Alternative:
    title: str
    text: str
    hypothesis: str = ""
    goal_id: str = ""
    solution_id: str = ""
    metric_id: str = ""


MIN_SHARED_WORDS = 2


def goals_for_cue(goals: Sequence[Mapping[str, Any]], cue: str) -> List[Mapping[str, Any]]:
    """The open goals the quest already holds that overlap the request (at least ``MIN_SHARED_WORDS`` of its meaningful words occur in the goal's title and text): the only goals an
    alternative may be adopted into. Model-free, by the same word weighting the milestone cue uses."""
    wanted = set(milestone_cue.words(cue))
    out = []
    for g in goals:
        if g.get("completed") or g.get("abandoned"):
            continue
        if len(wanted & set(milestone_cue.words(f"{g.get('title') or ''} {g.get('text') or ''}"))) >= MIN_SHARED_WORDS:
            out.append(g)
    return out


def solutions_listing(tables: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> List[Dict[str, str]]:
    """The quest's solutions that have a metric, as ``{solution_id, name, metric_id, metric_name}`` (what an alternative may be linked to)."""
    out = []
    for rid, row in (tables.get("solutions") or {}).items():
        for mid, metric in milestone_cue.metrics_of(tables, rid):
            out.append({"solution_id": rid, "name": str(row.get("name") or ""), "metric_id": mid, "metric_name": str(metric.get("name") or "")})
    return out


def build_prompt(cue: str, reply: str, goals: Sequence[Mapping[str, Any]], solutions: Sequence[Mapping[str, str]]) -> str:
    goal_lines = "\n".join(f"{g.get('id')} | {g.get('title')} | solution {g.get('spec_solution_id') or 'none'}" for g in goals) or "(none)"
    solution_lines = "\n".join(f"{s['solution_id']} | {s['name']} | metric {s['metric_id']} | {s['metric_name']}" for s in solutions) or "(none)"
    return ("A person asked for content in their quest's chat:\n" + str(cue).strip()[:800] + "\n\nThe reply written for them:\n" + str(reply).strip()[:5000]
            + "\n\nGoals the quest ALREADY holds for this (id | title | linked solution):\n" + goal_lines + "\n\nThe quest's solutions (solution id | name | metric id | metric name):\n" + solution_lines
            + "\n\nIf the reply holds two or more alternatives of one thing meant to be compared (versions of a post, a headline, a message), return JSON "
              '{"alternatives":[{"title":"...","goal_id":"...","starts_with":"the first six words of the alternative exactly as the reply writes them","hypothesis":"one line","solution_id":"...","metric_id":"..."}]} with one entry per alternative, in the order they appear in the reply. '
              "Adopting an existing goal ALWAYS wins over a new title: when a goal above tracks that alternative, or is the next free one of the same series (A, B, C), give its goal_id and keep its title "
              "exactly. Use a new title only when no goal above can hold the alternative, and then continue the series and style of the titles above. Choose solution_id and metric_id from the list above (the solution these alternatives serve), or leave them empty. "
              'Write no em dashes. If the reply does not hold alternatives to compare, return {"alternatives":[]}. Reply with the JSON only.')


def segment(reply: str, starts: Sequence[str]) -> List[str]:
    """Each alternative's text, cut out of the reply: from where its first words occur to where the next one's begin (the last to the end of the reply, at most 1500 characters).
    "" for one whose first words are not found. The model names only where each begins, so its answer stays short and the text is the reply's own."""
    low = reply.casefold()
    positions, cursor = [], 0
    for start in starts:
        at = low.find(" ".join(str(start).split()).casefold(), cursor) if str(start).strip() else -1
        positions.append(at)
        cursor = at + 1 if at >= 0 else cursor
    out = []
    for i, at in enumerate(positions):
        if at < 0:
            out.append("")
            continue
        following = next((p for p in positions[i + 1:] if p > at), len(reply))
        out.append(reply[at:following].strip()[:1500])
    return out


def parse(answer: str, goal_ids: Sequence[str], solutions: Sequence[Mapping[str, str]], reply: str = "") -> List[Alternative]:
    """The alternatives in the model's JSON, with every id checked against what it was offered (an id it was not offered is dropped, never trusted). The text of each is cut from the reply
    where its first words begin (``segment``), else the text the model gave."""
    text = str(answer or "")
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except ValueError:
        return []
    items = [i for i in ((data.get("alternatives") or []) if isinstance(data, dict) else []) if isinstance(i, dict)]
    cut = segment(reply, [i.get("starts_with") or "" for i in items])
    valid = {(s["solution_id"], s["metric_id"]) for s in solutions}
    solution_ids = {s["solution_id"] for s in solutions}
    out: List[Alternative] = []
    for item, body in zip(items, cut):
        body = body or str(item.get("text") or "").strip()
        if not str(item.get("title") or "").strip() or not body:
            continue
        solution, metric = str(item.get("solution_id") or ""), str(item.get("metric_id") or "")
        if solution not in solution_ids:
            solution, metric = "", ""
        elif metric and (solution, metric) not in valid:
            metric = ""
        goal = str(item.get("goal_id") or "")
        out.append(Alternative(str(item["title"]).strip(), body, str(item.get("hypothesis") or "").strip(), goal if goal in goal_ids else "", solution, metric))
    return out if len(out) >= 2 else []


def goal_arguments(quest_id: str, alt: Alternative, metric_name: str) -> Dict[str, Any]:
    description = alt.text + (f"\nHypothesis: {alt.hypothesis}" if alt.hypothesis else "") + (f"\nCompared by: {metric_name}" if metric_name else "")
    args: Dict[str, Any] = {"quest_id": quest_id, "title": alt.title, "description": description, "user_asked_for_this_field": True}
    if alt.solution_id:
        args["spec_solution_id"] = alt.solution_id
    if alt.metric_id:
        args["spec_metric_id"] = alt.metric_id
    return args          # an existing goal is reached only through its exact title (see ``run``); no goal is ever taken over by id


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
    goals = goals_for_cue(milestone_cue.goals_through(registry, quest_id), cue)
    solutions = solutions_listing(tables)
    answer = decide(build_prompt(cue, reply, goals, solutions))
    alts = parse(answer, {str(g.get("id")) for g in goals}, solutions, reply)
    if not alts:
        log.info("alternatives flow: no alternatives to keep in the reply (model answer: %r)", str(answer)[:300])
        return ""
    by_title = {str(g.get("title") or "").strip().casefold(): str(g.get("id")) for g in goals}
    taken = {a.goal_id for a in alts if a.goal_id}
    titles = {str(g.get("id")): str(g.get("title") or "").strip() for g in goals}
    for alt in alts:
        same = by_title.get(alt.title.strip().casefold(), "")
        if not alt.goal_id and same and same not in taken:      # a title that is an existing goal's title is that goal, whatever id the model gave
            alt.goal_id = same
            taken.add(same)
        if alt.goal_id:
            alt.title = titles.get(alt.goal_id) or alt.title        # the keyed goal is found by its exact title: renaming is never the way in
    from quest_ai_runner.the_world.tools.tools import ToolContext
    names = {(s["solution_id"], s["metric_id"]): s["metric_name"] for s in solutions}
    results = []
    for alt in alts:
        result = registry.invoke("create_or_update_quest_goal", goal_arguments(quest_id, alt, names.get((alt.solution_id, alt.metric_id), "")), ToolContext(quest_id=quest_id))
        if not result.ok:
            raise milestone_cue.LaneGap(f"{alt.title!r} could not be recorded: {str(result.text).strip()[:300]}")
        results.append({"title": alt.title, "hypothesis": alt.hypothesis, "state": str((result.data or {}).get("state") or "created")})
    metric = next((names[(a.solution_id, a.metric_id)] for a in alts if (a.solution_id, a.metric_id) in names), "")
    return compose_tail(results, metric)


def status_line(registry: Any, quest_id: str, cue: str) -> str:
    """What the quest already holds for this request, in plain words, for message 1: the goals that overlap it (titles), and, when they are linked to a plan solution, the solution and the
    metric it moves. "" when the quest holds none or cannot be read."""
    held = goals_for_cue(milestone_cue.goals_through(registry, quest_id), cue)
    if not held:
        return ""
    titles = ", ".join(str(g.get("title")) for g in held[:3])
    n = len(held)
    line = f"The quest already tracks {n} goal{'s' if n != 1 else ''} like this ({titles}{', and more' if n > 3 else ''})"
    linked = [str(g["spec_solution_id"]) for g in held if g.get("spec_solution_id")]
    if linked:
        tables, _ = milestone_cue.read_through(registry, quest_id)
        solution = max(set(linked), key=linked.count)
        name = str(((tables.get("solutions") or {}).get(solution) or {}).get("name") or "")
        metric = next((str(m.get("name") or "") for _, m in milestone_cue.metrics_of(tables, solution)), "")
        if name:
            line += f", under {name}" + (f", which moves {metric}" if metric else "")
    return line + "."
