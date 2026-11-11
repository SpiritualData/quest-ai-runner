"""Binds the hub's structure watch to an autopilot run: one small adapter from a quest's records to the hub's digest shape. The logic (digest, caps, the best-tier call, the repeat rule, the
route of each item through Judgment Calls) stays in the hub; this module only maps and files the proposals.

A quest's goals are the hub's milestones (a goal's deadline is its target, a goal another waits on is in ``blocked_by``), its experiments and investigations map by field name, and the
quest's recent autopilot work is the runs. Every record is a person's (no review mark), so every item is a PROPOSAL through the existing confirmation path (a decision request) and no
edit is ever applied: a person's goal is never renamed or re-described, and what a running experiment holds fixed is never reopened. The watch makes no call when nothing changed since the
last run, and none once the run is at the soft share of its ceilings. Standard library plus ``hubofhubs.api``."""
import logging
from pathlib import Path
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
    owner = quest_owner(client, quest_id) if items else ""
    if items and not owner:
        log.info("structure watch on %s: %d item(s) not filed, the quest names no owner who is a person to ask", quest_id, len(items))
        return 0
    for item in items:
        summary = f"Structure needs attention ({item['severity']}): {item['what']} Why now: {item['why_now']} Risk: {item['risk']} Proposed: {item.get('step_text') or item['step']}"
        client.create_decision(summary[:900], quest_id=quest_id, assignee_user_id=owner, default_on_silence="hold",
                               dedupe_key=f"structure-watch:{quest_id}:{item['subject']}:{item['step']}")
        filed += 1
    return filed


def quest_owner(client: Any, quest_id: str) -> str:
    """The quest's first owner who is not this runner's own account, "" when none can be read. Quest refuses an ask with nobody to answer it, and an ask to the runner's own
    account reaches no person, so a proposal goes to the person who owns the quest or is not filed."""
    try:
        owners = [str(o) for o in ((client.get_quest(quest_id) or {}).get("owner_user_ids") or []) if o]
        own = str(client.own_user_id() or "") if hasattr(client, "own_user_id") else ""
    except Exception:      # noqa: BLE001 an unreadable quest means no owner is known
        return ""
    return next((o for o in owners if o != own), "")


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


def provider_ask(provider: Any, call: str = "structure-watch") -> Callable[..., Mapping[str, Any]]:
    """The hub's ``ask(prompt, tier, cap)`` over the lane's own model provider: the model is the tier of the call's role in the hub's table (never a named model), and the reply carries
    the tokens and cost this one call used, read from the provider's per-thread counters (an absent counter reads as 0, and the hub then estimates from the text)."""
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

    def ask(prompt: str, tier: str, cap: int) -> Mapping[str, Any]:
        before = [float(getattr(provider, name, 0) or 0) for name in ("tokens_in", "tokens_out", "cost_usd")]
        registry = ModelRegistry(provider)
        model = registry.resolve_role(call)
        if tier == "best" and "best" not in registry.selected() and "best" not in registry._user_overrides:
            raise RuntimeError(f"no model is chosen for the best tier (the hub's public choice names none for this lane's providers, and none is pinned), so the structure watch does not run on {model}")
        text = provider.answer([{"role": "user", "content": prompt}], model=model) or ""
        after = [float(getattr(provider, name, 0) or 0) for name in ("tokens_in", "tokens_out", "cost_usd")]
        used = [max(a - b, 0.0) for a, b in zip(after, before)]
        log.info("structure watch call: tier %s, %d tokens in, %d out, cost %.4f", tier, used[0], used[1], used[2])
        return {"text": text, "tokens_in": int(used[0]), "tokens_out": int(used[1]), "cost_usd": used[2], "model": model}
    return ask


def build_structure_watch(client: Any, provider: Any, folder: str, *, enabled: bool = True, now: Optional[Callable[[], Any]] = None) -> Optional[Callable[[str, bool], Any]]:
    """The ``watch(quest_id, dry_run)`` an autopilot or routine pass is given, or None when the lane switched it off or holds no model provider. Each quest keeps its own record of the last
    watch under ``folder`` (so a quest whose records did not change since its last run makes no call); the watch never raises."""
    if not enabled or provider is None or client is None:
        return None
    from hubofhubs.api import RunStore
    from datetime import datetime, timezone
    ask, stores = provider_ask(provider), {}

    def watch(quest_id: str, dry_run: bool = False) -> Any:
        if dry_run:
            return None
        try:
            if quest_id not in stores:
                stores[quest_id] = RunStore(str(Path(folder) / quest_id) if folder else "")
            stamp = (now() if now else datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return watch_quest(client, quest_id, now=stamp, store=stores[quest_id], ask=ask)
        except Exception as error:      # noqa: BLE001 the watch never fails a run
            log.warning("structure watch on %s did not run: %s", quest_id, error)
            return None
    return watch
