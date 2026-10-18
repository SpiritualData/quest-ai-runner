"""Building the context view a run starts from: notes, updates, history, contracts.

A mixin of ``TaskExecutor`` (see ``executor.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .executor_contracts import (
    GOAL_UPDATE_CONTEXT_LIMIT,
    KEEP_GOING_CONTRACT,
    NOTE_CONTEXT_LIMIT,
    NO_ASSUMED_PROGRESS_CONTRACT,
    REPLY_LOOP_CONTRACT,
    RESULT_IS_THE_WORK_CONTRACT,
    email_contract,
    log,
    render_goal_notes,
    render_goal_updates,
    render_run_history,
)
from .executor_support import CONV_CONTEXT_MAX_BYTES


class ContextViewMixin:
    """Building the context view a run starts from: notes, updates, history, contracts."""

    def _build_context_view(self, goal_id: Optional[str], quest_id: Optional[str],
                            conv_id: Optional[str] = None,
                            rep_id: Optional[str] = None,
                            related_goal_id: Optional[str] = None,
                            with_updates: bool = True) -> str:
        """Fetch goal + quest metadata + notes + conversation history from the Quest API.

        ``with_updates`` is whether the context engine (when one is wired) is asked for what
        changed on this quest; a caller whose task text already carries the engine's block passes
        False.

        The context_view is passed to the orchestrator so the deep agent knows what goal/quest
        it's working on, what progress has been made, and the prior conversation that led to
        the task. Gracefully handles missing API (no-ops when client lacks needed methods) and
        API errors (builds partial context).

        ``related_goal_id`` (optional, additive) names a SPECIFIC goal inside a quest that this
        task is scoped to -- distinct from ``goal_id``, which (per a historical naming accident
        this repo does not fix) actually holds the QUEST's id on a task created against a quest.
        When present, it is the real goal id to fetch, and ``goal_id`` is what supplies the quest
        id to fetch it WITH (see the goal-metadata fetch below). Absent, behavior is unchanged:
        the existing single-id path runs exactly as before."""
        parts = []

        # Fetch prior conversation history if this task was delegated from a chat — but ONLY as a
        # FALLBACK when the orchestrator has no ConversationStore wired. When a store IS wired, the
        # orchestrator's Step 1 (User Input Understanding) pulls the relevant slice itself (and
        # resolves the request from it), so we must not also dump the full transcript here.
        if (conv_id and self._retrieval
                and getattr(self._orch, "conversation_store", None) is None):
            try:
                # Try to read the EXACT conversation by its conv_id. BOUNDED: max_bytes caps the
                # transcript at the source, so a long conversation can never grow this task's
                # prompt without bound (a conversation-aware adapter keeps the recent tail).
                obs = self._retrieval.read_section(str(conv_id),
                                                   max_bytes=CONV_CONTEXT_MAX_BYTES)
                if obs and obs.kind == "read" and obs.text:
                    # Explicitly mark which conversation we loaded to disambiguate from previous tasks
                    parts.append(f"=== Prior Conversation Context (conv_id={conv_id}) ===\n{obs.text}\n")
                elif obs and obs.kind == "error":
                    # Conversation not found is non-critical, but log it for debugging
                    # (the task still runs with just the goal/quest context, not the prior chat)
                    pass
            except Exception:  # noqa: BLE001 — conversation fetch failure is non-critical
                pass

        # Before the quest branch and before the email contract: how a result is written is the
        # same question for every task, and the answer must not depend on whether this one happens
        # to sit on a quest that mails.
        parts.append(RESULT_IS_THE_WORK_CONTRACT)

        if not goal_id and not quest_id:
            parts.append(KEEP_GOING_CONTRACT)
            return "\n".join(parts)
        # Not inside the ``if quest_id`` below: a task created against a quest carries that
        # quest's id in goal_id and leaves quest_id null, and those are exactly the runs that mail.
        self._append_email_contract(parts, goal_id, quest_id, rep_id)
        self._append_folder_zones_contract(parts, goal_id, quest_id)
        # Fetch quest metadata if available
        quest: Dict[str, Any] = {}
        if quest_id:
            get_quest = getattr(self._client, "get_quest", None)
            if callable(get_quest):
                try:
                    quest = get_quest(quest_id) or {}
                    if quest:
                        outcome = quest.get("outcome", "")
                        if outcome:
                            parts.append(f"Quest outcome: {outcome}")
                        completed = quest.get("completed")
                        if completed is not None:
                            status = "completed" if completed else "in progress"
                            parts.append(f"Quest status: {status}")
                except Exception:  # noqa: BLE001
                    pass  # API unavailable or error; continue with what we have

        # Fetch goal metadata if available. When related_goal_id is set, it is the real goal to
        # fetch and goal_id resolves the quest it lives in (the misnomer noted on this method's
        # docstring); otherwise this is exactly the prior single-id behavior.
        _fetch_goal_id = related_goal_id or goal_id
        _fetch_quest_id = goal_id if related_goal_id else quest_id
        if _fetch_goal_id and _fetch_quest_id:
            get_goal = getattr(self._client, "get_goal", None)
            if callable(get_goal):
                try:
                    goal = get_goal(_fetch_goal_id, quest_id=_fetch_quest_id)
                    if goal:
                        name = goal.get("name", "")
                        if name:
                            parts.append(f"Goal: {name}")
                        description = goal.get("description", "")
                        if description:
                            parts.append(f"Goal description: {description}")
                        deadline = goal.get("deadline", "")
                        if deadline:
                            parts.append(f"Goal deadline: {deadline}")
                        completed = goal.get("completed")
                        if completed is not None:
                            status = "completed" if completed else "in progress"
                            parts.append(f"Goal status: {status}")
                except Exception:  # noqa: BLE001
                    pass  # API unavailable or error; continue with what we have

        # The goal's own update thread, independent of whether the get_goal call above succeeded
        # (it hits a different endpoint): the person's check-in notes on this specific goal.
        if _fetch_goal_id:
            goal_updates_text = render_goal_updates(self._fetch_goal_updates(_fetch_goal_id))
            if goal_updates_text:
                parts.append(goal_updates_text)

        # The notes on the QUEST are the person's reply channel, so they are fetched whenever there
        # is a quest — with or without a goal on the task.
        if quest_id:
            # The whole thread, both sides, attributed: what was said on this quest so far.
            notes_text = render_goal_notes(self._fetch_person_notes(quest_id, goal_id))
            if notes_text:
                parts.append(notes_text)

            updates_text = insights_text = ""
            if self._update_engine is not None:
                # What is NEW or still OPEN on this quest's channels, through the same engine an
                # autopilot pass asks: the person's unanswered notes, the captures assigned to
                # this quest, comments on its documents, a collection it watches. Each carries a
                # ref, and the result closes with a receipt on every one. The engine owns the
                # captures either way: a batch the pass composed already carries them, judged,
                # in its own text, so nothing is fetched unjudged here beside them.
                if with_updates:
                    updates_text = self._collect_context_updates(quest, quest_id)
                    if updates_text:
                        parts.append(updates_text)
            else:
                # What the person captured on their phone and has not acted on yet. Autopilot
                # passes already read these; an ordinary scheduled run did not, so the same
                # capture steered a pass and was invisible to the daily task working the very
                # quest it was about.
                insights_text = self._fetch_quest_captures(quest_id)
                if insights_text:
                    parts.append(insights_text)

            history_text = render_run_history(self._fetch_run_history(quest_id))
            if history_text:
                parts.append(history_text)
                # Only where there IS a "previously". On a quest nothing has run on yet, nobody has
                # been asked to do anything, so this would only make a first run hedge about work
                # that was never requested.
                parts.append(NO_ASSUMED_PROGRESS_CONTRACT)

            if notes_text or insights_text or updates_text:
                parts.append(REPLY_LOOP_CONTRACT)

        # Applies to every run: hitting a blocker is normal, stopping because of one is not.
        parts.append(KEEP_GOING_CONTRACT)
        return "\n".join(parts)  # Combined conversation + quest/goal context

    def _append_email_contract(self, parts: List[str], goal_id: Optional[str],
                               quest_id: Optional[str],
                               rep_id: Optional[str] = None) -> None:
        """Add the email contract when (and only when) this quest actually mails its work.

        Both ids are tried, quest_id first and goal_id second, because a task created against a
        quest carries that quest's id in ``goal_id`` and leaves ``quest_id`` NULL -- the same
        goal-or-quest ambiguity the poller already handles for the quest folder map. Reading only
        ``quest_id`` meant the contract never fired for those tasks: every run concluded email was
        off, mailed by hand through a local script, and the person received the automatic copy and
        the hand-rolled one, minutes apart -- the exact duplicate this contract exists to prevent.
        A ``get_quest`` on an id that is a goal rather than a quest returns {} and falls through,
        so trying both is safe as well as necessary.
        """
        mailing = self._mailing_quest_id(goal_id, quest_id)
        if mailing:
            # The id that actually carries the settings, so the printed command names the quest
            # whose recipients the mail will go to.
            parts.append(email_contract(mailing, rep_id))

    def _mailing_quest_id(self, goal_id: Optional[str],
                          quest_id: Optional[str]) -> Optional[str]:
        """The id of the quest whose results are mailed automatically, or None when email is off
        (or the quest cannot be read). quest_id first, then goal_id, for the reason
        ``_append_email_contract`` gives."""
        get_quest = getattr(self._client, "get_quest", None)
        if not callable(get_quest):
            return None
        for candidate in (quest_id, goal_id):
            if not candidate:
                continue
            try:
                quest = get_quest(candidate) or {}
            except Exception:  # noqa: BLE001 — never let a context extra break a run
                continue
            settings = ((quest.get("autopilot") or {}).get("email") or {})
            if settings.get("enabled"):
                return candidate
        return None

    def _append_folder_zones_contract(self, parts: List[str], goal_id: Optional[str],
                                      quest_id: Optional[str]) -> None:
        """Tell a run with a synced working folder how that folder separates whose work is whose.

        Gated on the folder actually being MAPPED, not merely on the feature being on: a run with
        no folder told to check a ledger goes looking for a file that does not exist, and a run
        that cannot find what the prompt promised starts inventing a substitute. Same goal_id-then-
        quest_id precedence as ``_resolve_working_dir``, since this is the same folder.
        """
        if not self._quest_folder_zones:
            return
        folder = self._resolve_working_dir(goal_id, quest_id)
        if not folder:
            return
        from .quest_folder_zones import folder_zones_contract
        parts.append(folder_zones_contract(folder))

    def _fetch_person_notes(self, quest_id: str, goal_id: Optional[str]) -> List[Dict[str, Any]]:
        """The quest's notes, which is where a person answers their AI.

        ``list_quest_notes`` (GET /api/quests/{id}/notes) is the collection that actually holds
        them: it is what the Quest app writes, what ``quest_folder_sync`` mirrors, and what an
        owner adds a note to. ``list_goal_notes`` is tried only as a fallback for a backend that
        implements the per-goal route -- the reference backend does not, so asking it first (as
        this did) returned a 404 and left every run with NO notes at all while the person's replies
        sat unread on the quest.
        """
        list_quest_notes = getattr(self._client, "list_quest_notes", None)
        if callable(list_quest_notes):
            try:
                notes = list(list_quest_notes(quest_id) or [])
                if notes:
                    return notes
            except Exception:  # noqa: BLE001
                pass  # API unavailable or error; fall through to the per-goal route
        list_goal_notes = getattr(self._client, "list_goal_notes", None)
        if goal_id and callable(list_goal_notes):
            try:
                return list(list_goal_notes(goal_id, quest_id=quest_id,
                                            limit=NOTE_CONTEXT_LIMIT) or [])
            except Exception:  # noqa: BLE001
                pass  # API unavailable or error; continue with what we have
        return []

    def _fetch_goal_updates(self, goal_id: str) -> List[Dict[str, Any]]:
        """THIS goal's own check-in thread -- distinct from ``_fetch_person_notes`` above, which
        reads the quest-wide reply channel. ``list_goal_updates`` is duck-typed via getattr like
        every other client method here: it is a newer addition, and a consumer's ``QuestClient``
        that does not carry it yet must not break the rest of this context, just skip this block.
        """
        list_goal_updates = getattr(self._client, "list_goal_updates", None)
        if not callable(list_goal_updates):
            return []
        try:
            return list(list_goal_updates(goal_id, limit=GOAL_UPDATE_CONTEXT_LIMIT) or [])
        except Exception:  # noqa: BLE001
            return []  # API unavailable or error; the goal metadata above still stands

    def _fetch_run_history(self, quest_id: str) -> List[Dict[str, Any]]:
        """Recent tasks on this quest, whatever their outcome.

        Deliberately unfiltered by status: the run that most needs reading is often the one that
        FAILED, because its work happened anyway and only its confirmation did not.
        """
        list_tasks = getattr(self._client, "list_tasks", None)
        if not callable(list_tasks):
            return []
        try:
            return list(list_tasks(goal_id=quest_id) or [])
        except Exception:  # noqa: BLE001 — history is context, never a reason to fail a run
            return []

    def _collect_context_updates(self, quest: Dict[str, Any], quest_id: str) -> str:
        """The engine's block for this quest, or "" when nothing arrived or the engine failed.

        Kept on the executor as ``_context_bundle`` so the result can carry the receipt and the
        watermark can move once the run has had it. Best-effort: the engine reports its own
        per-source failures, and an engine that raises outright costs the run nothing but the
        block.
        """
        try:
            bundle = self._update_engine.collect(quest or {}, card_id=quest_id, card_kind="quest")
            self._context_bundle = bundle
            return bundle.as_prompt_block() or ""
        except Exception:  # noqa: BLE001 -- context is never worth failing a run over
            log.warning("context updates unavailable for quest %s", quest_id, exc_info=True)
            self._context_bundle = None
            return ""

    def _fetch_quest_captures(self, quest_id: Optional[str]) -> str:
        """The latest unacted insights assigned to THIS quest, rendered, or "" when there are none.

        Quest-scoped: a task with no quest id gets nothing, and the person's insights assigned to
        other quests are never read.
        """
        if not quest_id:
            return ""
        try:
            from .insights import collect_quest_insights
            return collect_quest_insights(self._client, quest_id).as_text()
        except Exception:  # noqa: BLE001
            return ""  # never let a context extra break a run
