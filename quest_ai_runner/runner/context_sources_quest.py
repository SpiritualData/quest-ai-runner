"""Context sources that read Quest itself: notes, goal updates, decisions, events, reflections, insights, collections.

Split out of ``context_updates.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence
from datetime import datetime, timedelta, timezone

from .context_update_types import (
    ContextUpdate,
    KIND_UNKNOWN_SENTINEL,
    MAX_OPEN_PER_SOURCE,
    OPEN_ITEM_MAX_AGE_DAYS,
    _as_utc,
    _clip,
    _utcnow,
    log,
)
from .context_sources_base import _BaseSource, _notes_explanation, _reply_channel, _runs_delivered
from .context_update_state import CollectRequest, _card_label


class QuestNotesSource(_BaseSource):
    """Notes the PERSON added to this quest that no assistant has answered yet.

    Only the person's own notes, and that is the point: a quest an assistant writes a summary note
    to every day would otherwise report its own output back to itself as news. Attribution comes
    from the backend's ``author_kind``; a note with none is left out of the updates rather than
    guessed at, since asserting an unattributed note is the person's instruction is the one error
    with real consequences here.

    OPEN UNTIL ANSWERED, same rule as ``DriveCommentsSource``. A person's note is answered once an
    assistant note follows it on the quest, OR once a run on this quest delivered its result after
    it. Both count, because they are the two places an assistant's answer actually lands, and
    which one it lands in is not the person's concern. A note with neither after it is still
    waiting however old it is. Three live failures this replaces: a first look offered ten notes
    answered days earlier, every one marked "needs an answer"; a time-filtered note was lost for
    good the moment one pass saw it and did nothing; and a note answered in a run's RESULT (the
    text the person reads in their inbox, on a quest whose mail is on) stayed open forever,
    because only a note on the quest counted and the answer was never written as one.

    WHERE AN ANSWER GOES is the quest's own delivery setting, not this source's choice. On a quest
    that mails, the run's result IS the message the person reads, so that is where an answer
    belongs and a note is only the internal record. On a quest that does not mail, a note on the
    quest is the channel. ``how_to_respond`` says whichever is true, because a run told to answer
    somewhere the person never reads has not answered at all.

    AND NEWER THAN THE WATERMARK, even when an assistant note follows it. The watermark moves only
    when a run was handed the notes, so a note newer than it has never been in front of any run.
    An assistant note after it does not change that: a run that started before the note arrived
    and posted its summary an hour later never saw it, and treating that summary as the answer is
    the one way this source could lose a note for good. Such a note is offered once, without the
    "needs an answer" flag, so the run can see it was written and judge whether the note after it
    actually answered it. Not on a first look: with no delivery on record, "newer than the
    watermark" is just "recent", and the answered notes of the last two weeks are history.
    """
    name = "quest_notes"
    describes = "notes the person added to this quest that have no assistant reply yet"
    tracks_asks = True

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        quest_id = request.card_id
        lister = getattr(client, "list_quest_notes", None)
        if not callable(lister) or not quest_id:
            return []
        out: List[ContextUpdate] = []
        where = request.card_label or _card_label(request.card) or "this quest"
        notes = lister(quest_id) or []
        delivered = _runs_delivered(request)
        answer_here = _reply_channel(request.card)
        open_notes = self.open_notes(notes, delivered_at=delivered)
        offered = _still_open(open_notes, request.now, lambda n: _as_utc(n.get("created_at")))
        open_ids = {id(n) for n in offered}
        persons = self.persons(notes)
        if not request.first_look:
            answered = [n for n in persons if id(n) not in {id(o) for o in open_notes}]
            for n in answered:
                when = _as_utc(n.get("created_at"))
                if not (request.since and when and when <= request.since):
                    offered.append(n)          # arrived after the last delivery: never seen
        request.account(len(persons), _notes_explanation(len(persons), len(offered),
                                                         len(open_notes)))
        for note in offered:
            when = _as_utc(note.get("created_at"))
            is_new = not (request.since and when and when <= request.since)
            is_open = id(note) in open_ids
            # A RECORDED status outranks the circumstantial reading. The inference above is doing
            # its best from timestamps; the ledger holds what a run actually said it did, and
            # "somebody replied afterwards" was never evidence that this was handled.
            tracked = request.recorded(str(note.get("id") or note.get("note_id") or ""))
            if tracked is not None and tracked.kind != KIND_UNKNOWN_SENTINEL:
                is_open = tracked.is_open
            text = str(note.get("text") or "").strip()
            author = str(note.get("author_name") or "").strip()
            title = f"{author or 'The person'} wrote on the quest"
            if not is_new:
                title += " (still open from before)"
            elif not is_open:
                title += " (an assistant has answered since; check that it answered THIS)"
            if tracked is not None and tracked.kind != KIND_UNKNOWN_SENTINEL:
                title = f"{author or 'The person'} wrote on the quest ({tracked.status_line()})"
            out.append(ContextUpdate(
                source=self.name,
                kind="note",
                item_id=str(note.get("id") or note.get("note_id") or ""),
                title=title,
                body=text,
                verbatim=True,  # quest note: their own words
                excerpt=text,
                author=author,
                occurred_at=when,
                location=where,
                how_to_respond=answer_here,
                needs_response=is_open,
                raw=dict(note or {}),
            ))
        return out

    @staticmethod
    def persons(notes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The person's own notes with text, oldest first (by ``created_at``, then list order)."""
        return [n for n in _by_time(notes) if str(n.get("author_kind") or "").lower() == "user"]

    @staticmethod
    def open_notes(notes: Sequence[Dict[str, Any]],
                   delivered_at: Sequence[datetime] = ()) -> List[Dict[str, Any]]:
        """The person's notes nothing has answered yet, oldest first, unbounded.

        Answered means either of the two places an answer lands: an assistant NOTE after it on the
        quest, or a RUN that delivered its result after it (``delivered_at``). The second is what
        a quest with mail switched on actually does, and without it every emailed answer left the
        note it answered looking untouched.
        """
        open_notes: List[Dict[str, Any]] = []
        for note in _by_time(notes):
            kind = str(note.get("author_kind") or "").lower()
            if kind == "user":
                open_notes.append(note)
            elif kind == "ai":
                open_notes = []
        if not delivered_at or not open_notes:
            return open_notes
        newest_delivery = max(delivered_at)
        return [n for n in open_notes
                if (_as_utc(n.get("created_at")) or newest_delivery) > newest_delivery]

    @classmethod
    def unanswered(cls, notes: Sequence[Dict[str, Any]],
                   now: Optional[datetime] = None,
                   delivered_at: Sequence[datetime] = ()) -> List[Dict[str, Any]]:
        """``open_notes`` bounded to the ones still worth offering (see ``_still_open``)."""
        return _still_open(cls.open_notes(notes, delivered_at), now,
                           lambda n: _as_utc(n.get("created_at")))


