"""The visible ask an autopilot pass raises when its team's daily budget ran out part-way.

Before this, a budget stop was an INFO log line and a note on the pass row, so a quest could
silently lose most of a day's runs. Now the quest's owner is asked, once per quest per day,
through the runner's ordinary escalation sink (``QuestDecisionSink`` resolves the owner and never
files the ask for the runner's own account, and de-duplicates on ``dedupe_key``).

The text is composed here from structured facts, never by a model, and carries no em dashes.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from quest_ai_runner.the_body.internal_context.adapters_values import Escalation

BUDGET_SETTING = "autopilot_daily_budget"


def budget_stop_escalation(*, quest_id: str, quest_label: str, skipped: List[str], needed: int,
                           budget: int, used: int, today: datetime) -> Escalation:
    """The ask for a pass stopped by its daily budget. ``skipped`` are the display names of the
    personas whose runs did not happen, ``needed`` how many runs this quest's pass wanted in all."""
    names = ", ".join(skipped)
    raise_to = used + len(skipped)
    day = today.strftime("%Y-%m-%d")
    summary = (
        f"Autopilot ran out of its daily budget on {quest_label or 'a quest'} and skipped "
        f"{len(skipped)} of {needed} run(s) today: {names}. "
        f"Today's budget is {budget} autopilot task(s) for the team and {used} were already used. "
        f"To let them run, raise the setting {BUDGET_SETTING} to at least {raise_to}.")
    return Escalation(
        summary=summary, kind="approve", quest_id=quest_id,
        dedupe_key=f"autopilot-budget:{quest_id}:{day}",
        why_only_you="Only the person who owns the quest or the runner can raise the daily budget.",
        options=[f"Raise {BUDGET_SETTING}", "Leave it as it is"])
