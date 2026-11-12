"""Research memory for a quest's chat: before the planner's web read searches, look for earlier research on the quest that already answers it.

The hub's Learning arm ``recorded_research`` decides what an earlier investigation is worth (the same question, or one a fast-tier judgement says answers it; fresh,
stale or smaller). This module is Quest's side of it: ``QuestInvestigationStore`` is the hub's investigation store port over a Quest client (the earlier investigations
linked to the strategies the question bears on, read through the Quest API or the in-process client), and ``RecordedWebResearch`` wraps the web adapter the way
``PrivateLinkWebResearch`` does, so the planner's own ``{"web": "<query>"}`` read goes through the lookup first.

- A quest with no investigation costs nothing: no model call, no read beyond the list.
- A fresh earlier investigation that answers the query is returned as the read, with its date, what it found, what it left open and its limits, and the search is not made
  (``fresh: true`` on the read asks for a new one). The text tells the writer to say, in a few plain words, that the answer builds on earlier research and when it was done.
- A stale or smaller one does not stop the search: its text goes first, so the turn sees what is known and its date, and what the new search adds is the gap.

Recording is not here: a quick lookup in conversation is not recorded (the arm's rule); an investigation that informs a decision is recorded by the ``investigate_for_quest``
tool, and the backend adds its findings to the strategy's knowledge and its context card. Every model call is the fast tier through the hub's seat (never a model name).
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_body.internal_context.adapters_values import Observation
from quest_ai_runner.the_world.science import investigate

log = logging.getLogger("quest-ai-runner.research-memory")

LOCATOR = "earlier research"


def hub_row(row: Mapping[str, Any]) -> Dict[str, str]:
    """A Quest investigation as the hub's investigation cells (the ones the arm reads)."""
    synthesis = row.get("synthesis") or {}
    return {"investigation_id": str(row.get("investigation_id") or ""), "question": str(row.get("question") or ""), "decision": str(row.get("decision") or ""),
            "size": str(row.get("size") or "quick"), "done_on": str(row.get("done_on") or ""), "refresh_on": str(row.get("refresh_on") or ""),
            "synthesis_known": "; ".join(synthesis.get("known") or []), "synthesis_unknown": "; ".join(synthesis.get("unknown") or []),
            "limitations": "; ".join(row.get("limitations") or []), "links": ";".join(f"solution:{s}" for s in row.get("solution_ids") or []),
            "status": str(row.get("status") or "open"), "reuse_state": str(row.get("reuse") or "")}      # Quest says how current its own record is (fresh, partial or stale)


