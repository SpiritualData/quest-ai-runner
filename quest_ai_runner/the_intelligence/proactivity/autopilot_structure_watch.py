"""Binds the hub's structure watch to an autopilot run: one small adapter from a quest's records to the hub's digest shape. The logic (digest, caps, the best-tier call, the repeat rule, the
route of each item through Judgment Calls) stays in the hub; this module only maps and files the proposals.

A quest's goals are the hub's milestones (a goal's deadline is its target, a goal another waits on is in ``blocked_by``), its experiments and investigations map by field name, and the
quest's recent autopilot work is the runs. Every record is a person's (no review mark), so every item is a PROPOSAL through the existing confirmation path (a decision request) and no
edit is ever applied: a person's goal is never renamed or re-described, and what a running experiment holds fixed is never reopened. The watch makes no call when nothing changed since the
last run, and none once the run is at the soft share of its ceilings. Standard library plus ``hubofhubs.api``."""
import logging
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

log = logging.getLogger(__name__)


def goals_of(listing: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    return [g for group in (listing or {}).get("period_groups") or () for g in group.get("goals") or ()]


def structure_of(quest_id: str, outcome: str, goals: Sequence[Mapping[str, Any]], experiments: Sequence[Mapping[str, Any]], investigations: Sequence[Mapping[str, Any]],
                 runs: Sequence[Mapping[str, Any]], now: str) -> Dict[str, Any]:
    """The hub's structure input for one quest. No ``review`` marks are given, so the hub treats every record as a person's."""
    milestones = [{"id": str(g.get("id")), "solution": quest_id, "text": str(g.get("name") or ""), "met": bool(g.get("completed")), "target": str(g.get("deadline") or "")[:10],
                   "blocked_by": str(g.get("parent_goal_id") or "")} for g in goals]
    exps = [{"experiment_id": str(e.get("id")), "status": str(e.get("status") or "running"), "reevaluate_time": str(e.get("reevaluate_time") or "")[:10], "held_fixed": str(e.get("held_fixed") or ""),
             "links": f"solution:{quest_id}", "name": str(e.get("name") or e.get("research_question") or "")} for e in experiments]
    invs = [{"investigation_id": str(i.get("id")), "status": "open" if not i.get("closed") else "superseded", "refresh_on": str(i.get("refresh_on") or "")[:10], "links": f"solution:{quest_id}",
             "question": str(i.get("question") or "")} for i in investigations]
    return {"intent": outcome or quest_id, "tables": {"solutions": [], "implementations": [], "metrics": [], "experiments": exps, "investigations": invs, "milestones": milestones},
            "results": [], "runs": list(runs), "now": now}


def raise_proposals(client: Any, quest_id: str, items: Sequence[Mapping[str, Any]]) -> int:
    """File each item as a decision request on the quest (the existing confirmation path); returns how many were filed. Never edits a record."""
    filed = 0
    for item in items:
        summary = f"Structure needs attention ({item['severity']}): {item['what']} Why now: {item['why_now']} Risk: {item['risk']} Proposed: {item.get('step_text') or item['step']}"
        client.create_decision(summary[:900], quest_id=quest_id, default_on_silence="hold", dedupe_key=f"structure-watch:{quest_id}:{item['subject']}:{item['step']}")
        filed += 1
    return filed


def watch_quest(client: Any, quest_id: str, *, now: str, store: Any, ask: Optional[Callable[..., Any]], runs: Sequence[Mapping[str, Any]] = (), outcome: str = "",
                ceilings: Optional[Mapping[str, Any]] = None, dry_run: bool = False, off: bool = False) -> Mapping[str, Any]:
    """Run the hub's structure watch once over a quest and file its items as decision requests. Never raises; a dry run reads and files nothing."""
    from hubofhubs.api import BestTierCall, SeverityPolicy
    quiet = {"called": False, "items": [], "filed": 0, "reason": "off"}
    if dry_run or off:
        return quiet
    try:
        structure = structure_of(quest_id, outcome, goals_of(client.list_quest_goals(quest_id)), client.list_experiments(quest_id), investigations_of(client, quest_id), runs, now)
        result = dict(BestTierCall(ask=ask, store=store, judge=SeverityPolicy().decide, applier=None, ceilings=ceilings).watch(structure, now))
        result["filed"] = raise_proposals(client, quest_id, result["items"])
        return result
    except Exception as error:      # noqa: BLE001 the watch never fails a run
        log.warning("structure watch on %s did not run: %s", quest_id, error)
        return dict(quiet, reason=f"failed: {type(error).__name__}")


def investigations_of(client: Any, quest_id: str) -> List[Mapping[str, Any]]:
    return client.list_investigations(quest_id) if hasattr(client, "list_investigations") else []
