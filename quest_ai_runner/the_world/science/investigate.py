"""Investigating for a person's quest: what a request needs from outside, looked into as a small review and handed back as a record.

The look is the hub's Learning solution (``the-learning``, its ``review``): a protocol fixed before searching, queries with dates and counts, screening
with a reason for each count, graded items, a synthesis and the limits. This module is Science's side of it for Quest: it decides, from structured facts and
one fast-tier judgement each (never from words in a message), WHICH strategy the question bears on, whether the question stays inside an experiment's
scope, and whether an earlier investigation already answers it; it carries the review out; and it turns the hub's record into the shape the Quest API
stores. Nothing here writes to Quest: the tool in ``the_body/hands/quest_operations/tools_quest_investigations.py`` records the result.

Every model call here is the fast tier (``tier="fast"``), through the hub's ``quest-world`` seat, so a deployment's tier table decides the model and nothing
is named here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from quest_ai_runner.the_self.integrity.prompts.prompt_specs import declare, render_prompt

SIZES = ("quick", "standard", "full")
FRESH = "fresh"

PLACE_PROMPT = """You place one question that Quest AI is about to look into for a person's quest.

The question: {question}
The decision it informs: {decision}

The strategies of the quest (id: name; the first one, whose id is the quest's own id, is the whole quest):
{strategies}

The experiment it is done inside (empty when none):
{experiment}

Decide three things and answer with one JSON object and nothing else:
- "solution_ids": the ids of the NARROWEST strategies the question bears on (one or several), copied exactly from the list. Use the whole quest's id only when no narrower strategy fits.
- "within_scope": true when no experiment is given, or when the question is inside the experiment's in-scope list, is not outside its scope and does not reopen anything it holds fixed. false otherwise.
- "scope_note": one short plain sentence saying why when within_scope is false, else an empty string.
Write no em dashes."""

REUSE_PROMPT = """Quest AI is about to look into a question for a person's quest. Earlier investigations on the same strategy or one above it are listed.

The question: {question}
The decision it informs: {decision}

Earlier investigations (id | date | how sure | question | what it found):
{earlier}

Answer with one JSON object and nothing else:
- "answered_by": the id of the ONE earlier investigation whose findings answer this question, copied exactly, or an empty string when none does. An investigation about a related but different question does not answer it.
Write no em dashes."""

PLACE_SPEC = declare("investigate.place", solution="web-search", metric="metric-search-the-web", tier="fast", cache_layer="none",
                     template=PLACE_PROMPT, output_schema=None)
REUSE_SPEC = declare("investigate.reuse", solution="web-search", metric="metric-search-the-web", tier="fast", cache_layer="none",
                     template=REUSE_PROMPT, output_schema=None)

SURE_WORDS = {"A": "well established", "B": "fairly sure", "C": "not very sure", "D": "weak"}


def sure_words(grade: Optional[str]) -> str:
    """How sure a result is, in plain words, from its best grade."""
    return SURE_WORDS.get(grade or "", "nothing reliable was found")


@dataclass
class Placement:
    solution_ids: List[str] = field(default_factory=list)
    within_scope: bool = True
    scope_note: str = ""


def parse_object(text: Any) -> Dict[str, Any]:
    """The JSON object in a model reply (fences and chatter around it are ignored); empty when there is none."""
    raw = str(text or "")
    found = re.search(r"\{.*\}", raw, re.S)
    if not found:
        return {}
    try:
        data = json.loads(found.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def world_ask(prompt: str) -> str:
    """One fast-tier model call through the hub's ``quest-world`` seat (a tier, never a model name)."""
    from esoi import AssemblyConfig

    from quest_ai_runner.the_self.assembler import hub
    seat = hub.child().assemble(AssemblyConfig(solutions=("quest-world",)))["quest-world"]
    return str(seat.ask(prompt, "fast") or "")


def experiment_text(experiment: Optional[Mapping[str, Any]]) -> str:
    if not experiment:
        return ""
    lines = [f"research question: {experiment.get('research_question') or experiment.get('question') or ''}"]
    for key, label in (("in_scope", "in scope"), ("outside_scope", "outside scope for now"), ("held_fixed", "held fixed until the reevaluation")):
        if experiment.get(key):
            lines.append(f"{label}: " + "; ".join(str(x) for x in experiment[key]))
    return "\n".join(lines)


def place(question: str, decision: str, solutions: Sequence[Mapping[str, Any]], experiment: Optional[Mapping[str, Any]] = None,
          ask: Optional[Callable[[str], str]] = None) -> Placement:
    """The narrowest strategies the question bears on, and whether it stays inside the experiment's scope. ``solutions``: ``{id, name}`` rows with the whole
    quest first. The ids a model returns are kept only when they are in the list, and an empty answer means the whole quest. A scope that cannot be
    checked counts as outside it: an experiment's held choices are never reopened on a guess."""
    known = {str(s["id"]): str(s.get("name") or "") for s in solutions}
    whole = str(solutions[0]["id"]) if solutions else ""
    prompt = render_prompt("investigate.place", question=question, decision=decision or "(none stated)",
                           strategies="\n".join(f"{i}: {n}" for i, n in known.items()), experiment=experiment_text(experiment) or "(none)")
    data = parse_object((ask or world_ask)(prompt))
    ids = [str(s) for s in dict.fromkeys(data.get("solution_ids") or []) if str(s) in known]
    if not data:
        return Placement([whole] if whole else [], within_scope=experiment is None, scope_note="" if experiment is None else "The scope could not be checked.")
    scope = data.get("within_scope") is True or experiment is None
    return Placement(ids or ([whole] if whole else []), scope, "" if scope else str(data.get("scope_note") or "It is outside what the experiment looks into now."))


def reuse_line(row: Mapping[str, Any]) -> str:
    found = "; ".join(str(x) for x in (row.get("known") or [])[:2]) or "; ".join(str(i.get("claim")) for i in (row.get("items") or [])[:2])
    return f"{row.get('investigation_id')} | {row.get('done_on')} | {sure_words(row.get('best_grade'))} | {row.get('question')} | {found}"


def answering(question: str, decision: str, earlier: Sequence[Mapping[str, Any]], ask: Optional[Callable[[str], str]] = None) -> Optional[Mapping[str, Any]]:
    """The earlier investigation that answers the question, or None. Only ids in ``earlier`` count."""
    if not earlier:
        return None
    prompt = render_prompt("investigate.reuse", question=question, decision=decision or "(none stated)", earlier="\n".join(reuse_line(r) for r in earlier))
    chosen = str(parse_object((ask or world_ask)(prompt)).get("answered_by") or "").strip()
    return next((r for r in earlier if r.get("investigation_id") == chosen), None)


def hub_learning():
    """The hub's Learning seat, built from QAR's selections (``quest_ai_runner.run`` runs only its entry, ``stale_areas``)."""
    from esoi import AssemblyConfig

    from quest_ai_runner.the_self.assembler import hub
    return hub.child().assemble(AssemblyConfig(solutions=("the-learning",)))["the-learning"]


def review(question: str, decision: str, size: str, research_question_id: str = "", criteria: Optional[Mapping[str, Any]] = None,
           queries: Sequence[str] = (), today: str = "", learning: Any = None, solution_ids: Sequence[str] = ()) -> Mapping[str, Any]:
    """Carry out the investigation as the hub's review: sized by ``size`` (quick, standard or full; the caller never passes full unless the person
    asked), screened by ``criteria``, with the model-written synthesis only above the quick size. The hub files an investigation under at least one
    solution: the quest's strategies named here go in as its ``solution:<id>`` links (so it adds no fallback note to the limitations). The reuse of an
    earlier investigation is Quest's own (``place`` and ``answering``, over the quest's records), so the hub's lookup in its own records is off."""
    seat = learning if learning is not None else hub_learning()
    return seat.review(question, decision, size=size, criteria=dict(criteria or {}), queries=list(queries), experiment_id=research_question_id,
                       today=today or date.today().isoformat(), use_model=size != "quick", links=[f"solution:{s}" for s in solution_ids], reuse=False)


def lines_of(cell: Any) -> List[str]:
    return [x.strip() for x in str(cell or "").split(";") if x.strip()]


def quest_record(hub_record: Mapping[str, Any], *, solution_ids: Sequence[str], research_question_id: str = "", goal_id: str = "",
                 asked_by: str = "person", origin: str = "chat", extends: str = "") -> Dict[str, Any]:
    """The hub's investigation record as the body the Quest API stores. Software-only parts of the hub's record (a licence per item, a knowledge id) are
    left out, and so is the hub's applied part: what a finding changes in a person's quest is decided afterwards, through the usual confirmation."""
    row = dict(hub_record.get("investigation") or {})
    known, contested, unknown = (lines_of(row.get(k)) or [str(row.get(k) or "")] for k in ("synthesis_known", "synthesis_contested", "synthesis_unknown"))
    items = [{"item_id": i.get("item_id"), "claim": i.get("claim"), "source": i.get("source"), "source_date": i.get("source_date"),
              "independent": i.get("independent") or "unknown", "verified": i.get("verified") or "snippet", "grade": i.get("grade") or "C"}
             for i in hub_record.get("items") or []]
    found = str(row.get("found") or "0")
    record: Dict[str, Any] = {
        "solution_ids": list(solution_ids), "decision": row.get("decision") or "", "research_question_id": research_question_id or None,
        "goal_id": goal_id or None, "extends_investigation_id": extends or None, "asked_by_kind": asked_by, "carried_out_by": "quest_ai", "origin": origin,
        "kind": row.get("kind") or "review", "size": row.get("size") or "quick", "protocol_date": row.get("protocol_date") or None,
        "done_on": row.get("done_on") or None, "method": row.get("method") or "", "criteria": row.get("criteria") or "",
        "sources": [s.strip() for s in str(row.get("sources") or "").split(",") if s.strip()],
        "queries": [{"source": q.get("source"), "query": q.get("query"), "date": q.get("date"), "hits": q.get("hits") or 0} for q in hub_record.get("queries") or []],
        "found": int(found) if found.isdigit() else 0,
        "screened_out": [{"count": n, "reason": reason} for reason, n in (hub_record.get("screened_out") or {}).items()],
        "items": items, "synthesis": {"known": [x for x in known if x], "contested": [x for x in contested if x], "unknown": [x for x in unknown if x]},
        "limitations": [str(row.get("limitations") or "")] if row.get("limitations") else [],
    }
    return {k: v for k, v in record.items() if v not in (None, "")}


def summary_text(recorded: Mapping[str, Any], reused: bool = False) -> str:
    """What was looked into, how sure it is and what it changed, in plain words, for the reply."""
    known = "; ".join((recorded.get("synthesis") or {}).get("known") or []) or "nothing was settled"
    unknown = "; ".join((recorded.get("synthesis") or {}).get("unknown") or [])
    limits = "; ".join(recorded.get("limitations") or [])
    lead = "An earlier investigation already answers this" if reused else "Looked into"
    queries = len(recorded.get("queries") or [])
    return (f"{lead}: {recorded.get('question')}. " + ("" if reused else f"It ran {queries} search(es) and kept {len(recorded.get('items') or [])} finding(s). ")
            + f"How sure: {sure_words(recorded.get('best_grade'))}. Found: {known}." + (f" Not established: {unknown}." if unknown else "")
            + (f" Limits: {limits}." if limits else "") + " It has changed nothing yet; any change to the person's quest is proposed and goes through their confirmation.")
