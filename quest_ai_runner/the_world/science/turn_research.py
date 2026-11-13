"""Research a chat turn already did, kept on the quest after the reply is sent.

A turn that did web reads and whose planner named the decision the research serves (the structured ``research_decision``, empty for a quick fact) records one investigation
afterwards: no new search is made, and the record is the hub's own (``recorded_research.record_observed``) built from what the turn has: the searches it issued with their
results, the pages it opened in full (the rest stay snippets), the answer it gave, and the discipline the planner stated before reading (``research_criteria``: what would count
as a good source, ``research_change``: what would change the conclusion). One fast-tier call over the answer and the results gives what is known, contested and not established
and how fast the subject moves (the revisit date); a placement call (the strategy the question bears on) is the other. A fresh earlier investigation that answers the question is not
duplicated; a stale or smaller one is extended. The Quest backend, on recording, adds the findings to the strategy's knowledge and writes the investigation's context card.

It runs in a background thread after the answer (single flight per orchestrator, never delaying it); a failure is logged and kept in ``last`` for the turn's context panel, never
shown to the person. Needs the consumer's research memory on the web adapter (``RecordedWebResearch``): without it nothing is recorded.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from quest_ai_runner.the_world.science import investigate
from quest_ai_runner.the_world.science.research_memory import QuestInvestigationStore

log = logging.getLogger("quest-ai-runner.turn-research")

def outcome_of(done: Mapping[str, Any]) -> Dict[str, Any]:
    """What became of a turn's research, as the host stores it on the reply: ``outcome`` kept, reused (an earlier investigation already answered), extended (an earlier one was
    updated) or not_kept (with the reason), the ``investigation_id`` and what Quest did with the findings as knowledge."""
    if not done.get("recorded"):
        return {"outcome": "not_kept", "reason": str(done.get("error") or "the research could not be recorded"), "investigation_id": str(done.get("investigation_id") or "")}
    kind = "reused" if done.get("reused") else "extended" if done.get("extended") else "kept"
    return {"outcome": kind, "investigation_id": str(done.get("investigation_id") or ""), **({"knowledge": dict(done["knowledge"])} if done.get("knowledge") else {})}


def observed_from(gathered: Sequence[Any]) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """The web reads of a turn as the hub records them: ``[{"query", "hits"}]`` for each search (the results, grouped by the query that found them), and ``{url: text}``
    for each page opened in full."""
    searches: Dict[str, List[Dict[str, Any]]] = {}
    pages: Dict[str, str] = {}
    for obs in gathered or []:
        if not isinstance(obs, Mapping):
            continue
        rel, locator = str(obs.get("rel_path") or ""), str(obs.get("locator") or "")
        if rel.startswith("web_search:"):
            for hit in obs.get("hits") or []:
                if isinstance(hit, Mapping):
                    searches.setdefault(str(hit.get("query") or rel[len("web_search:"):]), []).append(dict(hit))
        elif locator.startswith("web extract:"):
            found = re.search(r"https?://\S+", locator)
            if found and obs.get("text"):
                pages[found.group(0)] = str(obs["text"])[:4000]
    return [{"query": q, "hits": hits} for q, hits in searches.items()], pages


def wanted(plan: Any, res: Any, memory: Any, observed: Sequence[Any]) -> bool:
    """True when this turn's research is to be kept: an answer, a research decision named by the planner, a quest memory on the web adapter and searches with results."""
    return bool(getattr(res, "kind", "") == "answer" and str(getattr(plan, "research_decision", "") or "").strip() and memory is not None and observed)


