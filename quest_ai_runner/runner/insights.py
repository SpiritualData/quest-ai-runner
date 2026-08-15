"""The person's own captured insights, collected into one readable, tagged block.

Quest gives every person an "Insights" collection: a quick-capture space for the idea or
realization that arrives away from any goal ("the mornings are the only time the writing happens",
"stop scheduling calls before noon"). Each capture carries the text itself, the free-text
**category tags** the person chose for it, and an ``acted_on`` checkbox they tick once something
came of it.

That is the one channel in Quest where a person records something they have NOT yet turned into a
goal or a task. Everything a background pass otherwise reads is a row the system already recorded,
so an insight captured on Tuesday could sit untouched while every pass since composed its brief as
if it had never been written.

**The tags are context for the reader, never a filter for this code.** Nothing here matches a
category against a quest name, a goal title, or any other string. The tags are rendered next to
each insight exactly as the person typed them, and the model composing the run decides which (if
any) bear on the quest in front of it, the same way it judges the relevance of goals and
reflections. A hardcoded match would silently drop every insight whose tag the code did not
anticipate ("dissertation" vs. "thesis" vs. no tag at all), which is the failure mode this
repository's hard rule #3 exists to prevent.

Like ``runner.reflections``, every function here is best-effort and never raises: a client without
these methods, a 404, an empty collection, or a transport failure all degrade to an EMPTY context.
A person who has captured nothing is the normal case, not an error.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("quest-ai-runner.insights")

# Field ids in Quest's auto-created Insights collection schema (quest-backend's
# ``app/services/insights_service.py``). They are stable ids, not display names: a person can
# rename the collection or its columns and these do not change.
INSIGHT_FIELD_ID = "insight"
INSIGHT_CATEGORIES_FIELD_ID = "categories"
INSIGHT_ACTED_ON_FIELD_ID = "acted_on"
INSIGHT_ACTION_TAKEN_FIELD_ID = "action_taken"

# How far back to look when the caller gives no explicit cutoff, and the outer bound on any cutoff.
# An insight from three months ago that is still unacted is a fact about the person's backlog, not
# news for this run; carrying it in every brief forever would train the reader to skim past all of
# them.
DEFAULT_WINDOW_DAYS = 14

# How many insights a composed block may carry, newest first. The batch text already holds the
# goals, the plan of record, and a reflection; a capture spree must not push the actual instruction
# out of the model's attention.
MAX_INSIGHTS = 8

# Per-insight character cap, and the page size / page count used to walk the entries endpoint.
MAX_INSIGHT_CHARS = 400
ENTRY_PAGE_LIMIT = 50
MAX_PAGES = 3


def _clip(text: Any, limit: int = MAX_INSIGHT_CHARS) -> str:
    """A stripped string, truncated with an explicit marker so nothing silently disappears."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + " [...truncated]"


def _parse_created(raw: Any) -> Optional[datetime]:
    """The entry's ``created_at`` as an aware UTC datetime, or None when it cannot be read.

    Handles both shapes quest-backend can hand back for the same field: an ISO string over HTTP
    (tolerating a trailing ``Z``) and a real ``datetime`` from an in-process caller. A naive value
    is read as UTC; an aware one is CONVERTED rather than stamped, so an entry written at
    ``2026-08-12T23:30:00-07:00`` compares as the next UTC day it actually is.
    """
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _categories(raw: Any) -> List[str]:
    """The person's own tags for one insight, as a clean list.

    The field is typed ``tags`` and normally arrives as a list, but a comma-separated string is
    accepted too (quick capture and voice capture have both produced one). Nothing is lowercased or
    normalized: these are the person's words, and they are shown back exactly as written.
    """
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple, set)):
        parts = list(raw)
    else:
        return []
    return [t for t in (str(p).strip() for p in parts) if t]


def _acted_on(raw: Any) -> bool:
    """Whether the ``acted_on`` checkbox is ticked, tolerating the string forms a round-trip leaves.

    This reads a value the PERSON set in their own collection, not anything a model generated.
    """
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes", "on"}
    return bool(raw)


