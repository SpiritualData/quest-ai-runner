"""The person's own reflections, collected into one readable block.

Quest asks a person two standing questions and stores their answers verbatim:

  * the daily plan's "how did yesterday go" review (plus what they said they would do today), and
  * each period review's "how did this period go" / "what should I focus on next".

Those are the highest-signal context there is for deciding what to work on, because they are the
person's own words about their own work rather than anything inferred from task rows. Both live on
the USER (no team id, no quest id), so this module needs nothing but a client.

Two callers share it, and that is why it is here rather than inlined in either: the attended chat's
``QuestRetrievalAdapter`` (a live read the planner can issue mid-conversation) and the background
``AutopilotPass`` (which folds the latest reflection into the batch it composes). Both were
previously blind to reflections entirely, and answering "what should I work on" from two different
sets of material is exactly how a background pass and the person working the quest drift apart.

Every function here is best-effort and never raises: a missing endpoint, an unsubmitted review, or
a client that predates these methods degrades to an EMPTY context, which callers treat as "no
reflection on record" rather than as a failure. A person who has not written a reflection is the
normal case, not an error.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("quest-ai-runner.reflections")

# Which period reviews to look at, and in what order, when the caller does not say. Finest first:
# a week review is both more recent and more actionable than a year review. This is a DEFAULT, not
# an assumption baked into the logic -- every caller can pass its own order (autopilot derives one
# from the quest's own scope), and any of week/month/quarter/year is equally valid.
DEFAULT_PERIODS: Sequence[str] = ("week", "month")

# Per-field cap. A reflection is a free-text box, and an unusually long one must not push the goals
# and instructions it is meant to inform out of the model's attention.
MAX_FIELD_CHARS = 1200


def _clip(text: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """A stripped string, truncated with an explicit marker so nothing silently disappears."""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + " [...truncated]"


@dataclass
class ReflectionContext:
    """What the person most recently wrote, from the daily plan and one period review.

    Flat and all-optional: any subset can be present (a daily entry with no period review, a period
    review with no daily entry, neither). ``period`` names which period review the text came from,
    since "how did it go" means something different for a week than for a year.
    """
    daily_date: str = ""            # the day whose daily-plan entry this is (YYYY-MM-DD)
    yesterday_review: str = ""      # their reflection on the day BEFORE daily_date
    today_plan: str = ""            # what they said they would do ON daily_date
    period: str = ""                # "week" | "month" | "quarter" | "year", when a review was found
    period_label: str = ""          # "the current week" / "the previous week", for honest framing
    period_past: str = ""           # reflection_past: how that period went
    period_future: str = ""         # reflection_future: what to focus on next
    # Every period actually queried, in order. Kept so a caller can say "checked, nothing there"
    # instead of "did not look" -- the two are different pieces of information.
    checked_periods: List[str] = field(default_factory=list)

    def has_daily(self) -> bool:
        return bool(self.yesterday_review or self.today_plan)

    def has_period(self) -> bool:
        return bool(self.period_past or self.period_future)

    def has_any(self) -> bool:
        return self.has_daily() or self.has_period()

    def as_text(self) -> str:
        """The block a prompt or task text can carry, or "" when there is nothing to say.

        Labeled with the date and period it came from on purpose: a reflection read as if it were
        written today, when it is four days old, is worse than no reflection at all.
        """
        if not self.has_any():
            return ""
        lines = ["The person's own most recent reflection on Quest, in their words (not a summary, "
                 "and not inferred from task history):"]
        if self.yesterday_review:
            lines.append(f"  Daily review written on {self.daily_date or 'an unrecorded date'}, "
                         f"looking back at the day before it:\n    {self.yesterday_review}")
        if self.today_plan:
            lines.append(f"  What they said they would do on {self.daily_date or 'that day'}:"
                         f"\n    {self.today_plan}")
        if self.period_past:
            lines.append(f"  Their {self.period or 'period'} review ({self.period_label or 'recent'}), "
                         f"on how it went:\n    {self.period_past}")
        if self.period_future:
            lines.append(f"  Their {self.period or 'period'} review ({self.period_label or 'recent'}), "
                         f"on what to focus on next:\n    {self.period_future}")
        return "\n".join(lines)

    def one_line(self, limit: int = 220) -> str:
        """A single condensed line, for artifacts that hold one line of context (NextSteps.note)."""
        source = self.period_future or self.yesterday_review or self.today_plan or self.period_past
        if not source:
            return ""
        flat = " ".join(str(source).split())
        if len(flat) > limit:
            flat = flat[:limit].rstrip() + "..."
        if self.period_future or self.period_past:
            where = f"their {self.period or 'period'} review"
        else:
            where = f"their daily reflection ({self.daily_date})" if self.daily_date else "their daily reflection"
        return f"From {where}: {flat}"


def _daily_payload(client: Any, date: Optional[str]) -> Dict[str, Any]:
    getter = getattr(client, "get_daily_reflection", None)
    if not callable(getter):
        return {}
    try:
        payload = getter(date=date) or {}
    except Exception:  # noqa: BLE001 -- reflections are context, never worth failing a caller over
        log.info("reflections: daily reflection read failed (date=%s)", date, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def _period_payload(client: Any, period: str, use_previous: bool) -> Dict[str, Any]:
    getter = getattr(client, "get_period_reflection", None)
    if not callable(getter):
        return {}
    try:
        payload = getter(period, use_previous=use_previous) or {}
    except Exception:  # noqa: BLE001
        log.info("reflections: %s period reflection read failed (use_previous=%s)",
                 period, use_previous, exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_reflections(client: Any, *,
                        include_daily: bool = True,
                        periods: Sequence[str] = DEFAULT_PERIODS,
                        use_previous: bool = False,
                        previous_fallback: bool = True,
                        lookback_days: int = 2,
                        now: Optional[datetime] = None) -> ReflectionContext:
    """Read the person's latest daily and period reflections into one ``ReflectionContext``.

    ``client`` is a ``QuestClient`` (or anything exposing ``get_daily_reflection`` /
    ``get_period_reflection``). A client without those methods yields an empty context rather than
    an AttributeError, so an older or partial client stays usable.

    Daily: today's entry first. When the person has not filled today's in yet (common in the
    morning, and the exact case that made an autopilot pass look blind), walk back up to
    ``lookback_days`` days by explicit date and take the most recent entry that has text. Dates are
    computed in UTC; the no-date call uses the user's own timezone server-side, so the walk-back is
    a fallback, not the primary path.

    Period: ``periods`` in the order given, first review that actually has text wins -- nothing here
    presumes a week matters more than a quarter, the CALLER decides. When no requested period has a
    submitted review and ``previous_fallback`` is on, the same list is retried for the PREVIOUS
    period, because early in a period the newest thing the person wrote is last period's review.
    """
    ctx = ReflectionContext()
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    if include_daily:
        candidates: List[Optional[str]] = [None]
        candidates += [(at - timedelta(days=d)).strftime("%Y-%m-%d")
                       for d in range(1, max(0, lookback_days) + 1)]
        for date in candidates:
            payload = _daily_payload(client, date)
            review = _clip(payload.get("yesterday_review"))
            plan = _clip(payload.get("today_plan"))
            if review or plan:
                ctx.daily_date = str(payload.get("date") or date or "")
                ctx.yesterday_review = review
                ctx.today_plan = plan
                break

    wanted = [str(p).strip().lower() for p in (periods or ()) if str(p).strip()]
    passes = [use_previous] + ([not use_previous] if previous_fallback else [])
    for previous in passes:
        for period in wanted:
            if period not in ctx.checked_periods:
                ctx.checked_periods.append(period)
            payload = _period_payload(client, period, previous)
            if not payload.get("has_review"):
                continue
            past = _clip(payload.get("reflection_past"))
            future = _clip(payload.get("reflection_future"))
            if not (past or future):
                # A review row exists but the person left both boxes empty. Nothing to carry, and
                # treating it as a hit would stop the search on an empty answer.
                continue
            ctx.period = period
            ctx.period_label = f"the previous {period}" if previous else f"the current {period}"
            ctx.period_past = past
            ctx.period_future = future
            return ctx
    return ctx
