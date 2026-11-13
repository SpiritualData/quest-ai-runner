"""Critical data for a turn: surface what the request depends on before the mind plans, and check after the answer that it was consulted and that the reply addresses the request.

Two layers, kept apart on purpose.

Subconscious (``surface``): the consumer offers a menu of sources in ``context_meta["critical_sources"]`` (each ``{"id", "what"}``; for a matched quest, its open asks). One narrow fast-tier
question, answered as structure by the hub's ``declare``, says which of them THIS request depends on; those are read now and put at the top of the turn's context and among what the turn
gathered. A request that depends on none (a goal's due date, what a feature does) reads nothing, so no unrelated ask can reach the reply. A source that cannot be read is marked
``unreadable``, which the stop decision turns into an honest "could not read it", never a guess from other data.

Critical thinking (``check``): independently of what surfaced, the hub's selected Goal Completion judges the turn's outcome: data the request depends on and nobody consulted, or a reply that does not
address the request as understood, is not met; the hub's stop decision then says whether another pass runs (a read of the named source, a rewrite that names the gap). A turn is shown as met
only after this check passed. Nothing here reads the words of the request or of the reply: the declaration and the judgment are structured replies of two narrow questions."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional

from hubofhubs.api import ProgressEvent, declare, judge_answer, selected_goal_checker, selected_stop_decider
from quest_ai_runner.the_intelligence.mind.orchestrator_constants import log

MAX_PASSES = 2
BLOCK_HEAD = ("WHAT THIS ANSWER DEPENDS ON (read just now; the reply must be written from it, and must not say it is missing or ask the person for it):\n")
SOURCE = "critical-data"


def menu_of(ctx_meta: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [m for m in ((ctx_meta or {}).get("critical_sources") or []) if isinstance(m, dict) and str(m.get("id") or "").strip()]


def read_source(orchestrator: Any, source: str, menu: Any = None) -> Optional[str]:
    """The text of one offered source, or None when it could not be read. An offer may carry its own ``read`` (a callable returning the text or None, for a consumer whose records are
    not cards of the context store, such as an embedding service's database); otherwise the source is a card of the context store, read whole by id, never searched for."""
    entry = next((m for m in (menu or ()) if isinstance(m, dict) and m.get("id") == source), None)
    if entry is not None and callable(entry.get("read")):
        try:
            text = entry["read"]()
        except Exception:  # noqa: BLE001 -- an unreadable source is reported as such, never as nothing
            return None
        return str(text).strip() or None if text is not None else None
    try:
        from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_index import card_writer
        store = card_writer(getattr(orchestrator, "context_assembler", None))
        card = store.get_card(source) if store is not None else None
    except Exception:  # noqa: BLE001 -- an unreadable source is reported as such
        return None
    if not isinstance(card, dict):
        return None
    parts = [str((item.get("locator") or {}).get("text") or "") for item in card.get("content") or [] if isinstance(item, dict)]
    text = "\n".join(p for p in parts if p.strip()).strip()
    return text or None


def surface(orchestrator: Any, rs: SimpleNamespace, ctx_meta: Optional[Mapping[str, Any]], emit: Any = None) -> None:
    """Declare what the request depends on, read it, and put it into the turn. Leaves ``rs.critical_data`` as the declared items with ``consulted`` or ``unreadable`` set."""
    rs.critical_data = []
    menu = menu_of(ctx_meta)
    rs.critical_menu = menu
    if not menu or not getattr(rs, "message_is_user_turn", True):
        return
    items = declare(getattr(rs, "goal_condition", "") or rs.user_message, menu, orchestrator.cheap_decision)
    if not items:
        return
    blocks: List[str] = []
    for item in items:
        text = read_source(orchestrator, item["source"], menu)
        if text is None:
            item["unreadable"] = True
            log.warning("critical data %s could not be read", item["source"])
            continue
        item["consulted"] = True
        blocks.append(text)
        rs.gathered.append({"kind": "query", "locator": f"{SOURCE}({item['source']})", "text": text})
    rs.critical_data = items
    if blocks:
        view = BLOCK_HEAD + "\n\n".join(blocks)
        rs.context_view = (view + "\n\n" + rs.context_view) if getattr(rs, "context_view", "") else view
        if emit is not None:
            emit.status("Read what the answer depends on.")


def outcome_of(rs: SimpleNamespace, judged: Mapping[str, Any]) -> Dict[str, Any]:
    return {"text": str(rs.text or ""), "critical_data": [dict(i) for i in getattr(rs, "critical_data", [])],
            "answers_request": judged.get("answers_request"), "missing": judged.get("missing") or ""}


def history_of(rs: SimpleNamespace, outcome: Mapping[str, Any], began: float, passes: int) -> Dict[str, Any]:
    unread = [i["source"] for i in outcome.get("critical_data") or [] if not i.get("consulted")]
    missing = ([f"the data the answer depends on: {', '.join(unread)}"] if unread else []) + ([str(outcome.get("missing"))] if outcome.get("missing") else [])
    return {"goal": rs.user_message, "iterations": [{"verdict": False, "outcome": outcome, "changed": 0, "missing": missing or ["an answer to the request"], "spend_usd": 0.0,
                                                   "seconds": round(time.monotonic() - began, 1)}] * max(1, passes),
            "metric": None, "policy": {"max_iterations": MAX_PASSES + 1}}


def unread_of(outcome: Mapping[str, Any]) -> List[str]:
    return [i["source"] for i in outcome.get("critical_data") or [] if not i.get("consulted")]


def announce(emit: Any, decision: Mapping[str, Any], outcome: Mapping[str, Any], next_step: str = "") -> None:
    """One ``stop_decision`` progress event per decision of the hub, the same shape the names pass sends: whether the turn goes on, why, what is left and what comes next. Never raises."""
    if emit is None:
        return
    unread = [i["source"] for i in outcome.get("critical_data") or [] if not i.get("consulted")]
    left = ("the data the answer depends on" if unread else "") or (str(outcome.get("missing") or "").strip() or "an answer to what was asked")
    data: Dict[str, Any] = {"continue": decision.get("decision") == "continue", "decision": decision.get("decision"), "reason": decision.get("code"), "left": left}
    if data["continue"] and next_step:
        data["next"] = next_step
    try:
        emit.emit(ProgressEvent(type="stop_decision", text=None, data=data))
    except Exception:  # noqa: BLE001 -- a sink that fails never stops the run
        pass


def check(orchestrator: Any, rs: SimpleNamespace, emit: Any = None) -> Optional[Dict[str, Any]]:
    """Judge the finished reply with the hub's selected Goal Completion and iterate while its stop decision says continue. Returns the verdict (``met`` and the structured facts), or
    None when no check could be made (no selected arm): a turn nobody checked is not called met."""
    checker = selected_goal_checker()
    decider = selected_stop_decider()
    if checker is None:
        return None
    ask = getattr(orchestrator, "careful_decision", None) or orchestrator.cheap_decision      # the judgement the goal rests on runs at the verify tier
    request = (getattr(rs, "goal_condition", "") or rs.user_message or "").strip()
    began, passes = time.monotonic(), 0
    while True:
        judged = judge_answer(request, rs.text, ask)
        outcome = outcome_of(rs, judged)
        verdict = dict(checker(rs.user_message, "", outcome))
        verdict.update(answers_request=judged.get("answers_request"), critical_data=[dict(i) for i in getattr(rs, "critical_data", [])], checked_by="critical-data")
        if verdict.get("met") or decider is None or passes >= MAX_PASSES:
            break
        decision = dict(decider(history_of(rs, outcome, began, passes + 1)))
        verdict["stop_decision"] = decision.get("decision")
        log.info("critical data pass: %s (%s) untried=%s", decision.get("decision"), decision.get("code"), decision.get("untried"))
        announce(emit, decision, outcome, "reading it and writing the answer again" if unread_of(outcome) else "writing the answer again, naming what was left out")
        if decision.get("decision") != "continue":
            break
        passes += 1
        steer_parts: List[str] = []
        for source in decision.get("untried") or []:
            item = next((i for i in rs.critical_data if i["source"] == source), None)
            text = read_source(orchestrator, source, getattr(rs, "critical_menu", None)) if item is not None else None
            if item is None or text is None:
                if item is not None:
                    item["unreadable"] = True
                continue
            item["consulted"] = True
            rs.gathered.append({"kind": "query", "locator": f"{SOURCE}({source})", "text": text})
            rs.context_view = BLOCK_HEAD + text + ("\n\n" + rs.context_view if getattr(rs, "context_view", "") else "")
            steer_parts.append("The data the answer depends on is now in the context (see the top block); write the reply from it.")
        if outcome.get("answers_request") is False and outcome.get("missing"):
            steer_parts.append("The previous reply did not address the request: it left out " + str(outcome["missing"]).rstrip(".") + ". Answer exactly what was asked.")
        if not steer_parts:
            break
        if emit is not None:
            emit.status("Rewriting the answer to address what was asked…")
        try:
            rs.text = rs._gen_answer(" ".join(steer_parts) + " Never use em dashes.")
        except Exception:  # noqa: BLE001 -- the reply stands as written; the verdict says it was not met
            log.warning("critical data rewrite failed", exc_info=True)
            break
    return verdict