def _field_values(entry: Dict[str, Any]) -> Dict[str, Any]:
    """An entry's ``field_values``, under either casing.

    The HTTP response serializes camelCase (``fieldValues``); storage and in-process callers use
    snake_case. Reading only one of the two is how this would work in a test and return nothing in
    production.
    """
    for key in ("field_values", "fieldValues"):
        value = entry.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _created_raw(entry: Dict[str, Any]) -> Any:
    for key in ("created_at", "createdAt"):
        if entry.get(key):
            return entry[key]
    return None


@dataclass
class Insight:
    """One capture: what the person wrote, the tags they chose, and when.

    ``entry_id`` is kept for the write-back path (``QuestClient.mark_insight_acted_on``) and for
    logs, but is deliberately NOT rendered into a prompt: an opaque id in a brief is noise to a
    reader who has no way to use it.
    """
    entry_id: str = ""
    text: str = ""
    categories: List[str] = field(default_factory=list)
    created_at: Optional[datetime] = None

    @property
    def date_label(self) -> str:
        return self.created_at.strftime("%Y-%m-%d") if self.created_at else "an unrecorded date"

    def as_line(self) -> str:
        tags = ", ".join(self.categories)
        head = f"  - [{self.date_label}]" + (f" tagged {tags}" if tags else " (untagged)")
        return f"{head}\n      {self.text}"


@dataclass
class InsightsContext:
    """The person's recent unacted insights, ready to render into a prompt.

    ``collection_id`` is carried so a caller that DOES act on one can close the loop through
    ``QuestClient.mark_insight_acted_on`` without a second lookup.
    """
    insights: List[Insight] = field(default_factory=list)
    collection_id: str = ""
    # The cutoff this context was collected against, and the widest window that was asked for.
    # Kept so a caller can say "nothing new since your last pass" rather than "nothing on record" —
    # they are different pieces of information.
    since: Optional[datetime] = None
    window_days: int = DEFAULT_WINDOW_DAYS
    # The oldest instant this context could ever contain (``now - window_days`` at collect time).
    # A narrowing cutoff older than this is clamped to it, so the rendered label never claims a
    # reach the fetch did not have.
    window_start: Optional[datetime] = None

    def has_any(self) -> bool:
        return bool(self.insights)

    def narrow_to(self, cutoff: Optional[datetime]) -> "InsightsContext":
        """A copy holding only insights captured after ``cutoff``.

        This is what makes one fetch serve a whole pass. Insights are USER-scoped, so they are read
        once; but "since the last time it ran" is a PER-QUEST question (each quest carries its own
        ``last_pass_at``), so the narrowing happens here, in memory, instead of costing another
        round trip per quest. ``None`` means no narrowing: a quest that has never had a pass should
        see the whole window rather than nothing.
        """
        if cutoff is None:
            return self
        at = cutoff.astimezone(timezone.utc)
        if self.window_start is not None and at < self.window_start:
            # The fetch never reached that far back, so narrowing to it changes nothing and
            # labelling it that way would overstate what is in the list.
            at = self.window_start
            kept = list(self.insights)
        else:
            kept = [i for i in self.insights if i.created_at is None or i.created_at > at]
        return replace(self, insights=kept, since=at)

    def as_text(self) -> str:
        """The block a batch prompt can carry, or "" when there is nothing to say.

        The closing instruction is the load-bearing part. It states plainly that the tags are the
        person's labels for their own thinking rather than a routing rule, and hands the relevance
        judgment to the reader — because the alternative (this code deciding which tags "belong" to
        which quest) is a fixed string match that silently drops everything it did not anticipate.
        """
        if not self.has_any():
            return ""
        when = (f"since {self.since.strftime('%Y-%m-%d')}" if self.since
                else f"in the last {self.window_days} days")
        lines = [f"Insights the person captured on Quest {when} and has not yet marked acted on, "
                 f"in their own words, with the category tags they chose:"]
        lines += [i.as_line() for i in self.insights]
        lines.append(
            "Those are the person's own captures, not work items filed against this quest, and the "
            "tags are how they label their own thinking rather than a routing rule. Judge for "
            "yourself which of them (if any) bear on the goals above: one tagged for something else "
            "can still matter here, and one whose tag looks like a match can be irrelevant. Where "
            "an insight does apply, act on it and say so in your result. Pass over the rest without "
            "comment.")
        return "\n".join(lines)

    def one_line(self, limit: int = 220) -> str:
        """A single condensed line, for an artifact that holds one line of context."""
        if not self.has_any():
            return ""
        newest = self.insights[0]
        text = newest.text
        if len(text) > limit:
            text = text[:limit].rstrip() + "..."
        more = f" (+{len(self.insights) - 1} more)" if len(self.insights) > 1 else ""
        tags = f" [{', '.join(newest.categories)}]" if newest.categories else ""
        return f"Unacted insight from {newest.date_label}{tags}: {text}{more}"


