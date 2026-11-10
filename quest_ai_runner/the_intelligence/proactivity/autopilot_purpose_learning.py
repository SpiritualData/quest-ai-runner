"""The ``learning`` routine purpose: keep the hub's registry of the best existing implementations and public evidence current.

What is due is the hub's Learning solution (``quest_ai_runner.run("the-learning", <date>)``): this module only asks it, turns its answer into
items, and states the standing rules of the work. Each review is a small systematic review under the hub's knowledge search standard; where
the repository, the registry and the standard live is the routine's own instructions (consumer config), never this file.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, List, Mapping, Optional, Sequence

import quest_ai_runner

from quest_ai_runner.the_intelligence.proactivity.autopilot_routines import RoutinePurpose, register_purpose

LEARNING_SCOPE = ("keep the hub's registry of the best existing implementations and public evidence current, running each search as a small "
                  "systematic review under the hub's knowledge search standard.")

LEARNING_RULES = """- Ask only the hub's Learning what is due; the items listed below are that answer. Review nothing else.
- Run each review as the knowledge search standard says. Store each review record and every registry change where the hub's Learning stores them.
- Work in the hub repository named in the instructions, in its normal folder on its current main branch: edit and commit by explicit path, never push, never deploy, never create a branch or a copy. Put no private text, real names, emails, ids or absolute machine paths in anything you write there.
- Post ONE short note on the quest with add_quest_note: what was reviewed, what changed in the registry, what was adopted or rejected and why, and what is due next week.
- Raise a decision request (create_decision) ONLY for a choice a person must make, for example a licence question or promoting an arm. Everything else you decide yourself and record.
- Do nothing else on the quest."""


def item_of(entry: Any) -> Mapping[str, Any]:
    """One due entry of the hub's answer as an item: a plain name, or a mapping carrying its own id and words."""
    if isinstance(entry, Mapping):
        ident = str(entry.get("id") or entry.get("area") or entry.get("solution_id") or entry.get("name") or "")
        return {**entry, "id": ident, "what": str(entry.get("what") or entry.get("reason") or ident)}
    return {"id": str(entry), "what": str(entry)}


def learning_due(today: str, ask: Optional[Callable[[str], Any]] = None) -> List[Mapping[str, Any]]:
    """What the hub's Learning says is due on ``today`` (ISO date). ``ask`` replaces the hub call (tests, or a newer hub entry)."""
    answer = (ask or (lambda day: quest_ai_runner.run("the-learning", day)))(today)
    return [item_of(entry) for entry in (answer or [])]


def learning_purpose(ask: Optional[Callable[[str], Any]] = None) -> RoutinePurpose:
    def due_items(now: datetime) -> Sequence[Mapping[str, Any]]:
        return learning_due(now.date().isoformat(), ask)
    return RoutinePurpose("learning", LEARNING_SCOPE, LEARNING_RULES, due_items)


register_purpose(learning_purpose())
