"""The insights assigned to ONE quest, collected into a readable block.

Quest gives every person an "Insights" collection: a quick-capture space for the idea or
realization that arrives away from any goal ("the mornings are the only time the writing happens",
"stop scheduling calls before noon"). Each capture carries the text itself, the free-text
**category tags** the person chose for it, an ``acted_on`` checkbox they tick once something came
of it, and, since quest-backend assigns them at capture time, the **quest ids** it belongs to
(``field_values["quests"]``).

That assignment is what context assembly reads. A run asks for the latest insights of the quest in
front of it (``QuestClient.list_quest_insights``, one route, filtered server-side to that quest and
to unacted) and nothing else. There is deliberately NO function here that reviews all of a
person's unacted insights: pulling in captures that were assigned elsewhere put unrelated material
in front of every run and re-clarified the same insights on every request and pass. A request with
no quest gets no insights.

**The tags are context for the reader, never a filter for this code.** Nothing here matches a
category against a quest name, a goal title, or any other string; relevance was decided when the
insight was assigned. The tags are rendered next to each insight exactly as the person typed them
(hard rule #3 in this repository's CLAUDE.md: never keyword-match model or user text to decide
anything).

Like ``runner.reflections``, every function here is best-effort and never raises: a client without
the method, an older backend (404), an empty list, or a transport failure all degrade to an EMPTY
context. A quest with no assigned insights is the normal case, not an error.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger("quest-ai-runner.insights")

# Field ids in Quest's auto-created Insights collection schema (quest-backend's
# ``app/services/insights_service.py``). They are stable ids, not display names: a person can
# rename the collection or its columns and these do not change.
INSIGHT_FIELD_ID = "insight"
INSIGHT_CATEGORIES_FIELD_ID = "categories"
INSIGHT_ACTED_ON_FIELD_ID = "acted_on"

# How far back to look when the caller gives no explicit cutoff, and the outer bound on any cutoff.
# An insight from three months ago that is still unacted is a fact about the person's backlog, not
# news for this run; carrying it in every brief forever would train the reader to skim past all of
# them.
DEFAULT_WINDOW_DAYS = 14

# How many insights a composed block may carry, newest first. The batch text already holds the
# goals, the plan of record, and a reflection; a capture spree must not push the actual instruction
# out of the model's attention.
MAX_INSIGHTS = 8

# Per-insight character cap.
MAX_INSIGHT_CHARS = 400


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


# The framing around a rendered capture list. Shared with ``runner.context_updates`` so captures
# delivered per row through the engine's slot read exactly as the block always has.
BLOCK_FOOTER = (
    "These were assigned to this quest when the person captured them, so none needs a relevance "
    "guess. The tags are how they label their own thinking, not a routing rule. Where an "
    "insight applies to the work in front of you, act on it and say so in your result. If "
    "one cannot be acted on yet, say why in a sentence.")


WITHHELD_LINE = ("({count} more assigned to this quest were held back because the quest is shared and "
                 "they look personal. Do not ask for them or guess at them.)")


def block_header(since: Optional[datetime], window_days: int = DEFAULT_WINDOW_DAYS) -> str:
    when = (f"since {since.strftime('%Y-%m-%d')}" if since
            else f"in the last {window_days} days")
    return (f"Insights the person captured on Quest {when}, assigned to THIS quest, not yet "
            f"marked acted on, in their own words, with the category tags they chose:")


@dataclass
class InsightsContext:
    """One quest's recent unacted insights, ready to render into a prompt.

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
    # The oldest instant this context could contain (``now - window_days`` at collect time).
    window_start: Optional[datetime] = None
    # The quest these insights were assigned to.
    quest_id: str = ""
    # How many assigned insights the server held back because this quest is shared and they read as
    # personal or sensitive (the ``withheld`` count of the by-quest route). The run is told, so it
    # neither guesses at them nor reports "no insights".
    withheld: int = 0

    def has_any(self) -> bool:
        return bool(self.insights)

    def as_text(self) -> str:
        """The block a batch prompt can carry, or "" when there is nothing to say.

        The closing instruction is the load-bearing part: these were assigned to this quest at
        capture, so the reader acts on them rather than guessing whether they belong.
        """
        if not self.has_any():
            return ""
        lines = [block_header(self.since, self.window_days)]
        lines += [i.as_line() for i in self.insights]
        if self.withheld:
            lines.append(WITHHELD_LINE.format(count=self.withheld))
        lines.append(BLOCK_FOOTER)
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


def collect_quest_insights(client: Any, quest_id: str, *,
                           since: Optional[datetime] = None,
                           days_cap: int = DEFAULT_WINDOW_DAYS,
                           max_insights: int = MAX_INSIGHTS,
                           now: Optional[datetime] = None) -> InsightsContext:
    """Read the latest unacted insights assigned to ``quest_id`` into one ``InsightsContext``.

    ``client`` is a ``QuestClient`` (or anything exposing ``list_quest_insights``). A blank
    ``quest_id``, a client without the method, an older backend (the route answers 404, which the
    client reports as ``{}``) or a transport failure yields an EMPTY context, never an exception.
    Only the quest route is called: the person's other insights are never read.

    The server already filters to the quest and to unacted, newest first. Both the window and the
    acted check are applied again here, defensively: skip anything whose ``acted_on`` checkbox is
    ticked, and anything captured at or before the cutoff, the LATER of ``since`` and
    ``now - days_cap``. An entry with an unreadable timestamp is KEPT: a capture with a malformed
    date is still something the person wrote. Output is newest first and bounded by
    ``max_insights``.
    """
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_start = at - timedelta(days=max(0, days_cap))
    cutoff = window_start
    if since is not None:
        cutoff = max(window_start, since.astimezone(timezone.utc))
    qid = str(quest_id or "").strip()
    ctx = InsightsContext(since=cutoff if since is not None else None,
                          window_days=max(0, days_cap), window_start=window_start,
                          quest_id=qid)
    getter = getattr(client, "list_quest_insights", None)
    if not qid or not callable(getter):
        return ctx
    limit = max(1, max_insights)
    try:
        payload = getter(qid, limit=limit, include_acted=False) or {}
    except Exception:  # noqa: BLE001 -- insights are context, never worth failing a caller over
        log.info("insights: could not read the insights of quest %s", qid, exc_info=True)
        return ctx
    if not isinstance(payload, dict):
        return ctx
    ctx.collection_id = str(payload.get("collection_id") or payload.get("collectionId") or "")
    try:
        ctx.withheld = max(0, int(payload.get("withheld") or 0))
    except (TypeError, ValueError):
        ctx.withheld = 0
    items = payload.get("items") or []
    if not isinstance(items, list):
        return ctx

    rows: List[Insight] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        values = _field_values(entry)
        if _acted_on(values.get(INSIGHT_ACTED_ON_FIELD_ID)):
            continue
        created = _parse_created(_created_raw(entry))
        if created is not None and created <= cutoff:
            continue
        text = _clip(values.get(INSIGHT_FIELD_ID))
        if not text:
            continue
        rows.append(Insight(
            entry_id=str(entry.get("id") or entry.get("_id") or ""),
            text=text,
            categories=_categories(values.get(INSIGHT_CATEGORIES_FIELD_ID)),
            created_at=created,
        ))
    # Newest first regardless of the order the server used; an undated row sorts last.
    floor = datetime.min.replace(tzinfo=timezone.utc)
    rows.sort(key=lambda i: i.created_at or floor, reverse=True)
    ctx.insights = rows[:limit]
    return ctx