def _by_time(notes: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Notes with text, oldest first by ``created_at``; an unreadable timestamp keeps list order."""
    rows = []
    for i, note in enumerate(notes or []):
        if not isinstance(note, dict) or not str(note.get("text") or "").strip():
            continue
        rows.append((_as_utc(note.get("created_at")) or datetime.min.replace(tzinfo=timezone.utc),
                     i, note))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [n for _, _, n in rows]


def _still_open(items: List[Any], now: Optional[datetime],
                when_of: Callable[[Any], Optional[datetime]]) -> List[Any]:
    """The open items still worth offering: none older than ``OPEN_ITEM_MAX_AGE_DAYS``, and at
    most the newest ``MAX_OPEN_PER_SOURCE``. ``items`` oldest first; an undated item counts as
    fresh. Without this, one unanswered note from last year rides into every brief forever."""
    if not items:
        return []
    floor = (now or _utcnow()) - timedelta(days=OPEN_ITEM_MAX_AGE_DAYS)
    fresh = [it for it in items if (when_of(it) or floor) >= floor]
    return fresh[-MAX_OPEN_PER_SOURCE:]


def _flatten_goal_titles(payload: Dict[str, Any]) -> Dict[str, str]:
    """{goal_id: goal title}, out of ``list_quest_goals``'s period-grouped shape.

    The endpoint groups goals by time scope (today, this week, this quarter, ...) for a person to
    read; a goal update names one goal by id and does not care which group it lives in, so this
    flattens the grouping away once, here, rather than in every caller that only wants a title.
    """
    titles: Dict[str, str] = {}
    for group in (payload or {}).get("period_groups") or []:
        for goal in (group or {}).get("goals") or []:
            if not isinstance(goal, dict):
                continue
            goal_id = str(goal.get("id") or "").strip()
            if goal_id:
                titles[goal_id] = str(goal.get("name") or "").strip()
    return titles


def _goal_updates_explanation(total: int, offered: int) -> str:
    """Why fewer updates were offered than were found, in a person's words."""
    if not total or offered:
        return ""
    return f"{total} update(s), none new since the last look"


class GoalUpdatesSource(_BaseSource):
    """Check-ins the person wrote on individual goals of this quest, one row per update.

    WHAT THIS FIXES. A goal update is the per-goal equivalent of a note on the quest as a whole: the
    one place a person writes, on ONE goal, what they did or found. Before this source, nothing in
    this library ever read one, on any quest, so an autopilot pass composing today's brief saw a
    goal's name, deadline and done/not-done flag but never the person's own account of where it
    actually stood -- and could cheerfully re-propose a plan their last check-in had already
    overtaken.

    CARD-SCOPED, SO NEVER JUDGED (``judge_relevance = False``). An update is written ON a goal that
    belongs to THIS quest, the same reasoning that keeps ``QuestNotesSource`` out of the relevance
    judge: relevance here comes from WHERE the words were written, not from a model's opinion of
    them, and putting a card's own goal updates to a judgment could only ever lose one that plainly
    belonged.

    NOT AN ASK (``tracks_asks = False``). A check-in is the person recording where things stand for
    their own record, not a question they left open for an assistant to close. See the comment on
    ``_BaseSource.tracks_asks``: a live run once carried the habit log and the daily reflection as
    asks, and the next morning both came back "still owed, needs an answer" -- an assistant asking a
    person to answer their own diary. A goal update is the same shape of channel, so it is offered
    as news the run should read and act on, never as something owed a reply.

    A GOAL'S NAME COSTS A SECOND CALL. ``list_quest_goal_updates`` / ``list_goal_updates`` return a
    bare ``goalId``; without the quest's own goal list, a row could only say "an update on goal
    gl_xyz", which tells a run nothing it can act on. So this source reads ``list_quest_goals``
    first and flattens its period-grouped goals into an id -> title map, used only to LABEL rows --
    a goal missing from that map (renamed, deleted, or the method simply absent) still gets its
    update offered, just under a generic label instead of a name.

    ONE CALL WHERE THE BACKEND OFFERS IT, A FAN-OUT WHERE IT DOES NOT. ``list_quest_goal_updates``
    returns every goal's updates for the quest in one round trip; a client too old to have it still
    works, through ``list_goal_updates`` called once per goal id known from ``list_quest_goals``, at
    the cost of one call per goal. A client with neither method is not an error, just a quest this
    source cannot help yet: no rows, no exception.
    """
    name = "goal_updates"
    describes = "check-ins the person wrote on this quest's individual goals"
    judge_relevance = False
    tracks_asks = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        quest_id = request.card_id
        if not quest_id:
            return []
        bulk_lister = getattr(client, "list_quest_goal_updates", None)
        per_goal_lister = getattr(client, "list_goal_updates", None)
        if not callable(bulk_lister) and not callable(per_goal_lister):
            return []
        titles = self._goal_titles(request, quest_id)
        updates_by_goal = self._updates_by_goal(
            request, quest_id, titles, bulk_lister, per_goal_lister)
        total = sum(len(v or []) for v in updates_by_goal.values())
        out: List[ContextUpdate] = []
        for goal_id, updates in updates_by_goal.items():
            where = titles.get(goal_id) or "a goal on this quest"
            for update in updates or []:
                if not isinstance(update, dict):
                    continue
                when = _as_utc(update.get("createdAt"))
                if request.since and when and when <= request.since:
                    continue                   # not new: seen (or in scope) on an earlier look
                text = str(update.get("note") or "").strip()
                if not text:
                    continue
                author = str(update.get("userName") or "").strip()
                out.append(ContextUpdate(
                    source=self.name,
                    kind="goal update",
                    item_id=str(update.get("updateId") or ""),
                    title=f'{author or "Someone"} wrote an update on the goal "{_clip(where, 100)}"',
                    body=text,
                    verbatim=True,  # goal update: their own words
                    excerpt=text,
                    author=author,
                    occurred_at=when,
                    location=where,
                    needs_response=False,
                    how_to_respond=("act on it in the work for this goal and say what you did in "
                                    "your result; you can also reply by posting a goal update on "
                                    "this goal"),
                    raw=dict(update or {}),
                ))
        request.account(total, _goal_updates_explanation(total, len(out)))
        return out

    @staticmethod
    def _goal_titles(request: CollectRequest, quest_id: str) -> Dict[str, str]:
        """This quest's {goal_id: title}, read once per engine pass (see ``_runs_delivered``)."""
        lister = getattr(request.client, "list_quest_goals", None)
        if not callable(lister):
            return {}
        key = f"goal_updates:goals:{quest_id}"
        if key not in request.cache:
            try:
                request.cache[key] = _flatten_goal_titles(lister(quest_id) or {})
            except Exception as e:  # noqa: BLE001 -- a title is a nicety, never a reason to fail
                log.info("context updates: could not read goals for %s (%s)", quest_id, e)
                request.cache[key] = {}
        return request.cache[key]

    @staticmethod
    def _updates_by_goal(request: CollectRequest, quest_id: str, titles: Dict[str, str],
                         bulk_lister: Any, per_goal_lister: Any) -> Dict[str, List[Dict[str, Any]]]:
        """{goal_id: [update, ...]}, via the one-call route when the client has it."""
        key = f"goal_updates:updates:{quest_id}"
        if key in request.cache:
            return request.cache[key]
        limit_per_goal = int(request.opt("limit_per_goal") or 20)
        result: Dict[str, List[Dict[str, Any]]] = {}
        if callable(bulk_lister):
            try:
                result = dict(bulk_lister(quest_id, limit_per_goal=limit_per_goal) or {})
            except Exception as e:  # noqa: BLE001 -- one broken read never costs the rest
                log.info("context updates: could not read goal updates for %s (%s)", quest_id, e)
                result = {}
        elif callable(per_goal_lister):
            limit = int(request.opt("limit") or limit_per_goal)
            for goal_id in titles:
                try:
                    result[goal_id] = list(per_goal_lister(goal_id, limit=limit) or [])
                except Exception as e:  # noqa: BLE001 -- one goal's failure never costs the others
                    log.info("context updates: could not read updates for goal %s (%s)",
                            goal_id, e)
        request.cache[key] = result
        return result


class DecisionsSource(_BaseSource):
    """Asks sent to people on this quest: the ones still open, and the answers to the ones settled.

    WHAT THIS FIXES. A decision is where someone outside the work asks the person for a call
    (approve, decline, answer), and the answer lives on the decision row, not on the quest. Nothing
    in this library read decisions before, so a run or an autopilot pass could not see that a
    question was still waiting, or that it had been declined and why, and could re-raise a settled
    question or re-propose a declined one.

    OPEN ASKS ARE OFFERED ON EVERY LOOK, RESOLVED ONES ONLY WHEN NEW. An open ask is owed an answer
    however old it is, so it keeps being offered (bounded by ``MAX_OPEN_PER_SOURCE``, newest first).
    A resolved ask is history except on the look that first sees its resolution, so it is offered
    only when resolved after the watermark, and never on a first look.

    NOT AN ASK FOR THE RUN (``tracks_asks = False``). The person the ask went to answers it on the
    decision itself; a run's result does not resolve it. The backend row is the source of truth for
    status, so the feedback ledger would only hold a stale copy of it.

    CARD-SCOPED, SO NEVER JUDGED. A decision row names this quest, the same reason notes are not
    judged.
    """
    name = "decisions"
    describes = ("asks sent to people on this quest: still open, answered since the last look, or "
                 "answered and waiting for the AI to act")
    judge_relevance = False
    tracks_asks = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        quest_id = request.card_id
        lister = getattr(client, "list_decisions_for_quest", None)
        if not callable(lister) or not quest_id:
            return []
        try:
            rows = [r for r in (lister(quest_id) or []) if isinstance(r, dict)]
        except Exception as e:  # noqa: BLE001 -- one broken read never costs the rest of a bundle
            log.info("context updates: could not read decisions for %s (%s)", quest_id, e)
            return []
        where = request.card_label or _card_label(request.card) or "this quest"
        statuses = [str(r.get("status") or "").strip().lower() for r in rows]
        opens = [r for r, s in zip(rows, statuses) if s == "open"]
        settled = [r for r, s in zip(rows, statuses) if s != "open"]
        out: List[ContextUpdate] = []
        for row in opens[:MAX_OPEN_PER_SOURCE]:
            out.append(self._open_update(row, where))
        # Answered with "don't act yet" and not acted on since: owed action however old, so offered
        # on every look (first look included) until Quest marks it acted.
        waiting = [r for r in settled if _awaits_ai_action(r)][:MAX_AWAITING_ACTION]
        for row in waiting:
            out.append(self._awaiting_action_update(row, where))
        if not request.first_look:
            for row in settled:
                if _awaits_ai_action(row):
                    continue                   # offered above, as owed action
                when = _as_utc(row.get("resolved_at"))
                if when and request.since and when <= request.since:
                    continue                   # resolved before the last look: already seen
                if when is None and request.since:
                    continue                   # no resolution time, so it cannot be shown as new
                out.append(self._resolved_update(row, where, when))
        request.account(len(rows), _decisions_explanation(len(rows), len(out)))
        return out

    @staticmethod
    def _open_update(row: Dict[str, Any], where: str) -> ContextUpdate:
        asker = str(row.get("requester_name") or "").strip() or "Someone"
        assignee = str(row.get("assignee_name") or "").strip() or "the person it went to"
        summary = _clip(row.get("summary") or "a decision", 200)
        return ContextUpdate(
            source="decisions",
            kind="open ask",
            item_id=str(row.get("decision_id") or ""),
            title=f'{asker} asked {assignee}: "{_clip(summary, 100)}"',
            body=summary,
            author=asker,
            occurred_at=_as_utc(row.get("created_at")),
            location=where,
            needs_response=True,
            how_to_respond=("say what you would recommend in your result; the decision itself is "
                            "made on the ask, not by a run"),
            raw=dict(row),
        )

    @staticmethod
    def _awaiting_action_update(row: Dict[str, Any], where: str) -> ContextUpdate:
        summary = _clip(row.get("summary") or "a decision", 200)
        resolution = str(row.get("resolution") or "").strip() or "answered"
        response = str(row.get("response_text") or "").strip()
        answerer = str(row.get("resolved_by_name") or row.get("assignee_name") or "").strip() \
            or "The person"
        if _has_parked_action(row):
            respond = ("they approved a prepared action, which Quest runs itself when the queue is "
                       "run; do not perform it yourself, plan around it")
        else:
            respond = ("act on this answer in this run, together with any other answers waiting "
                       "on this quest; do not ask it again")
        return ContextUpdate(
            source="decisions",
            kind="answered ask, not yet acted on",
            item_id=str(row.get("decision_id") or ""),
            title=(f'{answerer} answered "{_clip(summary, 100)}" with {resolution} and asked the '
                   "AI to hold off until they had answered the rest; it is waiting for you to act"),
            body=response or resolution,
            verbatim=bool(response),
            excerpt=response,
            author=str(row.get("resolved_by_name") or "").strip(),
            occurred_at=_as_utc(row.get("resolved_at")),
            location=where,
            needs_response=not _has_parked_action(row),
            how_to_respond=respond,
            raw=dict(row),
        )

    @staticmethod
    def _resolved_update(row: Dict[str, Any], where: str, when: Optional[datetime]) -> ContextUpdate:
        asker = str(row.get("requester_name") or "").strip() or "Someone"
        summary = _clip(row.get("summary") or "a decision", 200)
        resolution = str(row.get("resolution") or "").strip()
        response = str(row.get("response_text") or "").strip()
        if row.get("auto_resolved") and not row.get("resolved_by_name"):
            outcome = "resolved automatically at its deadline"
        else:
            outcome = f"resolved as {resolution or 'answered'}"
        return ContextUpdate(
            source="decisions",
            kind="resolved ask",
            item_id=str(row.get("decision_id") or ""),
            title=f'The ask "{_clip(summary, 100)}" from {asker} was {outcome}',
            body=response or resolution or outcome,
            verbatim=bool(response),           # the resolver's own words, when they wrote any
            excerpt=response,
            author=str(row.get("resolved_by_name") or "").strip(),
            occurred_at=when,
            location=where,
            needs_response=False,
            raw=dict(row),
        )


# Answered asks still waiting on the AI, offered per look. Generous on purpose: the point of
# deferring is answering a batch (ten asks on one quest) and having them read together.
MAX_AWAITING_ACTION = 25


def _awaits_ai_action(row: Dict[str, Any]) -> bool:
    """Resolved by a person who chose "don't have my AI act on it yet", and not acted on since.
    Quest sets ``ai_action_status`` to "pending" for these and flips it once a run acts."""
    return str(row.get("ai_action_status") or "").strip().lower() == "pending" \
        and str(row.get("status") or "").strip().lower() != "open"


def _has_parked_action(row: Dict[str, Any]) -> bool:
    """An approved ask carrying a prepared action Quest itself runs (an email, a field edit), not
    yet run; Quest stamps ``ai_action_parked`` on the row. A run must not redo it by hand."""
    return bool(row.get("ai_action_parked")) and not row.get("executed")


def _decisions_explanation(total: int, offered: int) -> str:
    """Why fewer decisions were offered than the quest has."""
    if not total or offered:
        return ""
    return f"{total} decision(s), none open or newly answered"


def render_quest_event(event: Dict[str, Any]) -> str:
    """One ``analytics_events`` row's ``event_data`` as a short line saying what changed.

    ``quest_updated`` is the one that has to be handled with care: quest-backend's
    ``AnalyticsService.track_quest_updated`` (and ``QuestManager.update_quest_field``, its single
    sink) puts every field write under this ONE ``event_type``, distinguished only by
    ``event_data["updated_fields"]``. So "they changed the outcome" and "they changed the current
    state" are literally the same event name and only differ in this field -- reporting the bare
    event_type here ("the quest was updated") would lose exactly the distinction this source
    exists to carry, since a rewritten outcome and a moved current-state read as identical news.

    Every other event type carries its own small, already-named payload (a milestone's id and
    name, an old/new duration), so those just get pulled straight out.  An event_type this
    function does not recognise still renders (as its own name with underscores turned to
    spaces) rather than being dropped, so a future event added to quest-backend's vocabulary
    shows up as SOMETHING rather than silently disappearing until this function is updated to
    know about it.
    """
    event_type = str(event.get("event_type") or "").strip()
    data = event.get("event_data") or {}
    if event_type == "quest_updated":
        fields = [str(f) for f in (data.get("updated_fields") or []) if f]
        if fields:
            return f"Changed the {', '.join(fields)} field{'s' if len(fields) > 1 else ''}"
        return "Updated the quest"
    if event_type == "quest_created":
        outcome = data.get("outcome")
        return f'Created the quest: "{_clip(outcome, 100)}"' if outcome else "Created the quest"
    if event_type == "quest_completed":
        return "Marked the quest complete"
    if event_type == "quest_deleted":
        return "Deleted the quest"
    if event_type == "quest_archived":
        return "Archived the quest"
    if event_type == "quest_milestone_completed":
        name = data.get("milestone_name")
        return f'Completed the milestone "{_clip(name, 80)}"' if name else "Completed a milestone"
    if event_type == "quest_duration_updated":
        old_days, new_days = data.get("old_duration_days"), data.get("new_duration_days")
        if old_days is not None and new_days is not None:
            return f"Changed the quest duration from {old_days} to {new_days} day(s)"
        return "Changed the quest duration"
    return event_type.replace("_", " ").strip() or "quest activity"


class QuestEventsSource(_BaseSource):
    """What the quest's PERSON actually did to it since an assistant last looked.

    THE GAP THIS FILLS. ``QuestNotesSource`` sees a person's words and ``GoalUpdatesSource`` sees
    their check-ins, but neither sees a person just... doing the thing: ticking a milestone done
    in the app, rewriting the outcome, moving the deadline. None of that writes a note or an
    update, so an autopilot pass reading only those two channels sees nothing and can cheerfully
    re-propose work, or re-ask about a milestone, the person already finished themselves. This is
    the channel that lets a pass notice "they completed the milestone themselves" without being
    told -- reading quest-backend's ``analytics_events`` (quest-backend commit 55ad92be made the
    business layer actually emit ``quest_created``/``quest_updated``/``quest_completed``/
    ``quest_deleted``/``quest_milestone_completed``/``quest_duration_updated``/``quest_archived``
    from its single choke points, so an API-key write, an AI task and the app all produce the
    same event) through the read endpoint added alongside this source
    (``QuestClient.list_quest_events``, ``GET /api/quests/{quest_id}/events``).

    WATERMARKING IS CORRECT HERE, not a mistake. See the block comment just above
    ``DEFAULT_ALWAYS`` on why the previous AUTOPILOT RUN's own output (``runner/autopilot.py``'s
    ``last_run``) is deliberately kept OUT of this registry: it has to be in front of every pass,
    not delivered once and withheld from the next. A quest event is the opposite shape of thing.
    "What changed since I last looked" is exactly what it answers, the events are the PERSON's
    (see the actor filter below, not this assistant's own prior output), and re-showing an old
    one forever would be exactly the fatigue the first-look bound and the other caps in this
    module exist to prevent. Delivered once, like every other source here.

    CARD-SCOPED, SO NEVER JUDGED (``judge_relevance = False``). ``collect`` asks the backend for
    THIS quest's own id; the same reasoning ``QuestNotesSource`` states for itself applies letter
    for letter -- relevance comes from WHERE (which quest) the event happened, not from a model's
    opinion of it, and there is nothing here a relevance judge could do except occasionally lose
    an event that plainly belongs.

    NOT AN ASK (``tracks_asks = False``). Read ``_BaseSource.tracks_asks``'s own comment before
    ever flipping this: a live run once tracked the habit log and the daily reflection as asks,
    and the next morning both came back "still owed, needs an answer" -- an assistant asking a
    person to answer their own diary. A quest event is the same shape of trap wearing a new name:
    it is a RECORD of something that already happened, not a question anyone is waiting on an
    answer to, so nothing here is owed a reply and nothing here belongs in the feedback ledger.

    THE ACTOR FILTER IS THE WHOLE POINT, and it is conservative on purpose. ``analytics_events``
    rows are written from the SAME business-layer choke points regardless of who or what caused
    the change -- the app, an API-key caller (an AI task, autopilot, this very runner), or a
    person signed in -- so ``user_id`` alone can never distinguish "the person did this" from
    "their assistant did this on their behalf," and reading the latter back as the former is
    exactly the self-observation loop quest-backend's own commit message for the ``actor`` field
    (1dbb1871) names. ``collect`` therefore always asks the endpoint for ``actor="app"`` only.
    ``api_key`` is deliberately NOT folded into "human" or into "AI" either: an API key
    authenticates AS the account owner, which proves only that the write did NOT come from the
    app, never that a model made it (a person's own script, a Zapier hook, or a future
    integration could just as easily be behind it). Treating ``api_key`` as human would risk
    showing a run its own prior write, or another AI's, back to it as if the person had done it;
    treating it as AI would risk hiding a real integration-driven change a person set up
    themselves. Excluding it is the conservative reading either way: this source can UNDER-report
    (an ``api_key`` change a person genuinely made by hand through some other tool never surfaces
    here) but it can never INVENT a person's action out of a write nobody can attribute to them,
    which is the only direction of error this channel cannot afford.

    RENDERED AS A SHORT LINE, not a raw dict (``render_quest_event``). A ``quest_updated`` row's
    ``event_data`` only ever carries ``updated_fields``, so this is what turns "the quest was
    updated" into "they changed the outcome" -- the distinction the whole point of this source
    rests on. The event's own timestamp becomes ``occurred_at``, so the "and when" half of "what
    changed and when" comes from the manifest line's own date column rather than being repeated
    in the body.

    CAPPED like ``QuestNotesSource``/``GoalUpdatesSource`` at ``MAX_OPEN_PER_SOURCE`` -- passed
    straight to the endpoint's own ``limit``, so a quest edited thirty times in a day costs one
    small request and never a page the model has to skim past to find the actual instructions.

    NO DEDICATED ITEM ID. The read endpoint's response is exactly ``{event_type, event_data,
    timestamp, user_id, actor}`` -- there is no event id to key off, so ``item_id`` is composed
    from ``event_type`` and the row's own timestamp. That is unique enough for this source's own
    purposes (nothing here enters the feedback ledger or the relay path, both of which are the
    only things that lean on ``item_id`` for real identity), and good enough to dedupe the rare
    case of the same card being collected twice in one pass via the engine's cache.
    """
    name = "quest_events"
    describes = "quest lifecycle changes (outcome, deadline, milestones) the person made themselves"
    judge_relevance = False
    tracks_asks = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        quest_id = request.card_id
        lister = getattr(client, "list_quest_events", None)
        if not callable(lister) or not quest_id:
            return []
        since_iso = request.since.isoformat() if request.since else None
        try:
            events = list(lister(quest_id, since=since_iso, limit=MAX_OPEN_PER_SOURCE,
                                 actor="app") or [])
        except Exception as e:  # noqa: BLE001 -- one broken read never costs the rest of a bundle
            log.info("context updates: could not read quest events for %s (%s)", quest_id, e)
            return []
        where = request.card_label or _card_label(request.card) or "this quest"
        out: List[ContextUpdate] = []
        for event in events:
            if not isinstance(event, dict):
                continue
            summary = render_quest_event(event)
            event_type = str(event.get("event_type") or "quest_event")
            out.append(ContextUpdate(
                source=self.name,
                kind="quest event",
                item_id=f"{event_type}:{event.get('timestamp')}",
                body=summary,
                excerpt=summary,
                occurred_at=_as_utc(event.get("timestamp")),
                location=where,
                needs_response=False,
                raw=dict(event),
            ))
        request.account(len(events))
        return out


class ReflectionsSource(_BaseSource):
    """The person's latest PERIOD reflection (``runner.reflections``).

    Wrapped rather than reimplemented: the module already handles the period order, the framing and
    the degradation to empty. What this adds is that it now arrives through the same engine, with a
    ref, so a run can be asked what it did with it.

    Deliberately ``include_daily=False``: the daily plan's free text is the same for every card this
    engine assembles for, so delivering it here handed every quest's card the person's WHOLE
    reflection regardless of what it concerned. That text is now split into per-quest notes at
    reflection time (quest-backend's daily-plan reflection-notes endpoint), and a quest's own notes
    already reach its own card correctly scoped, through the ordinary quest-reference path. The
    period review stays: it is one review of the whole period, without an obvious single quest to
    attach to the way a daily entry's individual sentences do.
    """
    name = "reflections"
    describes = "the person's own period reflections"

    slot = "reflection"

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        from .reflections import collect_reflections, DEFAULT_PERIODS
        periods = tuple(request.opt("periods") or DEFAULT_PERIODS)
        # Reflections are USER-scoped, so a pass covering five quests would otherwise fetch the
        # same two documents five times. Cached on the engine (see ``CACHE_TTL_SECONDS``).
        key = ("reflections", periods)
        ctx = request.cache.get(key)
        if ctx is None:
            ctx = collect_reflections(request.client, periods=periods, now=request.now,
                                       include_daily=False)
            request.cache[key] = ctx
        text = ctx.as_text() if ctx else ""
        if not text:
            return []
        when = _as_utc(ctx.daily_date) or request.now
        return [ContextUpdate(
            source=self.name,
            kind="reflection",
            item_id=str(ctx.daily_date or ctx.period or "latest"),
            title="Their own reflection, most recent on record",
            body=text,
            verbatim=True,  # their reflection, as they wrote it
            occurred_at=when,
            location="Quest reflections",
            slot=self.slot,
            needs_response=False,
            summary=ctx.one_line(),
            raw={"period": ctx.period, "daily_date": ctx.daily_date},
        )]


class InsightsSource(_BaseSource):
    """The insights assigned to THIS quest (``runner.insights``), ONE UPDATE PER CAPTURE.

    Quest-scoped: the source asks the backend for the latest unacted insights assigned to the
    card's quest (a card of kind ``quest``, or any card whose row names a ``quest_id``) and reads
    nothing else. A card with no quest gets no insights. Insights are assigned to a quest when they
    are captured, so every row is relevant to the card and none is put to the relevance judge or
    held back as unsure.

    One row each: the receipt answers for each capture in the person's own words. The rows render
    through the composer's ``insights`` slot inside the framing the block carries
    (``runner.insights.block_header`` / ``BLOCK_FOOTER``). The person's category tags are shown
    beside each row as their own words and never decide anything.
    """
    name = "insights"
    describes = "the insights assigned to this quest that the person has not acted on"
    judge_relevance = False

    slot = "insights"

    @staticmethod
    def _quest_id(request: CollectRequest) -> str:
        if (request.card_kind or "") == "quest" and request.card_id:
            return str(request.card_id)
        card = request.card or {}
        return str(card.get("quest_id") or card.get("questId") or "")

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        from .insights import BLOCK_FOOTER, block_header, collect_quest_insights
        quest_id = self._quest_id(request)
        if not quest_id:
            return []
        # Cached per quest within one engine window: the cache is shared across cards, so a bare
        # "insights" key would hand one quest's captures to the next card.
        key = f"insights:{quest_id}:{request.since.isoformat() if request.since else ''}"
        ctx = request.cache.get(key)
        if ctx is None:
            ctx = collect_quest_insights(request.client, quest_id, since=request.since,
                                         now=request.now)
            request.cache[key] = ctx
        rows = list(getattr(ctx, "insights", None) or [])
        if not rows:
            return []
        frame = (block_header(getattr(ctx, "since", None),
                              getattr(ctx, "window_days", 14)), BLOCK_FOOTER)
        out: List[ContextUpdate] = []
        for row in rows:
            text = str(getattr(row, "text", "") or "").strip()
            if not text:
                continue
            cats = [str(c) for c in (getattr(row, "categories", None) or [])]
            when = _as_utc(getattr(row, "created_at", None))
            date = when.strftime("%Y-%m-%d") if when else "an unrecorded date"
            tags = f" tagged {', '.join(cats)}" if cats else " (untagged)"
            out.append(ContextUpdate(
                source=self.name,
                kind="capture",
                item_id=str(getattr(row, "entry_id", "") or ""),
                title="They captured this for this quest and have not acted on it",
                body=f"[{date}]{tags}\n      {text}",
                verbatim=True,  # their capture, as they wrote it
                excerpt=text,
                occurred_at=when,
                location="Quest insights",
                slot=self.slot,
                slot_frame=frame,
                how_to_respond="act on it and say so in your result, or say why it cannot be acted on yet",
                needs_response=True,
                summary=f"Unacted insight from {date}{tags}: {_clip(text, 220)}",
                raw={"categories": cats},
            ))
        return out


# Entry keys a habit/timer collection keeps for its own bookkeeping. Rendered, they are noise:
# a run does not need to know a day entry's period bounds, only what the person did that day.
_COLLECTION_INTERNAL_FIELDS = frozenset({
    "period", "period_start", "period_end", "last_activity_date", "entry_date", "completed",
    "sessions", "habit_timer", "completionType",
})


def _duration_label(seconds: Any) -> str:
    """``10573`` -> ``"2h 56m"``. A habit timer's raw seconds mean nothing at a glance."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total <= 0:
        return ""
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


class CollectionEntriesSource(_BaseSource):
    """New entries in one of the person's own collections -- a habit, a timer, a log.

    Spec: ``{"source": "collection", "name": "Focus on PhD Dissertation"}`` (resolved by name), or
    ``{"collection_id": "coll_..."}`` when the id is known.

    WHY A CARD WANTS THIS. A habit tracked against a piece of work IS a record of that work: the
    dissertation quest's own timer says whether the person sat down to it yesterday, for how long,
    and in how many sittings. A run composing today's brief without it is guessing at exactly the
    thing the person already measured, and will cheerfully propose a plan for a day they already
    spent three hours on.

    NOT put to a relevance judgment, and that is deliberate: unlike a capture, which arrives from a
    space covering the person's whole life, a collection reaches a card only because the card NAMED
    it. It is card-scoped by construction, so judging it could only ever lose one.

    ONE UPDATE PER COLLECTION, the new entries in its body, not one per entry. A log is one
    channel: seven days of a habit timer are one thing to take into account, and seven refs would
    be seven receipt lines each saying "noted" -- verified live, where a week of entries was the
    bulk of a bundle and the person's one capture sat at the bottom of it.
    """
    name = "collection"
    describes = "new entries in a habit, timer or log collection this work tracks"
    judge_relevance = False

    def collect(self, request: CollectRequest) -> Sequence[ContextUpdate]:
        client = request.client
        lister = getattr(client, "list_collection_entries", None)
        if not callable(lister):
            return []
        collection_id, label = self._resolve(request)
        if not collection_id:
            return []
        entries = lister(collection_id) or []
        rows = entries.get("items") if isinstance(entries, dict) else entries
        if not isinstance(rows, list):
            return []
        kept = []                      # (when, rendered line, timer seconds, is a habit)
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            values = entry.get("fieldValues") or entry.get("field_values") or {}
            when = (_as_utc(values.get("entry_date")) or _as_utc(values.get("last_activity_date"))
                    or _as_utc(entry.get("createdAt")) or _as_utc(entry.get("created_at")))
            if request.since and when and when <= request.since:
                continue
            line = self._render(values)
            if not line:
                continue
            timer = values.get("habit_timer")
            seconds = timer.get("value") if isinstance(timer, dict) else timer
            try:
                seconds = int(float(seconds))
            except (TypeError, ValueError):
                seconds = 0
            kept.append((when or datetime.min.replace(tzinfo=timezone.utc), line, seconds,
                         str(entry.get("type") or "") == "habit"))
        if not kept:
            return []
        kept.sort(key=lambda k: k[0], reverse=True)
        kept = kept[:int(request.opt("max_entries") or 7)]
        newest, oldest = kept[0][0], kept[-1][0]
        span = (newest.strftime("%Y-%m-%d") if len(kept) == 1 or oldest == newest
                else f"{oldest.strftime('%Y-%m-%d')} to {newest.strftime('%Y-%m-%d')}")
        total = _duration_label(sum(k[2] for k in kept))
        excerpt = (f"{len(kept)} entr{'y' if len(kept) == 1 else 'ies'}, {span}"
                   + (f", {total} in all" if total else ""))
        return [ContextUpdate(
            source=self.name,
            kind="habit" if any(k[3] for k in kept) else "log",
            item_id=collection_id,
            title=f"{label}, as they logged it (newest first)",
            body="\n".join(k[1] for k in kept),
            excerpt=excerpt,
            occurred_at=newest,
            location=label,
            how_to_respond="",
            needs_response=False,
            summary=f"{label}: {excerpt}",
            raw={"collection_id": collection_id, "entries": len(kept)},
        )]

    @staticmethod
    def _render(values: Dict[str, Any]) -> str:
        """One readable line for one entry: the date, what was done, how long, plus any own fields."""
        parts: List[str] = []
        date = str(values.get("entry_date") or "").strip()
        if date:
            parts.append(date)
        completion = str(values.get("completionType") or "").strip()
        if completion:
            parts.append(completion)
        timer = values.get("habit_timer")
        seconds = timer.get("value") if isinstance(timer, dict) else timer
        spent = _duration_label(seconds)
        if spent:
            sessions = values.get("sessions")
            count = len(sessions) if isinstance(sessions, list) else 0
            parts.append(f"{spent} over {count} session(s)" if count else spent)
        for key, value in values.items():
            if key in _COLLECTION_INTERNAL_FIELDS:
                continue
            # Emptiness has to be checked on the RENDERED value, not the raw one: a field the
            # person left blank arrives as "", "  ", [] or a dict of empties depending on its type,
            # and an un-normalized check let "value_achieved: " through on live data.
            rendered = _clip(value, 120)
            if not rendered or rendered in ("{}", "[]", "None"):
                continue
            parts.append(f"{key}: {rendered}")
        return ", ".join(parts)

    def _resolve(self, request: CollectRequest) -> tuple:
        """(collection_id, label) from the spec, resolving a name against the person's own list."""
        wanted_id = str(request.opt("collection_id") or "").strip()
        wanted_name = str(request.opt("name") or request.opt("collection") or "").strip()
        if wanted_id and not wanted_name:
            return wanted_id, str(request.opt("label") or wanted_id)
        lister = getattr(request.client, "list_collections", None)
        if not callable(lister):
            return wanted_id, str(request.opt("label") or wanted_name or wanted_id)
        cached = request.cache.get("collections")
        if cached is None:
            cached = lister() or []
            request.cache["collections"] = cached
        for coll in cached:
            if not isinstance(coll, dict):
                continue
            cid, cname = str(coll.get("id") or ""), str(coll.get("name") or "")
            if (wanted_id and cid == wanted_id) or (
                    wanted_name and cname.strip().lower() == wanted_name.lower()):
                return cid, cname or wanted_name
        log.warning("context updates: no collection named %r", wanted_name or wanted_id)
        return "", wanted_name or wanted_id
