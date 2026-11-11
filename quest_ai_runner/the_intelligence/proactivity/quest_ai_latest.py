"""The standing "quest-ai-latest" schedule: when Quest AI should take a new look at a quest, and which look comes first. No autopilot involved.

WHEN is the hub's Proactivity (``quest_ai_runner.run("proactivity", ...)``); this module only says what the quest's own signals mean as schedules:

- ``quest-ai-latest``: reactive, its event source is ``quest-activity`` (things that happened on the quest, each ``{"at": ISO time, "kind": ...}``, read by the host
  from its own records and passed in; ``QuestActivity`` turns them into Intake Sources events). Its last run is the quest's last look.
- ``quest-ai-latest:revisit``: reactive; each investigation whose revisit date has passed is an event at that date.
- ``quest-ai-latest:acted-on``: reactive; the time the last finding on the current card was acted on is its event, when every finding has been.

The minimum gap between looks is not a rule of Proactivity: it is the person's ``not_before`` in Prioritization's answer (last look plus the gap), which holds the
event schedule back. A revisit or an all-acted-on card is not held by the gap. WHAT to look into is Prioritization too: the ``candidates`` (each ``id``, ``kind`` among
``KIND_ORDER`` and optionally ``deadline``) come back in its order. The host owns the data and the look itself; nothing here reads a store or calls a model.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

import quest_ai_runner
from hubofhubs.api import attach_events

from quest_ai_runner.the_body.intake_sources.quest_events import QuestActivity, ACTIVITY_SOURCE

SCHEDULE = "quest-ai-latest"
REVISIT = "quest-ai-latest:revisit"
ACTED_ON = "quest-ai-latest:acted-on"
KIND_ORDER = ("refresh", "check_habit", "plan_question")     # what to look into, first first: an overdue look again, a check on what the person did, the plan's open question
DEFAULT_MIN_GAP_DAYS = 3
SCHEDULE_KIND_ORDER = (REVISIT, ACTED_ON, SCHEDULE)


def parse_time(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def schedules_of(last_look_at: str, revisit_dates: Sequence[str], acted_on_at: str) -> List[Dict[str, Any]]:
    last = last_look_at or ""
    return [
        {"name": SCHEDULE, "request": "look at what changed on the quest", "event_source": ACTIVITY_SOURCE, "last_run": last, "kind": SCHEDULE},
        {"name": REVISIT, "request": "look again at an earlier look", "reactive": True, "last_run": last, "events": list(revisit_dates), "kind": REVISIT},
        {"name": ACTED_ON, "request": "look into what is next", "reactive": True, "last_run": last, "events": [acted_on_at] if acted_on_at else [], "kind": ACTED_ON},
    ]


def due_triggers(schedules: Sequence[Mapping[str, Any]], events: Sequence[Mapping[str, Any]], now: str, min_gap_days: float, last_look_at: str) -> List[Mapping[str, Any]]:
    """The hub's Proactivity says which schedules are due; Prioritization then holds the event schedule back until the minimum gap after the last look has passed."""
    ready = attach_events(schedules, events)
    triggers = quest_ai_runner.run("proactivity", {"schedules": ready, "now": now})
    if not triggers:
        return []
    last = parse_time(last_look_at)
    stated: Dict[str, Any] = {"kind_order": list(SCHEDULE_KIND_ORDER)}
    if last is not None and min_gap_days > 0:
        stated["not_before"] = {SCHEDULE: (last + timedelta(days=min_gap_days)).isoformat()}
    candidates = [{"id": t["schedule"], "kind": next((s["kind"] for s in ready if s["name"] == t["schedule"]), "standing-work"), "request": t["request"]} for t in triggers]
    answer = quest_ai_runner.run("prioritization", {"candidates": candidates, "subject": "work", "stated": stated, "now": now})
    return quest_ai_runner.run("proactivity", {"schedules": ready, "now": now, "priorities": answer})


def order_looks(candidates: Sequence[Mapping[str, Any]], now: str) -> List[str]:
    """The ids of what to look into, in Prioritization's order: ``KIND_ORDER`` first, then the earlier ``deadline`` (a revisit date that has passed), then as given."""
    if len(candidates) < 2:
        return [str(c["id"]) for c in candidates]
    answer = quest_ai_runner.run("prioritization", {"candidates": list(candidates), "subject": "work", "stated": {"kind_order": list(KIND_ORDER)}, "now": now})
    return [str(item["id"]) for item in answer["ordered"]]


def next_look(*, now: Optional[datetime] = None, last_look_at: str = "", activity: Sequence[Mapping[str, Any]] = (), revisit_dates: Sequence[str] = (),
              acted_on_at: str = "", candidates: Sequence[Mapping[str, Any]] = (), min_gap_days: float = DEFAULT_MIN_GAP_DAYS) -> Dict[str, Any]:
    """Whether Quest AI should take a new look at a quest now, why, and what to look into first.

    ``last_look_at``: when the quest was last looked at (any origin), empty for never. ``activity``: what happened on the quest, each ``{"at", "kind"}``.
    ``revisit_dates``: the revisit dates of the quest's investigations that have passed. ``acted_on_at``: when the last finding on the current card was acted on
    (empty unless every finding has been). ``candidates``: what could be looked into (``id``, ``kind`` of ``KIND_ORDER``, optional ``deadline``).
    Returns ``{"due": bool, "trigger": "event" | "revisit" | "acted_on" | "", "reasons": [...], "order": [candidate ids]}``; ``order`` is empty when nothing is due."""
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    events = QuestActivity(activity).pull(ACTIVITY_SOURCE, limit=max(1, len(activity)))
    found = due_triggers(schedules_of(last_look_at, revisit_dates, acted_on_at), events, moment, min_gap_days, last_look_at)
    if not found:
        return {"due": False, "trigger": "", "reasons": [], "order": []}
    first = found[0]["schedule"]
    return {"due": True, "trigger": {REVISIT: "revisit", ACTED_ON: "acted_on"}.get(first, "event"), "reasons": [t["reason"] for t in found], "order": order_looks(candidates, moment)}
