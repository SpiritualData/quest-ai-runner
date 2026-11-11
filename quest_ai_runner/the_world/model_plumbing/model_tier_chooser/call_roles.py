"""Which tier each Quest AI Runner model call runs on: its call names a ROLE of the hub's tier table, and the table gives the tier.

The policy lives once, in the hub's role table (``hubofhubs.api.role_tiers``: role, tier, the rule, evidence). This module is only QAR's
binding: ``CALL_ROLES`` says which hub role each QAR call is (a prompt spec id, or the id of a direct call that has no spec), and
``tier_for_call`` applies a person's or lane's explicit tier on top, under the three guards the owner set:

  * classification is never above fast (an explicit tier is ignored for a classification role);
  * a response a person reads is never below balanced (an explicit tier may only raise it);
  * best is reached only where a person asked for it or set it (a role the table lets a person set) or by the hub's structure watch (the ``structure-watch`` role, one small call per autopilot run).

A call whose tier is the caller's on purpose (a task's tier, a chat setting) names a role the table lets a person set (``main_session``) and
passes that tier as ``requested``. No code names a model; ``ModelRegistry.resolve_role`` turns the tier into the lane's model, and a lane's
``QAR_MODEL_<TIER>`` pins still decide which model a tier is.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional

from hubofhubs.api import role_tiers

TIERS = role_tiers.TIERS
ORDER = {tier: rank for rank, tier in enumerate(TIERS)}
# A tier word that is a domain of calls and not a capability level. It stays readable so existing configs keep working, and resolves
# through the table as the role that domain's calls have: science calls are investigations (high volume, fast). A rare foundational
# scientific judgement names the ``foundational_task`` role itself.
LEGACY_TIER_ROLES: Mapping[str, str] = {"science": "learning_investigation"}

# The structure watch: a small best-tier call the hub's Proactivity times and the hub binds here, one per autopilot run. The hub's table row that
# carries it is ``structure-watch``, the one row of the table that is best without the person asking.
STRUCTURE_WATCH_CALL = "structure-watch"
STRUCTURE_WATCH_ROLE = "structure-watch"

# call id -> hub role. A prompt spec id is the call id of its prompt; the others name a direct call.
CALL_ROLES: Dict[str, str] = {
    # the chat turn: understanding, routing, planning, tool choice
    "route-a-message.understand": "request_classification",
    "route-a-message.planner": "request_planning",
    "route-a-message.cascade-review": "critical_review",
    "claim-reach.reach-judge": "critical_review",
    "preflight.plan-sufficiency": "critical_review",
    "replay-known-operations.fill-args": "request_extraction",
    "ground-in-intent.derive-goal-condition": "request_extraction",
    "ground-in-intent.resolve-request": "request_extraction",
    "ground-in-intent.condense-decision": "summary",
    # judgments that label the turn
    "judge-a-decision.intent-directive": "request_classification",
    "judge-a-decision.mode-release": "request_classification",
    "judge-a-decision.found-constraint": "request_classification",
    "judge-a-decision.landed-work-denial": "request_classification",
    "judge-a-decision.quest-ranking": "request_classification",
    "assist-a-terminal-session.goal-judge": "request_classification",
    "assist-a-terminal-session.ask-classify": "request_classification",
    "hand-off-a-goal.assignee": "representative_selection",
    "hand-off-a-goal.doability": "request_classification",
    "select-a-rep.explicit-ask": "representative_selection",
    "select-a-rep.judge": "representative_selection",
    "feed-context-sources.admission": "request_classification",
    "feed-context-sources.relevance": "request_classification",
    "reply-check.view": "request_classification",
    # context selection and retrieval
    "select-context.consolidate": "context_retrieval",
    "select-context.rank-files": "context_retrieval",
    "select-context.card-scores": "context_retrieval",
    "select-context.bm25-queries": "context_retrieval",
    "select-context.guidance": "context_retrieval",
    "select-context.anticipation": "context_retrieval",
    "index-by-meaning.queries": "context_retrieval",
    "index-by-meaning.review": "context_retrieval",
    "index-by-meaning.association": "context_retrieval",
    "search-cards-and-specs.pool-judge": "context_retrieval",
    "spec-selection.batch-judge": "context_retrieval",
    "digest-records-for-a-need.digest-head": "context_retrieval",
    "digest-records-for-a-need.digest-group": "context_retrieval",
    "describe-an-attachment.image": "context_retrieval",
    "feed-context-sources.card-update": "card_building",
    "label-context-cards.metadata": "card_building",
    "label-context-cards.categorize-files": "card_building",
    "label-context-cards.after-deep": "card_building",
    "label-context-cards.bootstrap": "card_building",
    "investigate.place": "learning_investigation",
    "investigate.reuse": "learning_investigation",
    "web-search.research": "web_search",
    # the answer, and everything else a person reads
    "answer.response": "response_writing",
    "answer.task-report": "response_writing",
    "answer.clarify": "response_writing",
    "explain-an-answer.how-i-got-this": "summary",
    "narrate-a-turn.first-beat": "acknowledgement",
    "assist-a-terminal-session.ack": "acknowledgement",
    "reply-in-steps.interim-progress": "status_narration",
    "narrate-a-turn.progress": "status_narration",
    "milestone-progress.deliverable": "response_writing",
    "milestone-progress.judgment": "request_classification",
    # checks on a run and its result
    "verify-goal.verdict": "stop_decision",
    "watch-a-run.overseer": "stop_decision",
    # deep work and what a person or a task sets
    "deep-run.session": "main_session",
    "deep-run.sub-agent": "sub_agent",
    "draft-a-metric.value": "main_session",
    "autopilot.work": "main_session",
    # the one role that may resolve to best without the person asking: bound by the hub's structure watch, one small call per autopilot run
    STRUCTURE_WATCH_CALL: STRUCTURE_WATCH_ROLE,
}


# The reply a person's own tier (a task's tier, a chat setting) is applied to: it is the main work of that turn, so their explicit tier, best included,
# wins there (the hub's ``response_writing`` row does not let a person set a tier, so this binding says where a person's tier is the point).
PERSON_SET_CALLS = frozenset({"answer.response", "answer.task-report"})


def legacy_word_tier(word: str) -> Optional[str]:
    """The tier a legacy domain word (``science``) reads as, through its role in the table; None for any other word."""
    role = LEGACY_TIER_ROLES.get((word or "").strip().lower())
    return role_tiers.tier_for(role) if role else None


def role_of(call: str) -> str:
    """The hub role of ``call``. Raises ``KeyError`` for a call with no role, so a typo or a new call without one fails where it is written."""
    try:
        return CALL_ROLES[call]
    except KeyError:
        raise KeyError(f"model call {call!r} has no role in CALL_ROLES; add it with the hub role whose tier it should have") from None


def tier_for_call(call: str, requested: Optional[str] = None, table: Optional[Mapping[str, Mapping[str, str]]] = None) -> str:
    """The tier ``call`` runs on: its role's tier from the hub's table, with an explicit ``requested`` tier (a person's, a task's or a lane's) applied
    under the guards in the module docstring. ``requested`` that is not a tier is ignored; the legacy word ``science`` reads as its role's tier."""
    rows = table if table is not None else role_tiers.read_table()
    role = role_of(call)
    row = rows[role]
    base = role_tiers.tier_for(role, table=rows)
    want = (requested or "").strip().lower()
    if want in LEGACY_TIER_ROLES:
        want = role_tiers.tier_for(LEGACY_TIER_ROLES[want], table=rows)
    want = role_tiers.normalise(want)
    if want not in TIERS or row.get("kind") == "classify":
        return base
    follows = (row.get("tier") or "").strip().lower() not in TIERS    # a role that follows another (a sub-agent follows the main session)
    if follows or (row.get("person_may_set") or "").strip().lower() == "yes":
        return role_tiers.tier_for(role, want, table=rows)
    if want == "best" and call not in PERSON_SET_CALLS:
        want = "quality"                      # best is a person's explicit tier on their own work, or the structure watch's, alone
    if row.get("kind") == "write":
        return want if ORDER[want] >= ORDER[base] else base
    return want


def calls_at(tier: str, table: Optional[Mapping[str, Mapping[str, str]]] = None) -> list:
    """Every call whose table tier is ``tier`` (for the record and the cost report)."""
    rows = table if table is not None else role_tiers.read_table()
    return sorted(call for call in CALL_ROLES if role_tiers.tier_for(CALL_ROLES[call], table=rows) == tier)


def spend_by_role(rows, since: float = 0.0, until: float = 0.0, table: Optional[Mapping[str, Mapping[str, str]]] = None) -> list:
    """Spend by call role and tier from the per-call records the runner already keeps (``read_call_log``: ``ts``, ``spec_id``, ``tokens_in``,
    ``tokens_out``, ``cost_usd``), over ``since`` to ``until`` (epoch seconds; 0 means open). One row per role, biggest cost first:
    ``role``, ``tier``, ``calls``, ``tokens``, ``cost_usd``, ``share`` (of total cost, 0 to 1) and ``evidence`` (the table's mark, shown for every
    role above fast; empty for fast). A call whose spec is not in ``CALL_ROLES`` (a call made with no registered prompt spec) is the role
    ``(untagged)``, so nothing is hidden."""
    rows_table = table if table is not None else role_tiers.read_table()
    totals: Dict[str, Dict[str, float]] = {}
    for row in rows:
        stamp = float(row.get("ts") or 0)
        if (since and stamp < since) or (until and stamp > until):
            continue
        spec = str(row.get("spec_id") or "")
        role = CALL_ROLES.get(spec)
        key = role if role in rows_table else "(untagged)"
        entry = totals.setdefault(key, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
        entry["calls"] += 1
        entry["tokens"] += int(row.get("tokens_in") or 0) + int(row.get("tokens_out") or 0)
        entry["cost_usd"] += float(row.get("cost_usd") or 0.0)
    total_cost = sum(entry["cost_usd"] for entry in totals.values())
    report = []
    for key, entry in totals.items():
        tier = role_tiers.tier_for(key, table=rows_table) if key in rows_table else ""
        evidence = rows_table[key].get("evidence", "") if key in rows_table and tier and ORDER.get(tier, 0) > 0 else ""
        report.append({"role": key, "tier": tier, "calls": entry["calls"], "tokens": entry["tokens"], "cost_usd": round(entry["cost_usd"], 6),
                       "share": (entry["cost_usd"] / total_cost) if total_cost else 0.0, "evidence": evidence})
    return sorted(report, key=lambda r: (-r["cost_usd"], -r["calls"], r["role"]))


def render_spend_report(report: list) -> str:
    """The report as aligned text: role, tier, calls, tokens, cost, share, and the evidence mark of every role above fast."""
    lines = [f"{'role':32} {'tier':9} {'calls':>7} {'tokens':>11} {'cost USD':>10} {'share':>6}  evidence (roles above fast)"]
    for r in report:
        lines.append(f"{r['role']:32} {r['tier'] or '-':9} {r['calls']:>7} {r['tokens']:>11} {r['cost_usd']:>10.4f} {r['share'] * 100:>5.1f}%  {r['evidence']}")
    return "\n".join(lines)
