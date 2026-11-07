"""The readiness stage of a milestone's metric: how ready the milestone's next step is, counted from records only (no model, no keyword on any model text).

Role: a first stage of the milestone metric in ESOI Application (a cascading stage). A milestone's own value (a count of people, a listing that is live) often cannot move
in one turn because it needs people. What an increment can move is how ready the next step is to be done, and that is measurable from the goal the quest keeps for it:
the step is ready when it has an owner the milestone's record lists, a due date inside this week, the links to the solution and to the metric, a concrete kept deliverable
(no placeholder slot left in it), and the one step only a person can do named. The stage is measured as ``<n> of <total>`` (``Stage.text``) and worded in a reply by its items with no count (``Stage.describe``), read from the goal before
the write and after it, and the milestone's own value is said beside it as moved or unchanged. A goal is read as the quest's listing carries it
(``assignee_user_id``, ``due``, ``text``) or as a write returns it (``assigned_to_user_id``, ``scheduled_date``, ``description``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

HUMAN_STEP = "Human step:"
DRAFT = "Draft:"
#: The items of the stage, in the order they are said: ``(key, what it is)``.
ITEMS: Tuple[Tuple[str, str], ...] = (
    ("owner", "an owner from the milestone's record"),
    ("due", "a due date this week"),
    ("links", "the solution and metric links"),
    ("deliverable", "a concrete kept draft"),
    ("human_step", "the human step named"),
)
#: A checkbox mark or a markdown link is not a slot to fill.
SLOT = re.compile(r"\{\{[^}\n]*\}\}|\[(?![ xX]?\])[^\]\n]{1,80}\](?!\()")


@dataclass(frozen=True)
class Stage:
    """The items of the readiness stage that hold, by key."""
    met: Tuple[str, ...]

    @property
    def total(self) -> int:
        return len(ITEMS)

    def text(self) -> str:
        """``<n> of <total>`` with the held items named: the measurement, kept for records and tests. It is not what a reply says (``describe``), because the total is computed by code
        and a reader of the reply cannot find it in any read."""
        names = [what for key, what in ITEMS if key in self.met]
        return f"{len(self.met)} of {self.total} ({'; '.join(names) if names else 'none held yet'})"

    def describe(self) -> str:
        """The stage as a reply words it, by its items with no count: ``none held``, ``all held: ...``, or ``held: ...; still missing: ...``."""
        held = [what for key, what in ITEMS if key in self.met]
        missing = [what for key, what in ITEMS if key not in self.met]
        if not held:
            return "none held"
        return f"all held: {', '.join(held)}" if not missing else f"held: {', '.join(held)}; still missing: {', '.join(missing)}"


def placeholder_slots(text: Any) -> Tuple[str, ...]:
    """The slots left in a draft to be filled by someone (``[source name]``, ``{{link}}``): a structural check of the text, never a reading of what the model decided."""
    return tuple(found.group(0) for found in SLOT.finditer(str(text or "")))


def draft_in(text: Any) -> str:
    """The draft a goal's text holds (everything after its ``Draft:`` line start), else ""."""
    head, sep, rest = str(text or "").partition(DRAFT)
    return rest.strip() if sep else ""


def human_step_in(text: Any) -> str:
    """The sentence after ``Human step:`` in a goal's text, else ""."""
    found = re.search(r"(?m)^" + re.escape(HUMAN_STEP) + r"[ \t]*(.*)$", str(text or ""))
    return found.group(1).strip() if found else ""


def day_between(value: Any, first: str, last: str) -> bool:
    """True when ``value`` starts with a YYYY-MM-DD day within ``first``..``last`` (ISO days compare as text)."""
    day = str(value or "")[:10]
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)) and first <= day <= last


def measure(goal: Optional[Mapping[str, Any]], *, owners: Sequence[str], solution_id: str, metric_ids: Sequence[str], monday: str, due: str) -> Stage:
    """The stage of the goal that is the milestone's next step; no goal holds nothing. ``owners`` are the user ids the record lists (empty when it lists none: any assignee holds)."""
    if not goal:
        return Stage(())
    pick = lambda *names: next((goal.get(n) for n in names if goal.get(n) not in (None, "")), None)
    text = pick("text", "description") or ""
    assignee = str(pick("assignee_user_id", "assigned_to_user_id") or "")
    draft = draft_in(text)
    held = {
        "owner": bool(assignee) and (not owners or assignee in {str(o) for o in owners}),
        "due": day_between(pick("due", "scheduled_date"), monday, due),
        "links": str(goal.get("spec_solution_id") or "") == solution_id and (not metric_ids or str(goal.get("spec_metric_id") or "") in {str(m) for m in metric_ids}),
        "deliverable": bool(draft) and not placeholder_slots(draft),
        "human_step": bool(human_step_in(text)),
    }
    return Stage(tuple(key for key, _ in ITEMS if held[key]))


def closing_line(before: Stage, after: Stage, *, label: str, metric: str) -> str:
    """The one line message 2 ends with: the readiness stage before and after, and the milestone's metric before and after as ``metric`` words them (said by the caller, who knows where
    each reading came from)."""
    return (f"Readiness of the next step: before, {before.describe()}; after, {after.describe()}. "
            f"Milestone metric ({label}): {metric}.")
