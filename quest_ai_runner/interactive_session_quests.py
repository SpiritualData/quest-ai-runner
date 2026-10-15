"""Quest binding for the session: remote quest list, picker, per-turn grounding and next-steps refresh.

A mixin of ``InteractiveSession`` (see ``interactive_session.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple
from pathlib import Path


class QuestsMixin:
    """Quest binding for the session: remote quest list, picker, per-turn grounding and next-steps refresh."""

    def fetch_remote_quests(self) -> None:
        """Fill ``remote_quests`` from the Quest API (every team this lane polls, plus own quests)."""
        try:
            client = self._quest_client()
            if client is None:
                return
            from .runner.quest_folder_index import reachable_quests
            team_ids = [getattr(self._cfg, "team_id", None) or ""]
            team_ids += [t.strip() for t in os.getenv("QUEST_TEAM_IDS", "").split(",") if t.strip()]
            self.remote_quests = reachable_quests(client, list(dict.fromkeys(team_ids)))
            pinned = self.pinned_quest
            if pinned is not None and not pinned.folder and not pinned.title:
                # A quest selected by id before its details were known: fill them in.
                self.pinned_quest = next(
                    (q for q in self.remote_quests if q.quest_id == pinned.quest_id), pinned)
        except Exception:  # noqa: BLE001 — the menu just shows local quests
            self.remote_quests = []
        for listener in list(getattr(self, "quest_list_listeners", [])):
            try:
                listener()
            except Exception:  # noqa: BLE001
                pass

    def no_quests_reason(self) -> str:
        """Why the quest list is empty, in words a person can act on."""
        where = self.quest_search_root or "the corpus root"
        parts = [f"No quests found. No folder under {where} has a QUEST_SYNC.md"]
        if self._quest_client() is None:
            parts.append("and this chat is not signed in to Quest (QUEST_BASE_URL and "
                         "QUEST_API_KEY are not set), so it cannot list your Quest quests")
        else:
            parts.append("and Quest returned no quests for this account")
        return ", ".join(parts) + "."

    def quest_choices(self) -> list:
        """Every quest a person can pick: local synced folders first, then Quest-only ones."""
        from .runner.quest_folder_index import merge_quests
        return merge_quests(self.quest_folders, self.remote_quests)

    def quest_for_turn(self, user_text: str):
        """The synced quest this turn is about: the pinned one, else a clear match, else None.

        A follow-up that names nothing ("and what is next for it?") is matched together with the
        previous user message, so a conversation about a quest stays grounded in it.
        """
        if self.pinned_quest is not None:
            return self.pinned_quest
        if self.quest_match_mode == "none" or not self.quest_folders:
            return None
        return self.choose_quest(user_text, self.quest_folders)

    def choose_quest(self, user_text: str, choices) -> Optional["QuestFolder"]:
        """Pick the quest a message is about with ONE LLM judgment over ``choices``.

        An explicit quest id typed in the message decides outright (an exact id, not a guess).
        Otherwise the judge decides, seeing the previous message so a follow-up stays on its quest
        and the HOME quest (the folder this session runs in) as the default. Decisions are cached
        per (previous message, message), so retries and re-renders never call again.
        """
        lowered = (user_text or "").lower()
        for entry in choices:
            if entry.quest_id and entry.quest_id.lower() in lowered:
                return entry
        if not choices:
            return None
        home = getattr(self, "home_quest", None)
        previous = self._session_history[-1][0] if self._session_history else ""
        key = (previous, user_text, tuple(q.quest_id for q in choices))
        cache = self.__dict__.setdefault("quest_choice_cache", {})
        if key not in cache:
            quests = [{"quest_id": q.quest_id, "title": q.title or Path(q.folder).name,
                       "state": q.current_state} for q in choices]
            cache[key] = self._orch.judge_quest_for_turn(
                user_text, previous, quests, home.quest_id if home is not None else None)
        chosen = cache[key]
        return next((q for q in choices if q.quest_id == chosen), None)

    def turn_grounding(self, user_text: str) -> Tuple[Optional[str], Optional[dict]]:
        """``(rep_preamble, context_meta)`` for a turn, grounded in the quest it is about.

        A quest synced to a local folder has a context card (see ``sync_quest_cards``), so the
        match only tells card selection to put that card first (``priority_card_ids``) and to
        favour the quest folder's other cards (``quest_folder``); no extra prompt text. A quest
        known only from Quest (no folder, so no local card) is added to the preamble instead.
        """
        quest = None
        try:
            quest = self.quest_for_turn(user_text)
        except Exception:  # noqa: BLE001 — grounding is a bonus, never a reason to fail a turn
            quest = None
        self.turn_quest = quest
        standing = getattr(self, "_standing_next_steps", None)
        if quest is not None and standing is not None and standing.quest_id == quest.quest_id:
            # The quest's card already carries its next steps; do not say them twice.
            self._standing_next_steps = None
            try:
                base = self._effective_preamble()
            finally:
                self._standing_next_steps = standing
        else:
            base = self._effective_preamble()
        if quest is None:
            return base, None
        meta: dict = {}
        if quest is self.pinned_quest:
            # SELECTED, not guessed: bind the turn to it exactly as choosing a quest in the Quest AI
            # chat does (quest-backend passes the chosen quest as quest_id and in quest_ids), so the
            # scope fence keeps other quests' context out. A guessed match stays a soft nudge.
            meta["quest_ids"] = [quest.quest_id]
        if quest.folder:
            from .runner.quest_folder_index import quest_card_id
            meta.update({"priority_card_ids": [quest_card_id(quest.quest_id)],
                         "quest_folder": quest.folder})
            return base, meta
        from .runner.quest_folder_index import render_quest_folder_context
        block = render_quest_folder_context(quest)
        return (f"{base}\n\n{block}" if base else block), (meta or None)

    def bound_quest_id(self) -> Optional[str]:
        """The quest id this session's turns are bound to: a selected quest, else /goal's."""
        if self.pinned_quest is not None:
            return self.pinned_quest.quest_id
        return self._goal_id

    def cmd_quest(self, arg: str) -> None:
        """/quest [auto|none|<name or id>]: how turns are grounded in a synced quest folder."""
        from .runner.quest_folder_index import describe_match
        c = self._console
        arg = arg.strip()
        low = arg.lower()
        if low in ("none", "off", "no"):
            self.quest_match_mode = "none"
            self.pinned_quest = None
            self._goal_id = None
            self._persist_session_state()
            c.dim("  Quest matching off: no quest is added to your messages. /quest auto turns it back on.")
            return
        if low in ("auto", "on"):
            self.quest_match_mode = "auto"
            self.pinned_quest = None
            self._goal_id = None
            self._persist_session_state()
            c.dim("  Quest matching on: each message is matched to the synced quest it is about.")
            return
        if arg:
            choices = self.quest_choices()
            found = next((q for q in choices if q.quest_id == arg), None) \
                or self.choose_quest(arg, choices)
            if found is None:
                c.dim(f"  No synced quest clearly matches {arg!r}. /quest lists them.")
                return
            self.pinned_quest = found
            c.dim(f"  Selected for this conversation: {describe_match(found)}")
            c.dim("  Every message is now about this quest, as when you choose a quest in Quest AI "
                  "chat. /quest auto goes back to matching each message.")
            return
        if self.pinned_quest is not None:
            c.dim(f"  Selected {describe_match(self.pinned_quest)}")
        else:
            c.dim(f"  Quest matching: {self.quest_match_mode}")
        choices = self.quest_choices()
        if not choices:
            c.dim("  " + self.no_quests_reason())
            return
        c.dim("  Quests (type /quest and a space to pick one):")
        for q in choices:
            c.dim(f"    {q.title or q.quest_id}  ·  {q.folder or 'not synced locally'}")
        c.dim("  /quest none  never add a quest  ·  /quest auto  match each message  ·  "
              "/quest <name>  pin one")

    def _maybe_refresh_next_steps(self, final) -> None:
        """Write this turn's conclusion back as the quest's standing next steps, when it earns it.

        Called once per completed turn from the UI's turn-completion path, and deliberately thin:
        every judgment about WHETHER a turn warrants a refresh, and what the refreshed artifact
        says, lives in ``runner/session_next_steps.next_steps_from_turn`` — the half no UI owns, so
        the trigger cannot drift between them. Conservative by design: only a turn that actually
        executed work and left some of it unfinished replaces the standing answer.

        Never raises, and never reports a write that did not happen: ``refresh_from_turn`` returns
        None for every declined or failed case.
        """
        standing = getattr(self, "_standing_next_steps", None)
        if standing is None or final is None:
            return
        try:
            from .runner.session_next_steps import refresh_from_turn
            result = refresh_from_turn(
                self._quest_client(), standing,
                kind=getattr(final, "kind", "") or "",
                goals=list(getattr(final, "goals", None) or []),
                deep_results=list(getattr(final, "deep_results", None) or []),
            )
        except Exception:  # noqa: BLE001 — the artifact must never fail an otherwise-good turn
            return
        if result is None:
            return
        self._console.dim(f"  Standing next steps refreshed: {result.sync_path}")
        if result.detail:
            # The local file is current either way; what may not have happened is the Quest-side
            # write, and a silently local-only artifact is how the two views drift apart again.
            self._console.dim(f"  On Quest: {result.detail}")

    # -- Quest client (lazy, only when credentials are configured) -------------

    def _quest_client(self):
        """Return a QuestClient if Quest credentials are configured, else None."""
        try:
            from .runner.quest_client import QuestClient
            url = getattr(self._cfg, "quest_base_url", "") or ""
            key = getattr(self._cfg, "quest_api_key", "") or ""
            if not url or not key:
                return None
            return QuestClient(url, key,
                               team_id=getattr(self._cfg, "team_id", None) or None)
        except Exception:  # noqa: BLE001
            return None
