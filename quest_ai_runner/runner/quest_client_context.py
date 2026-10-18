"""Email, context entries, reflections and insight collections.

A mixin of ``QuestClient`` (see ``quest_client.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import timezone

from .quest_client_support import QuestApiError, QuestNotConfigured, log


class ContextEntriesMixin:
    """Email, context entries, reflections and insight collections."""

    def send_quest_email(self, quest_id: str, *, subject: str, body: str,
                         rep_id: Optional[str] = None,
                         task_id: Optional[str] = None,
                         recipients: Optional[List[str]] = None) -> Dict[str, Any]:
        """POST /api/quests/{quest_id}/email — mail this quest's people, as the persona that wrote it.

        THE way a run sends mail. Not a local mail script: going through Quest is what gives the
        message the quest's own Reply-To (so an answer comes back as a note on the quest rather
        than into a void), the account's unsubscribe handling, a record, and a signature naming the
        persona instead of a generic assistant.

        The audience defaults to the quest's own settings, so a run decides what to say and when,
        not who a standing quest's mail reaches, by default. ``recipients`` is an explicit
        override for the genuine one-off case: someone who is not, and should not become, a
        standing participant on the quest (a donor, an applicant, a specific person not yet
        sharing it) who still needs this quest's Reply-To rather than a mailer with none. It
        replaces the quest's own list for this one send rather than adding to it; the backend caps
        how many addresses one call may name. Requires the person to have enabled email on the
        quest (which is also what mints the reply address); without it the backend answers 400 and
        nothing is sent.

        RAISES on failure rather than returning a falsy value: a run that believes it has told
        someone something, when it has not, will go on to act as though the message landed.
        """
        self._require()
        body_payload: Dict[str, Any] = {"subject": subject, "body": body}
        if rep_id:
            body_payload["rep_id"] = rep_id
        if task_id:
            body_payload["task_id"] = task_id
        if recipients:
            body_payload["recipients"] = recipients
        return self._request("POST", f"/api/quests/{quest_id}/email", body=body_payload) or {}

    def create_email_draft(self, *, recipient: str, subject: str, body: str,
                           cc: Optional[List[str]] = None,
                           task_id: Optional[str] = None) -> Dict[str, Any]:
        """POST /api/email-drafts - file an email draft into the key owner's Approval Queue.

        NOTHING is sent. The draft waits in the Quest app's Approval Queue (status
        ``pending_review``) until a person approves it, and only that approval sends it, through
        Quest's one send path (suppression check, unsubscribe footer, CC screening). The draft
        belongs to the account the API key authenticates as; the backend never takes the owner
        from the body. The account's standing Always CC is added by the backend, so callers do
        not add it. ``task_id`` is recorded as the draft's ``source_task_id`` for tracing.

        RAISES on failure, so a run never believes a draft is waiting when it is not.
        """
        self._require()
        payload: Dict[str, Any] = {"recipient": recipient, "subject": subject, "body": body}
        if cc:
            payload["cc"] = cc
        if task_id:
            payload["sourceTaskId"] = task_id
        return self._request("POST", "/api/email-drafts", body=payload) or {}

    # --- quest context entries (the quest's own documents; UPDATABLE, unlike notes) ------------

    def list_context_entries(self, quest_id: str) -> List[Dict[str, Any]]:
        """GET /api/quests/{quest_id}/context-entries — this quest's context documents.

        Each item is a preview dict (``id``, ``name``, ``char_count``, ``tags``, ``created_at``).
        These are the quest's own attached documents, distinct from its notes in the one way that
        matters for a refreshing artifact: an entry can be REPLACED in place (see
        ``update_context_entry``), while a note can only ever be appended. Returns [] on failure.
        """
        try:
            self._require()
            resp = self._request("GET", f"/api/quests/{quest_id}/context-entries") or []
            return resp if isinstance(resp, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_context_entries failed for quest %s: %s", quest_id, e)
            return []

    def create_context_entry(self, quest_id: str, name: str, content: str) -> Dict[str, Any]:
        """POST /api/quests/{quest_id}/context-entries — add one named context document.

        The server creates the quest's context collection on first use, so no setup call is needed.
        Empty ``content`` is rejected server-side (422). Returns {} on failure.
        """
        try:
            self._require()
            resp = self._request("POST", f"/api/quests/{quest_id}/context-entries",
                                 body={"name": name, "content": content}) or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("create_context_entry failed for quest %s: %s", quest_id, e)
            return {}

    def update_context_entry(self, quest_id: str, entry_id: str, *,
                             name: Optional[str] = None,
                             content: Optional[str] = None) -> Dict[str, Any]:
        """PUT /api/quests/{quest_id}/context-entries/{entry_id} — replace an entry's name/content.

        This is what makes an upsert possible: re-publishing a document that refreshes (a quest's
        current next steps, say) replaces the one entry instead of piling up a new object per
        refresh. Only the fields given are changed. Returns {} on failure.
        """
        body: Dict[str, Any] = {}
        if name is not None:
            body["name"] = name
        if content is not None:
            body["content"] = content
        try:
            self._require()
            resp = self._request(
                "PUT", f"/api/quests/{quest_id}/context-entries/{entry_id}", body=body) or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("update_context_entry failed for quest %s entry %s: %s",
                        quest_id, entry_id, e)
            return {}

    # --- the person's own reflections (USER-scoped; no team_id, no quest_id) -------------------
    # Quest asks a person two standing questions and keeps their answers verbatim: the daily plan's
    # "how did yesterday go" review, and each period review's "how did this period go" / "what to
    # focus on next". Both endpoints are authenticated as the CALLER and scoped to that user, so
    # neither takes (nor accepts) a team or quest id. Reading them is how anything here can answer
    # "what should I work on" from what the person actually wrote instead of guessing from task
    # rows. See ``runner.reflections`` for the helper that composes both into one context block.

    PERIOD_REVIEW_PERIODS = ("week", "month", "quarter", "year")

    def get_daily_reflection(self, *, date: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/daily-plan/today[?date=YYYY-MM-DD] — one day's daily-plan entry.

        Returns ``{has_plan, plan_id, date, yesterday_review, today_plan, goals_created}``.
        ``yesterday_review`` is the person's reflection on how the day BEFORE ``date`` went (they
        write it while planning ``date``); ``today_plan`` is what they said they would do on
        ``date`` itself.

        Omitting ``date`` means today in the USER's own timezone, resolved server-side — which is
        why the no-date call is the right default and an explicit date is only for walking back to
        an earlier day. ``has_plan: False`` with both texts ``None`` simply means they have not
        filled that day in; it is an ordinary state, not a failure. Returns {} on any error.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/daily-plan/today",
                                 params={"date": date} if date else None) or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_daily_reflection failed (date=%s): %s", date, e)
            return {}

    def get_period_reflection(self, period: str = "week", *, use_previous: bool = False,
                              tz: Optional[str] = None) -> Dict[str, Any]:
        """GET /api/period-review/{period}/current — one period review's REVIEW block.

        ``period`` is one of ``PERIOD_REVIEW_PERIODS`` (week/month/quarter/year). There is no
        "day" here: the daily equivalent is ``get_daily_reflection`` above, a different endpoint
        with a different shape. An unknown period is rejected client-side (returns {}) rather than
        spent on a 422. ``use_previous=True`` asks for the period BEFORE the current one, which is
        what you want early in a period, before this one has been reviewed. ``tz`` is an IANA zone
        name deciding where the period boundaries fall; the server defaults to UTC.

        Returns the response's ``review`` block: ``{has_review, review_id, status,
        reflection_past, reflection_future, ai_suggestions, suggestions_accepted}``.
        ``has_review: False`` means the person has not submitted that review yet — expected, not an
        error, and the reflection fields are ``None``. The response's ``stats`` block (completions
        and time distribution) is deliberately dropped: this method exists to read what the PERSON
        wrote, and the stats are a separate, much larger concern. Returns {} on any error.
        """
        period = str(period or "").strip().lower()
        if period not in self.PERIOD_REVIEW_PERIODS:
            log.warning("get_period_reflection: unknown period %r (expected one of %s)",
                        period, ", ".join(self.PERIOD_REVIEW_PERIODS))
            return {}
        try:
            self._require()
            params: Dict[str, Any] = {}
            if use_previous:
                params["use_previous"] = "true"
            if tz:
                params["timezone"] = tz
            resp = self._request("GET", f"/api/period-review/{period}/current",
                                 params=params or None) or {}
            review = resp.get("review") if isinstance(resp, dict) else None
            return review if isinstance(review, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_period_reflection failed for period %s: %s", period, e)
            return {}

    # --- the person's captured insights (USER-scoped; no team_id, no quest_id) ------------------
    # Quest auto-creates one "Insights" collection per person: quick capture for the idea that
    # arrives away from any goal, with the free-text category tags they chose, an ``acted_on``
    # checkbox, and a description of what was done. It is the one place in Quest holding something
    # the person recorded that has NOT been turned into a goal or task yet. See ``runner.insights``
    # for the helper that composes recent unacted captures into one context block.

    def get_insights_collection(self) -> Dict[str, Any]:
        """GET /api/data/insights/collection — the caller's Insights collection.

        Returns the collection dict (``id``, ``name``, ``customFields``, …), the same shape as
        ``GET /api/data/collections/{id}``. The route CREATES the collection when the person does
        not have one yet, so an empty result means a failed read rather than "no insights feature"
        — but the ``id`` is all a reader needs, and the entries live behind
        ``list_collection_entries``. Returns {} on any error.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/data/insights/collection") or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("get_insights_collection failed: %s", e)
            return {}

    def list_quest_insights(self, quest_id: str, limit: int = 10,
                            include_acted: bool = False) -> Dict[str, Any]:
        """GET /api/data/insights/by-quest -- the latest insights ASSIGNED to one quest.

        An insight carries the quest ids it was assigned to when it was captured, so this is the
        only insights read context assembly makes: the quest in front of the run, never the
        person's whole backlog. Returns ``{"collection_id", "has_quests_field", "items": [...]}``,
        newest first, already filtered to that quest and (unless ``include_acted``) to unacted
        ones. Items are entry dicts shaped like ``list_collection_entries`` items (camelCase or
        snake_case). An older backend without the route answers 404 and a person whose collection
        has no quests field gets an empty list; both read as "no insights". Returns {} on any
        error, including a blank ``quest_id``.
        """
        if not str(quest_id or "").strip():
            return {}
        try:
            self._require()
            resp = self._request("GET", "/api/data/insights/by-quest",
                                 params={"quest_id": quest_id, "limit": int(limit),
                                         "include_acted": "true" if include_acted else "false"}) or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_quest_insights failed for quest %s: %s", quest_id, e)
            return {}

    def add_context_pref(self, user_id: str, pref: str, *,
                         team_id: Optional[str] = None, source: str = "auto") -> bool:
        """POST /api/teams/{team_id}/members/{user_id}/context-prefs -- teach a rep where it works.

        One short sentence naming what a run actually consulted ("Consults the funding spreadsheet
        and donor list for this kind of task."), so the rep's next run starts where its last one
        ended up. ``source: "auto"`` marks it as learned rather than typed by a person.

        Returns True when it landed. Best-effort like every other write here: learning is worth
        having and never worth failing a run over.
        """
        try:
            self._require()
            tid = team_id or self.team_id
            if not (tid and user_id and str(pref or "").strip()):
                return False
            self._request("POST", f"/api/teams/{tid}/members/{user_id}/context-prefs",
                          body={"pref": str(pref).strip(), "source": source})
            return True
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("add_context_pref failed for %s: %s", user_id, e)
            return False

    def list_collections(self) -> List[Dict[str, Any]]:
        """GET /api/data/collections -- every collection the caller owns.

        Needed to resolve a collection by NAME. A card that watches a habit is far more readable,
        and far more durable across a rebuild, saying "Focus on PhD Dissertation" than carrying an
        opaque id; this is what turns the one into the other. Returns [] on any error.
        """
        try:
            self._require()
            resp = self._request("GET", "/api/data/collections") or {}
            if isinstance(resp, list):
                return resp
            data = resp.get("data") if isinstance(resp, dict) else None
            if isinstance(data, dict):
                items = data.get("items")
                if isinstance(items, list):
                    return items
            items = resp.get("items") if isinstance(resp, dict) else None
            return items if isinstance(items, list) else []
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_collections failed: %s", e)
            return []

    def list_collection_entries(self, collection_id: str, *, page: int = 0,
                                limit: int = 50) -> Dict[str, Any]:
        """GET /api/data/collections/{collection_id}/entries — one page of a collection's entries.

        Generic across every collection type, not insights-specific. Returns
        ``{"items": [...], "pagination": {total, page, limit, total_pages, has_next, has_prev}}``,
        newest first (the route sorts by ``created_at`` descending). Each item carries
        ``fieldValues`` keyed by the collection's own field ids, plus ``createdAt``.

        There is NO server-side filter on this route — not by date, not by field value — so any
        "recent" or "unacted" selection is the caller's to apply client-side. Returns {} on any
        error.
        """
        try:
            self._require()
            resp = self._request("GET", f"/api/data/collections/{collection_id}/entries",
                                 params={"page": page, "limit": limit}) or {}
            return resp if isinstance(resp, dict) else {}
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("list_collection_entries failed for collection %s (page %s): %s",
                        collection_id, page, e)
            return {}

    def mark_insight_acted_on(self, entry_id: str, collection_id: str,
                              action_taken_description: str = "") -> bool:
        """PATCH /api/data/insights/mark-acted-on — tick one insight's ``acted_on`` box.

        ``action_taken_description`` fills the entry's "Action Taken" field, and is what the person
        sees next to the tick when they review their captures. Write it as a statement of what
        actually exists now ("created the goal 'Draft chapter two'"), not what was intended.

        Returns True only when the write succeeded. This is a WRITE on the person's own data with a
        visible consequence — a ticked insight drops out of every unacted list, including the one
        their weekly review is built from — so a failure is logged and reported rather than
        swallowed, and a caller must not treat "I asked" as "it is done". By the same token, do not
        call this for work that was merely QUEUED: an insight marked acted on because a task was
        created, where that task is then never approved or fails, has been silently removed from
        the person's list without anything having happened.
        """
        if not (entry_id and collection_id):
            log.warning("mark_insight_acted_on needs both entry_id and collection_id")
            return False
        try:
            self._require()
            self._request("PATCH", "/api/data/insights/mark-acted-on",
                          body={"entry_id": entry_id, "collection_id": collection_id,
                                "action_taken_description": action_taken_description or ""})
            return True
        except (QuestApiError, QuestNotConfigured) as e:
            log.warning("mark_insight_acted_on failed for entry %s: %s", entry_id, e)
            return False