class TurnResearch:
    """Keeps a turn's research on its quest. ``last`` is what became of the latest attempt, for the context panel."""

    def __init__(self) -> None:
        self.running = threading.Event()
        self.last: Dict[str, Any] = {}

    def record(self, memory: Any, question: str, plan: Any, answer: str, observed: Sequence[Dict[str, Any]], pages: Mapping[str, str],
               ask: Optional[Callable[[str], str]] = None, today: str = "") -> Dict[str, Any]:
        """Place the question, then record through the hub's arm and the quest's store. Returns ``{"recorded", "investigation_id"?, "reused"?, "error"?}``; never raises."""
        try:
            from hubofhubs.api import RecordedResearch
            solutions = [{"id": s["id"], "name": s.get("name")} for s in memory.client.investigation_solutions(memory.quest_id)]
            decision = str(plan.research_decision).strip()
            placed = investigate.place(question, decision, solutions, None, ask)
            store = QuestInvestigationStore(memory.client, memory.quest_id)
            arm = RecordedResearch(freshness=None, science=None, web=None, plumbing=None, ask=ask or investigate.world_ask, store=store, knowledge=QuestHeldKnowledge(),
                                   placer=lambda q, d: placed.solution_ids)
            done = arm.record_observed(question, decision, observed, answer=answer, read=pages, source_criteria=str(getattr(plan, "research_criteria", "") or ""),
                                       would_change=str(getattr(plan, "research_change", "") or ""), size="quick", intent_id=memory.quest_id, today=today)
            made = store.last or {}
            statuses: Dict[str, int] = {}
            for answer in made.get("knowledge") or []:
                statuses[str(answer.get("status"))] = statuses.get(str(answer.get("status")), 0) + 1
            return {"recorded": bool(done.get("recorded")), "reused": bool(done.get("reused")), "extended": bool(done.get("updated")), "investigation_id": str(made.get("investigation_id") or done.get("investigation", {}).get("investigation_id") or ""),
                    "knowledge": statuses, **({"error": done["record_error"]} if done.get("record_error") else {})}
        except Exception as error:  # noqa: BLE001 -- keeping research is an aid: a failure is logged, never shown to the person
            log.warning("turn research not kept: %s: %s", type(error).__name__, str(error)[:300])
            return {"recorded": False, "error": f"{type(error).__name__}: {str(error)[:200]}"}

    def kickoff(self, memory: Any, question: str, plan: Any, res: Any, gathered: Sequence[Any], ask: Optional[Callable[[str], str]] = None) -> bool:
        """Start the recording in a background thread after the answer, unless one is already running. True when it was started."""
        observed, pages = observed_from(gathered)
        if not wanted(plan, res, memory, observed) or self.running.is_set():
            return False
        self.running.set()
        answer = str(getattr(res, "text", "") or "")

        def work() -> None:
            try:
                self.last = self.record(memory, question, plan, answer, observed, pages, ask)
                log.info("turn research: %s", self.last)
                report = getattr(memory, "report", None)
                if report is not None:          # what became of it, for the host to set on the stored reply (the reply itself never says it was kept: that is only true after this)
                    try:
                        report(outcome_of(self.last))
                    except Exception as error:  # noqa: BLE001
                        log.warning("turn research outcome not reported: %s", error)
            finally:
                self.running.clear()

        threading.Thread(target=work, name="turn-research", daemon=True).start()
        return True


def keep(orchestrator: Any, rs: Any, res: Any, gathered: Sequence[Any], ask: Optional[Callable[[str], str]] = None) -> bool:
    """Start keeping this turn's research when it is to be kept (see ``wanted``); True when started, so the reply can say so."""
    if getattr(rs, "plan", None) is None or not observed_from(gathered)[0]:   # no plan or no web search in the turn (an own-updates short circuit): nothing to keep, and the web adapter is not even looked at
        return False
    memory = getattr(getattr(orchestrator, "web", None), "memory", None)
    if memory is None:
        return False
    holder = getattr(orchestrator, "turn_research", None)
    if holder is None:
        holder = orchestrator.turn_research = TurnResearch()
    return holder.kickoff(memory, str(getattr(rs, "user_message", "") or ""), rs.plan, res, gathered, ask)


class QuestHeldKnowledge:
    """The hub's knowledge port for a quest: the findings become knowledge of their strategy when Quest records the investigation (its own AI write, with its own gates), so this
    port writes nothing and says so."""

    def add(self, solution_ids: Sequence[str], claim: str, source: str, knowledge_id: str, investigation_id: str, kind: str = "evidence", recorded: str = "") -> Mapping[str, Any]:
        return {"status": "proposed", "reason": "Quest adds the findings to the strategy's knowledge when it records the investigation"}