def _collection_id(client: Any) -> str:
    getter = getattr(client, "get_insights_collection", None)
    if not callable(getter):
        return ""
    try:
        payload = getter() or {}
    except Exception:  # noqa: BLE001 -- insights are context, never worth failing a caller over
        log.info("insights: could not read the Insights collection", exc_info=True)
        return ""
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("id") or payload.get("_id") or "")


def _entry_page(client: Any, collection_id: str, page: int, limit: int) -> Dict[str, Any]:
    getter = getattr(client, "list_collection_entries", None)
    if not callable(getter):
        return {}
    try:
        payload = getter(collection_id, page=page, limit=limit) or {}
    except Exception:  # noqa: BLE001
        log.info("insights: entry page %s read failed for collection %s", page, collection_id,
                 exc_info=True)
        return {}
    return payload if isinstance(payload, dict) else {}


def collect_unacted_insights(client: Any, *,
                             since: Optional[datetime] = None,
                             days_cap: int = DEFAULT_WINDOW_DAYS,
                             max_insights: int = MAX_INSIGHTS,
                             page_limit: int = ENTRY_PAGE_LIMIT,
                             max_pages: int = MAX_PAGES,
                             now: Optional[datetime] = None) -> InsightsContext:
    """Read the person's recent, still-unacted insights into one ``InsightsContext``.

    ``client`` is a ``QuestClient`` (or anything exposing ``get_insights_collection`` and
    ``list_collection_entries``). A client without those methods yields an empty context rather
    than an AttributeError, so an older or partial client stays usable.

    The entries endpoint has NO server-side filter for either condition, so both are applied here,
    mirroring quest-backend's own ``_get_recent_unacted_insights``: skip anything whose ``acted_on``
    checkbox is ticked, and anything captured before the cutoff. The cutoff is the LATER of ``since``
    and ``now - days_cap`` — a caller passing a very old "last time I ran" still gets a bounded,
    recent list rather than the whole history.

    Entries come back newest-first, so paging stops as soon as a page reaches past the cutoff (and,
    regardless, at ``max_pages`` or once ``max_insights`` are in hand). An entry with an unreadable
    timestamp is KEPT rather than dropped, matching the backend: a capture with a malformed date is
    still something the person wrote.
    """
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = at - timedelta(days=max(0, days_cap))
    cutoff = window_start
    if since is not None:
        since_utc = since.astimezone(timezone.utc)
        cutoff = max(window_start, since_utc)

    ctx = InsightsContext(since=cutoff if since is not None else None,
                          window_days=max(0, days_cap),
                          window_start=window_start)
    collection_id = _collection_id(client)
    if not collection_id:
        return ctx
    ctx.collection_id = collection_id

    collected: List[Insight] = []
    for page in range(max(1, max_pages)):
        payload = _entry_page(client, collection_id, page, page_limit)
        items = payload.get("items") or payload.get("entries") or []
        if not isinstance(items, list) or not items:
            break
        reached_past_cutoff = False
        for entry in items:
            if not isinstance(entry, dict):
                continue
            values = _field_values(entry)
            if _acted_on(values.get(INSIGHT_ACTED_ON_FIELD_ID)):
                continue
            created = _parse_created(_created_raw(entry))
            if created is not None and created <= cutoff:
                # Newest-first ordering means everything after this is older too.
                reached_past_cutoff = True
                continue
            text = _clip(values.get(INSIGHT_FIELD_ID))
            if not text:
                continue
            collected.append(Insight(
                entry_id=str(entry.get("id") or entry.get("_id") or ""),
                text=text,
                categories=_categories(values.get(INSIGHT_CATEGORIES_FIELD_ID)),
                created_at=created,
            ))
            if len(collected) >= max(1, max_insights):
                break
        if len(collected) >= max(1, max_insights) or reached_past_cutoff:
            break
        pagination = payload.get("pagination") or {}
        has_next = pagination.get("has_next", pagination.get("hasNext"))
        if has_next is False:
            break

    ctx.insights = collected
    return ctx
