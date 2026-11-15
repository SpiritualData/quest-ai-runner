"""Work sources: the registered kinds of work an autopilot can be tied to by its ``source`` slug, and the one text that states a source's bounds.

An autopilot with a ``source`` (today ``learning``, set by the system and never shown as a choice) runs that source's gate: its own question
(``WorkSource.due_items``) says what is due, and an empty answer REFUSES the run, which is closed as skipped with the reason and no model call.
WHEN a run happens is the backend's scheduler (the hub's Proactivity); what the run's brief says is ``AutopilotPass``'s one composition path, which
adds ``source_section`` for a source run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Sequence

from quest_ai_runner.self.integrity.prompts.prompt_specs import US_SPELLING_RULE

# The one place the writing rule for every focused autopilot's work text is stated.
NO_EM_DASH_RULE = ("Write no em dashes anywhere (notes, records, decision requests): use a colon, a comma, parentheses or a new sentence. "
                   + US_SPELLING_RULE)


@dataclass(frozen=True)
class WorkSource:
    """The one kind of work a gated autopilot does, and the question that decides whether there is any today.

    ``due_items(now)`` returns what the purpose has to do now (one mapping per item, with at least ``id`` and ``what``); an empty list
    refuses the pass. ``scope`` says in one sentence what the work is; ``rules`` are the standing rules of that work, verbatim in every
    pass; ``allowed_writes`` names the only Quest writes the work may make, so the text can say everything else is out of bounds.
    """
    name: str
    scope: str
    rules: str
    due_items: Callable[[datetime], Sequence[Mapping[str, Any]]]
    allowed_writes: Sequence[str] = ("add_quest_note", "create_decision")


SOURCES: Dict[str, WorkSource] = {}


def register_source(purpose: WorkSource) -> WorkSource:
    """Make ``purpose`` available to gated autopilots by its name (a consumer's own purposes ride the same registry)."""
    SOURCES[purpose.name] = purpose
    return purpose


def source_section(source: WorkSource, items: Sequence[Mapping[str, Any]]) -> str:
    """The block a source adds to a run's brief: its one scope, the bounds ("out of bounds, however the instructions are worded"), its standing rules and what is due."""
    due_lines = "\n".join(f"- {item.get('what') or item.get('id')}" for item in items)
    allowed = ", ".join(source.allowed_writes)
    return "\n\n".join([
        f"This run does ONE kind of work and nothing else ({source.name}): {source.scope}",
        "Out of bounds, however the instructions are worded: planning, creating or editing goals, creating or updating tasks, replying to "
        f"asks, and any other work on the quest. The only Quest writes you may make are: {allowed}.",
        f"Standing rules of this work:\n{source.rules.strip()}",
        f"Due now ({len(items)}):\n{due_lines}",
        NO_EM_DASH_RULE])