class QuestInvestigationStore:
    """The hub's ``InvestigationStore`` over a Quest client: ``related`` is the quest's earlier investigations on the given strategies and the ones above them (and on the
    experiment, when given), open ones only. ``record`` is the one way an investigation is recorded on the quest, for a turn's own research, an explicit investigation and a
    deferred task alike."""

    def __init__(self, client: Any, quest_id: str, research_question_id: str = ""):
        self.client, self.quest_id, self.research_question_id = client, quest_id, research_question_id
        self.rows: Dict[str, Mapping[str, Any]] = {}
        self.last: Mapping[str, Any] = {}

    def related(self, solution_ids: Sequence[str], scope: Sequence[str] = (), today: str = "") -> List[Dict[str, str]]:
        found = self.client.earlier_investigations(self.quest_id, list(solution_ids), self.research_question_id) or []
        self.rows.update({str(r.get("investigation_id")): r for r in found})
        return [hub_row(r) for r in found if (r.get("status") or "open") == "open"]

    def items(self, investigation_id: str) -> List[Dict[str, str]]:
        return [dict(i) for i in (self.rows.get(investigation_id) or {}).get("items") or []]

    def write(self, spec: Mapping[str, Any]) -> str:
        """Record the hub's investigation on the quest through the Quest API's own record call (the one the ``investigate_for_quest`` tool uses, with the same body builder).
        One that extends an earlier investigation of the quest (the hub keeps its id) is recorded as its successor, so the earlier one is superseded, not duplicated."""
        row, items = dict(spec["investigation"]), list(spec["items"])
        cells = [[p.strip() for p in entry.split("|")] for entry in str(row.get("queries") or "").split(";") if entry.strip()]
        counted = [entry.strip().split(" ", 1) for entry in str(row.get("screened_out") or "").split(";") if entry.strip()]
        hub_record = {"investigation": row, "items": items, "queries": [dict(zip(("source", "query", "date", "hits"), c)) for c in cells],
                      "screened_out": {reason: int(n) for n, reason in counted if n.isdigit()}}
        solution_ids = [link[9:] for link in str(row.get("links") or "").split(";") if link.startswith("solution:")] or [self.quest_id]
        extends = row["investigation_id"] if row["investigation_id"] in self.rows else ""
        return str(self.record(hub_record, solution_ids=solution_ids, extends=extends)["investigation_id"])

    def record(self, hub_record: Mapping[str, Any], *, solution_ids: Sequence[str], extends: str = "", research_question_id: str = "", goal_id: str = "", asked_by: str = "person",
               origin: str = "chat", question: str = "") -> Mapping[str, Any]:
        """Record the hub's investigation on the quest through the Quest API's record call, with the body builder every path shares. One that extends an earlier investigation
        is recorded as its successor (the earlier one is superseded, not duplicated). Returns what Quest answered, with the investigation id; raises when Quest refuses."""
        from quest_ai_runner.the_world.tools.tools import data_of
        row = dict(hub_record.get("investigation") or {})
        body = investigate.quest_record(hub_record, solution_ids=list(solution_ids), research_question_id=research_question_id or self.research_question_id, goal_id=goal_id,
                                        asked_by=asked_by, origin=origin, extends=extends)
        if row.get("refresh_on"):
            body["refresh_on"] = row["refresh_on"]
        if row.get("change_my_mind"):
            body["limitations"] = list(body.get("limitations") or []) + [f"What would change this: {row['change_my_mind']}"]
        made = data_of(self.client.record_investigation(self.quest_id, question or row.get("question") or "", **{k: v for k, v in body.items() if k != "question"})) or {}
        if not made.get("investigation_id"):
            raise RuntimeError("Quest refused the investigation")
        self.last = made
        return made


def hub_earlier(store: Any, question: str, decision: str, size: str, solution_ids: Sequence[str], ask: Optional[Callable[[str], str]] = None, today: str = ""):
    """The hub's recorded_research lookup and judgement over a quest store: ``(earlier or None, the arm)``. ``earlier`` is the investigation to reuse or extend, with its ``state``
    (fresh, stale or partial). The one implementation a turn's own research, an explicit investigation and a deferred task share."""
    from hubofhubs.api import RecordedResearch
    arm = RecordedResearch(freshness=None, science=None, web=None, plumbing=None, ask=ask or investigate.world_ask)
    return arm.earlier(store, question, decision, size, list(solution_ids), (), today or date.today().isoformat()), arm


def choose_solution(records: Mapping[str, Any], text: str, ask: Optional[Callable[[str], str]] = None) -> str:
    """The one solution of the quest's records that ``text`` informs, by the same matcher the structure reply uses (the hub's Relevant Solutions arm: the records' shortlist, then one
    fast-tier call), or '' when none fits. ``records`` is ``{table: {record id: row}}``."""
    from quest_ai_runner.the_intelligence.mind.response_writer.structure_reply import solution_choice
    return solution_choice(records, text, ask or investigate.world_ask)


def earlier_text(earlier: Mapping[str, str], state: str) -> str:
    """What the turn is told about earlier research, in plain words."""
    standing = {"fresh": "still current", "stale": "out of date", "partial": "thinner than this question needs"}.get(state, state)
    head = f"Earlier research on this quest (investigation {earlier['investigation_id']}, done {earlier.get('done_on')}, {standing}): {earlier.get('question')}."
    body = f" Found: {earlier.get('synthesis_known') or 'nothing settled'}." + (f" Not established: {earlier['synthesis_unknown']}." if earlier.get("synthesis_unknown") else "") \
        + (f" Limits: {earlier['limitations']}." if earlier.get("limitations") else "")
    return head + body


