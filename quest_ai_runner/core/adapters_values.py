"""Value objects the brain passes around: events, observations, plan decisions, deep results, escalations, deadlines.

Split out of ``adapters.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# Value objects passed across the boundary (plain data, no behavior).
# ---------------------------------------------------------------------------

from hubofhubs.ports.retrieval import Observation  # noqa: F401  (moved to the hub; same class, old import path)
from hubofhubs.orchestrate.plan import PlanDecision  # noqa: F401  (moved to the hub; same class, old import path)


@dataclass
class Escalation:
    """A human-only step the brain refuses to take autonomously."""
    summary: str
    kind: str = "approve"
    quest_id: Optional[str] = None
    assignee: Optional[str] = None              # consumer-defined routing key
    default_on_silence: str = "hold"
    # WHEN this decision should be resolved by, an aware datetime or None for no deadline. A
    # sink that supports deadlines (``QuestDecisionSink``) applies its own configured default
    # (``QAR_DECISION_DEFAULT_DEADLINE_HOURS``) when this is left None -- callers that never
    # think about deadlines still get one if the deployment wants that; an explicit value here
    # always wins.
    deadline: Optional[datetime] = None
    # OPTIONAL http(s) page the human should open to decide: the draft, preview, doc or PR the ask
    # is about. Shown as a prominent link on the ask in Quest's daily reflection and Asks for you.
    review_url: Optional[str] = None


# A short relative form the planner can emit instead of computing a real timestamp itself:
# "in 48h", "in 2 days", "in 30m". Kept intentionally small (hours/days/minutes) -- a deadline
# on a human decision is never usefully more precise than that.
RELATIVE_DEADLINE_RE = re.compile(
    r"^in\s+(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|days?|d|minutes?|mins?|m)$",
    re.IGNORECASE,
)


def parse_deadline(raw: Optional[str], *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse a planner-supplied deadline string into an aware UTC datetime, or None.

    Accepts either a full ISO datetime (``2026-09-25T18:00:00Z`` or with a numeric UTC offset) or
    the short relative form above. Anything else -- missing, empty, unparseable -- returns None,
    exactly as if no deadline had been given: a malformed deadline must degrade to "no deadline",
    never raise into the escalation path.
    """
    text = (raw or "").strip()
    if not text:
        return None
    m = RELATIVE_DEADLINE_RE.match(text)
    if m:
        amount = float(m.group(1))
        unit = m.group(2).lower()
        base = now if now is not None else datetime.now(timezone.utc)
        if unit.startswith("h"):
            return base + timedelta(hours=amount)
        if unit.startswith("d"):
            return base + timedelta(days=amount)
        return base + timedelta(minutes=amount)
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
