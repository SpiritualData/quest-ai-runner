"""Iterating on a name nothing placed, until the hub's stop decision says to stop.

A request that holds a name (an acronym, say) that no glossary or readable file placed leaves the goal open. The next move is not asked of a model and is not a count: the
hub's selected Goal Completion ``decide_stop`` reads the iteration history, and this module only keeps that history from the run's own record. ``available`` is every route that
could place the name (the glossary, a file search, a wider file search, a read-only deep search when one can run); ``attempted`` is the routes the run really used, and a
file search that the time budget cut short is NOT counted as attempted, because a cut search is not proof of absence. A run that says it is blocked (``blocked``) with an untried
route gets ``continue`` naming that route, so "I don't know" is only possible after the routes were tried; only ``stop_met`` makes the goal met, and ``ask`` or a stop leaves it open.
Nothing here reads the words of a reply."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Mapping, Optional

from hubofhubs.api import ProgressEvent, ground_terms, selected_stop_decider

from quest_ai_runner.the_intelligence.mind.orchestrator_constants import log
from quest_ai_runner.the_self.role_understanding import terms as names

GLOSSARY, FILES, WIDER, DEEP = "glossary", "file-search", "wider-file-search", "deep-run"
WIDER_SECONDS, WIDER_LEVELS_UP, WIDER_DEPTH_DOWN = 10.0, 8, 5
MAX_PASSES = 4
ROUTE_WORDS = {GLOSSARY: "the glossary", FILES: "the readable files", WIDER: "nearby folders", DEEP: "a deeper read-only search"}
STOP_DECISION = "stop_decision"


def available_routes(deep_capable: bool) -> List[str]:
    """Every route that could place a name here, in the order they are tried."""
    return [GLOSSARY, FILES, WIDER] + ([DEEP] if deep_capable else [])


def attempted_routes(terms: List[Mapping[str, Any]], ran: List[str]) -> List[str]:
    """The routes the run used so far, from its own record: the glossary, the file search of the turn start (unless the time budget cut it short, which is not proof of absence),
    and every route this pass has run since (a route run once is not run again; the answer then says its search was cut)."""
    used = [GLOSSARY]
    if not any(t.get("grounded") is False and t.get("complete") is False for t in terms):
        used.append(FILES)
    return list(dict.fromkeys(used + list(ran)))


def route_words(routes: List[str]) -> str:
    """The routes in plain words, in order, for the person ("the glossary, the readable files and folders around them")."""
    words = [ROUTE_WORDS.get(r, r) for r in routes]
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1] if words else ""


def ask_line(open_names: List[str], attempted: List[str]) -> str:
    """The question a turn ends with when the hub decides only the person can place the name: written by code from the run's own record (what was open, what was tried), never a model's wording."""
    what = ", ".join(open_names)
    looked = route_words(attempted)
    return f"What does {what} stand for here?" + (f" I looked in {looked}." if looked else "")


def announce(emit: Any, decision: Mapping[str, Any], open_names: List[str], next_route: str = "") -> None:
    """One ``stop_decision`` progress event per decision of the hub: whether the turn goes on, why (the hub's reason code), what is left (the open names) and, when it goes on,
    what it tries next. The chat shows it as a live "still working" line and, at the end, as the plain reason the goal is not met. Never raises."""
    if emit is None:
        return
    go = decision.get("decision") == "continue"
    data: Dict[str, Any] = {"continue": go, "decision": decision.get("decision"), "reason": decision.get("code"),
                            "left": "what " + ", ".join(open_names) + (" stands" if len(open_names) == 1 else " stand") + " for"}
    if go and next_route:
        data["next"] = ROUTE_WORDS.get(next_route, next_route)
    try:
        emit.emit(ProgressEvent(type=STOP_DECISION, text=None, data=data))
    except Exception:  # noqa: BLE001 -- a sink that fails never stops the run
        pass


def decide(history: Mapping[str, Any]) -> Mapping[str, Any]:
    """The hub's selected stop decision for the history; when none can be assembled the pass stops asking (it never loops without a decider)."""
    decider = selected_stop_decider()
    if decider is None:
        return {"decision": "ask", "continue": False, "code": "no_decider", "reason": "no stop decision could be made here", "facts": {}}
    return decider(history)


def history_of(rs: SimpleNamespace, available: List[str], attempted: List[str], began: float, open_names: List[str], passes: List[Dict[str, Any]]) -> Dict[str, Any]:
    iterations = [{"verdict": False, "outcome": {"text": str(getattr(rs, "text", "") or ""), "terms": rs.terms, "blocked": "no source placed " + ", ".join(open_names)},
                   "changed": 0, "missing": [f"what {n} stands for" for n in open_names], "spend_usd": 0.0, "seconds": round(time.monotonic() - began, 1), "blocked": True,
                   "attempted": p.get("attempted") or []} for p in passes] or [
        {"verdict": False, "outcome": {"text": "", "terms": rs.terms}, "changed": 0, "missing": [f"what {n} stands for" for n in open_names], "spend_usd": 0.0,
         "seconds": 0.0, "blocked": True, "attempted": attempted}]
    return {"goal": rs.user_message, "iterations": iterations, "blocked": True, "available": available, "attempted": attempted, "metric": None, "policy": {"max_iterations": MAX_PASSES + 1}}


def iterate_on_names(orchestrator: Any, rs: SimpleNamespace, emit: Any, deep_search: Optional[Callable[[List[str]], Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Run passes on the open names while the hub says ``continue``. Returns ``{"decision": <hub decision>, "passes": [...], "placed": bool}`` and leaves ``rs.terms`` and, when a
    pass placed every name, a regenerated ``rs.text`` (the answer written again with the meaning in its context)."""
    open_names = names.open_names(rs.terms)
    if not open_names:
        return {"decision": {"decision": "stop_met", "code": "no_open_names"}, "passes": [], "placed": True}
    deep_capable = bool(deep_search is not None and orchestrator._has_deep_execution_capability())
    available = available_routes(deep_capable)
    folders = list(getattr(orchestrator.cfg, "private_path_roots", None) or [])
    began, ran, passes = time.monotonic(), [], []
    decision: Mapping[str, Any] = {}
    for _ in range(MAX_PASSES + 1):
        attempted = attempted_routes(rs.terms, ran)
        history = history_of(rs, available, attempted, began, open_names, passes)
        decision = decide(history)
        log.info("names pass: %s (%s) untried=%s", decision.get("decision"), decision.get("code"), decision.get("untried"))
        route = (decision.get("untried") or [""])[0]
        announce(emit, decision, open_names, route if decision.get("decision") == "continue" and len(passes) < MAX_PASSES else "")
        if decision.get("decision") != "continue" or len(passes) >= MAX_PASSES:
            break
        if emit is not None:
            emit.status(f"Looking for what {', '.join(open_names)} stands for ({route})…")
        if route in (FILES, WIDER):
            roots = [f for f in folders if f] or [__import__("os").getcwd()]
            found = {}
            for root in roots[:3]:
                for t in ground_terms(rs.user_message, root, names.glossary(), search=True, seconds=WIDER_SECONDS, levels_up=WIDER_LEVELS_UP, depth_down=WIDER_DEPTH_DOWN):
                    if t["term"] not in found or (t["grounded"] and not found[t["term"]]["grounded"]):
                        found[t["term"]] = t
            rs.terms = list(found.values())
        elif route == DEEP and deep_search is not None:
            try:
                found_by_deep = dict(deep_search(open_names) or {})
            except Exception:  # noqa: BLE001 -- a failing deep search is a tried route that placed nothing
                log.warning("names pass: the deep search failed", exc_info=True)
                found_by_deep = {}
            if found_by_deep.get("met") and str(found_by_deep.get("text") or "").strip():      # the run's own verified goal (its structured ``met``), never the wording of its text
                rs.terms = [dict(t, grounded=True, basis="deep-run", complete=True, passage=str(found_by_deep["text"])[:1800], source="a read-only deep search") if t.get("grounded") is False
                            else t for t in rs.terms]
        else:
            break
        ran.append(route)
        passes.append({"route": route, "attempted": attempted_routes(rs.terms, ran)})
        open_names = names.open_names(rs.terms)
        if not open_names:
            break
    placed = not names.open_names(rs.terms)
    if placed:
        decision = {"decision": "stop_met", "code": "placed", "reason": "every name the request holds was placed", "passes": len(passes)}
    return {"decision": dict(decision), "passes": passes, "placed": placed, "open": list(open_names), "attempted": attempted_routes(rs.terms, ran)}