class ResearchMemory:
    """Looks up earlier research on one quest for a query."""

    def __init__(self, client: Any, quest_id: str, ask: Optional[Callable[[str], str]] = None, today: str = "", report: Optional[Callable[[Dict[str, Any]], None]] = None,
                 card_prefix: str = ""):
        self.client, self.quest_id, self.ask, self.today = client, quest_id, ask, today or date.today().isoformat()
        self.store = QuestInvestigationStore(client, quest_id)
        self.listed: Optional[bool] = None
        self.report = report            # called with what became of this turn's research once the recording ends (the host writes it onto the stored reply)
        self.card_prefix = card_prefix  # the host's context card id before an investigation id; with it, earlier research already in the turn's context is not told twice
        self.context: Any = None        # the turn's card cache, set by the read dispatcher before a web read

    def held_in_context(self, investigation_id: str) -> bool:
        """True when this turn's context already holds the investigation's card (so its findings are in front of the writer once)."""
        if not self.card_prefix or self.context is None:
            return False
        wanted = self.card_prefix + investigation_id
        held = list((getattr(self.context, "results", None) or {}).values()) + [getattr(self.context, "turn_start_result", None)]
        return any(wanted in (getattr(assembled, "card_ids", None) or []) for assembled in held if assembled is not None)

    def has_research(self) -> bool:
        if self.listed is None:
            try:
                self.listed = any((r.get("status") or "open") == "open" for r in (self.client.list_investigations(self.quest_id) or []))
            except Exception as error:  # noqa: BLE001 -- the memory is an aid: a failed read means no memory, never a failed turn
                log.info("research memory: could not list investigations: %s", error)
                self.listed = False
        return self.listed

    def earlier(self, question: str) -> Optional[Dict[str, str]]:
        """The earlier investigation that answers ``question`` with its ``state`` (fresh, stale or partial), or None."""
        if not question.strip() or not self.has_research():
            return None
        try:
            solutions = [{"id": s["id"], "name": s.get("name")} for s in self.client.investigation_solutions(self.quest_id)]
            placed = investigate.place(question, "", solutions, None, self.ask)
            return hub_earlier(self.store, question, "", "quick", placed.solution_ids, self.ask, self.today)[0]
        except Exception as error:  # noqa: BLE001
            log.info("research memory: lookup failed: %s", error)
            return None


class RecordedWebResearch:
    """Wrap a web adapter so a search first asks the quest's research memory. Everything else (fetch, describe, ...) is the wrapped adapter's own."""

    def __init__(self, web: Any, memory: ResearchMemory) -> None:
        self.web, self.memory = web, memory

    def __getattr__(self, name: str) -> Any:
        return getattr(self.web, name)

    def search(self, queries: Any, *, fresh: bool = False, **kwargs: Any) -> Observation:
        listed = [queries] if isinstance(queries, str) else list(queries or [])
        earlier = None if fresh or not listed else self.memory.earlier(str(listed[0]))
        if earlier is None:
            return self.web.search(queries, fresh=fresh, **kwargs)
        text = earlier_text(earlier, earlier["state"])
        if earlier["state"] == "fresh" and self.memory.held_in_context(earlier["investigation_id"]):
            return Observation(kind="query", locator=LOCATOR, hits=[], text=f"Earlier research on this quest (investigation {earlier['investigation_id']}, done {earlier.get('done_on')}, "
                               f"still current) covers this and its findings are already in your context above. Nothing new was searched. Build on it, and say in a few plain words "
                               f"that the answer builds on earlier research from {earlier.get('done_on')}. Search again with fresh=true only if the person asks for a new look.")
        if earlier["state"] == "fresh":
            return Observation(kind="query", locator=LOCATOR, text=text + " Nothing new was searched. Build on it, and say in a few plain words that the answer builds on earlier "
                               f"research from {earlier.get('done_on')}. Search again with fresh=true only if the person asks for a new look.", hits=[])
        found = self.web.search(queries, fresh=fresh, **kwargs)
        if found.kind == "error":
            return found
        return Observation(kind=found.kind, locator=found.locator, hits=found.hits, scope=found.scope, pattern=found.pattern, planner_note=found.planner_note, rel_path=found.rel_path,
                           text=text + " The search below looks for what has changed since; say in a few plain words that it adds to earlier research from "
                           f"{earlier.get('done_on')}.\n\n" + str(found.text or ""))
