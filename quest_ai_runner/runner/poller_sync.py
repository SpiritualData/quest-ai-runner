"""Two-way sync: rep files and preambles, quest folders and quest goals.

A mixin of ``Poller`` (see ``poller.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from pathlib import Path

from .executor import task_scope_tags
from .poller_schedule import log


class SyncMixin:
    """Two-way sync: rep files and preambles, quest folders and quest goals."""

    def _resolve_rep_target(self, task: Dict[str, Any]) -> Optional[tuple]:
        """Map a task to ``(user_id, skill_dir)`` via the opt-in ``rep_sync_resolver`` (or None).

        Resolved ONCE per task and reused for both the handler label (stamped on claim) and the
        rep skill-file sync. Never raises — a missing resolver or a bad one yields None."""
        resolver = getattr(self.cfg, "rep_sync_resolver", None)
        if resolver is None:
            return None
        try:
            return resolver(task)
        except Exception as e:  # noqa: BLE001 — a bad resolver must never break execution
            log.info("rep_sync_resolver raised (%s) — skipping rep resolution", e)
            return None

    def _handler_label(self, target: Optional[tuple]) -> Optional[str]:
        """Derive the handler label stamped on claim — WHO ran this task.

        If the rep resolver mapped the task to a ``(user_id, skill_dir)``, the handler is the
        basename of ``skill_dir`` (the rep slug, e.g. "alex"/"sam"). Otherwise fall back to
        the runner's configured ``runner_label`` (or None when neither is available)."""
        if target:
            _user_id, skill_dir = target
            slug = Path(str(skill_dir)).name
            if slug:
                return slug
        return self.cfg.runner_label or None

    def _rep_context_dirs(self, user_id: str) -> tuple:
        """Return ``(cards_dir, rep_notes_dir, rep_turns_dir)`` for a given rep.

        Deterministic from ``(user_id, cfg)`` so callers can reconstruct it cheaply without
        storing extra state.
        """
        import os as _os
        root = self.cfg.corpus_root or _os.getcwd()
        cards_dir = self.cfg.context_cards_dir or _os.path.join(root, ".quest-context")
        rep_notes_dir = _os.path.join(cards_dir, "reps", user_id, "notes")
        rep_turns_dir = _os.path.join(cards_dir, "reps", user_id, "turns")
        return cards_dir, rep_notes_dir, rep_turns_dir

    @staticmethod
    def _task_rep_preamble(task: Dict[str, Any]) -> Optional[str]:
        """The persona a TASK DOCUMENT supplies for itself: its optional ``rep_preamble`` field.

        A consumer that already knows the voice a task must speak in can stamp it on the task when
        it queues the work, and the runner will use it as the deep run's persona (and therefore as
        the voice of the fold-back "done" report) with no rep profile and no resolver wired. The
        motivating case is a task deferred out of a live conversation: the queueing side stamps that
        conversation's own system prompt on the task, so the report that lands back in the
        conversation sounds like the replies already in it.

        This is a FALLBACK only: when a rep IS resolved for the task, that rep's pulled persona wins
        (see ``_handle_one``). Anything that is not a non-empty string is ignored, so a malformed or
        placeholder field can never poison the run.
        """
        value = task.get("rep_preamble")
        if isinstance(value, str) and value.strip():
            return value
        return None

    def _pull_rep_for(self, task: Dict[str, Any], target: Optional[tuple] = None) -> Optional[str]:
        """Best-effort PRE-run pull, gated on direction; returns the rep's per-run preamble or None.

        Only fires when the consumer wired a ``rep_sync_resolver`` (OFF by default) AND the
        configured ``rep_sync_direction`` includes a pull ("pull" or "both"). ``target`` is the
        already-resolved ``(user_id, skill_dir)`` (or None to skip). We ``pull_rep_to_skill`` so
        the local skill file reflects the current persona/corrections, then read its MANAGED
        sections and compose them with the runner's context doctrine into a per-run preamble the
        executor injects into the deep run — so the task runs AS that rep by default, no extra
        consumer glue.

        Also builds rep-specific NoteContextStore and TurnContextStore instances, syncs the
        note store from the just-pulled profile, assembles both into context blocks, and appends
        them to the preamble.

        Never raises: a sync failure is logged and the run proceeds (with the previously synced
        file, and no preamble from this pull)."""
        if not target:
            return None
        if self.cfg.rep_sync_direction not in ("pull", "both"):
            return None  # direction is push-only: do not pull before the run
        user_id, skill_dir = target
        team_id = task.get("team_id") or self.cfg.team_id
        try:
            from pathlib import Path as _Path

            from ..core.context_doctrine import compose_deep_preamble
            from ..core.note_context_store import NoteContextStore
            from ..core.turn_context_store import TurnContextStore
            from .rep_sync import SKILL_FILE_NAME, parse_skill_file, pull_rep_to_skill

            _cards_dir, rep_notes_dir, rep_turns_dir = self._rep_context_dirs(user_id)

            # Build the note store and pass it to pull so the sync happens in one call.
            note_store = NoteContextStore(rep_notes_dir)
            pull_rep_to_skill(self.client, team_id, user_id, skill_dir, note_store=note_store)

            # Read the rep's persona + learned corrections back out of the just-pulled file.
            skill_text = (_Path(skill_dir) / SKILL_FILE_NAME).read_text(encoding="utf-8")

            # Assemble rep-specific note and turn context for the preamble.
            task_text = task.get("text") or task.get("title") or ""
            note_ctx = note_store.assemble(task_text)
            rep_turn_store = TurnContextStore(turns_dir=rep_turns_dir)
            # ONE history per prompt. When the lane's own context assembler already renders past
            # turns (the org-wide store, fenced and relevance-filtered per goal), the rep's copy of
            # the same turns is not rendered again. Otherwise it is, fenced to this task's quest:
            # a rep works many quests, and its turns on one must not ride into a run on another.
            from ..core.turn_context_store import assembler_renders_turns
            lane_assembler = getattr(self._orch(), "context_assembler", None)
            turn_view = ""
            if not assembler_renders_turns(lane_assembler):
                turn_view = rep_turn_store.assemble(
                    task_text, meta={"scope_tags": task_scope_tags(task),
                                     "task_id": task.get("task_id") or task.get("id")}
                ).context_view

            return self._build_rep_preamble(
                skill_text, compose_deep_preamble, parse_skill_file,
                note_ctx_view=note_ctx.context_view,
                turn_ctx_view=turn_view,
            )
        except Exception as e:  # noqa: BLE001 — best-effort, like progress posting/heartbeat
            log.info("rep pull for %s failed (%s) — running with existing skill file", user_id, e)
            return None

    @staticmethod
    def _build_rep_preamble(skill_text: str, compose_deep_preamble, parse_skill_file,
                            *, note_ctx_view: str = "", turn_ctx_view: str = "") -> Optional[str]:
        """Compose a deep-run preamble from a skill file's MANAGED sections (persona + learned).

        Generic: it only knows ``persona`` + ``learned_notes`` (the rep_sync managed shape) and the
        runner's context doctrine. Optionally appends rep-specific note context (learned corrections
        from the NoteContextStore) and turn context (past task history from the TurnContextStore).
        Returns None when the file carries no rep identity to inject."""
        parsed = parse_skill_file(skill_text or "")
        persona = (parsed.get("persona") or "").strip()
        learned = parsed.get("learned_notes") or []
        if not persona and not learned and not note_ctx_view and not turn_ctx_view:
            return None
        parts: List[str] = []
        if persona:
            parts.append("=== ACT AS THIS PERSON (their persona) ===\n" + persona)
        if learned:
            bullets = "\n".join(f"- {str(n.get('text', '')).strip()}"
                                for n in learned if str(n.get("text", "")).strip())
            if bullets:
                parts.append("=== LEARNED CORRECTIONS (apply these) ===\n" + bullets)
        if note_ctx_view:
            parts.append(note_ctx_view)
        if turn_ctx_view:
            parts.append(turn_ctx_view)
        if not parts:
            return None
        # Combine the runner's doctrine with this rep's persona/learned via the existing composer,
        # so deep agents obey the same disciplines AND adopt the rep's identity.
        return compose_deep_preamble("\n\n".join(parts))

    def _push_rep_for(self, task: Dict[str, Any], target: Optional[tuple] = None) -> None:
        """Best-effort POST-run push, gated on direction.

        Fires only when a ``rep_sync_resolver`` resolved a target AND ``rep_sync_direction`` is
        "push" or "both": the rep's local skill file is written back up to its Quest profile after
        the task ran. Never raises — a push failure is logged and the (already reported) task is
        unaffected."""
        if not target:
            return
        if self.cfg.rep_sync_direction not in ("push", "both"):
            return
        user_id, skill_dir = target
        team_id = task.get("team_id") or self.cfg.team_id
        try:
            from .rep_sync import push_skill_to_rep
            push_skill_to_rep(self.client, team_id, user_id, skill_dir)
        except Exception as e:  # noqa: BLE001 — best-effort; a push failure never fails the task
            log.info("rep push for %s failed (%s) — leaving Quest profile unchanged", user_id, e)

    def _record_rep_turn(self, task: Dict[str, Any], target: Optional[tuple],
                         outcome: Any) -> None:
        """Best-effort POST-run: record this task's outcome into the rep's per-rep turn store.

        Only fires when a rep resolver returned a target.  The rep's TurnContextStore lives at
        ``<cards_dir>/reps/<user_id>/turns/`` so it stays namespaced per rep and doesn't pollute
        the org-wide turn store.  Never raises."""
        if not target:
            return
        user_id, _skill_dir = target
        try:
            from ..core.turn_context_store import TurnContextStore
            _cards_dir, _rep_notes_dir, rep_turns_dir = self._rep_context_dirs(user_id)
            task_text = task.get("text") or task.get("title") or ""
            result_text = (getattr(outcome, "result", None) or "").strip()
            rep_turn_store = TurnContextStore(turns_dir=rep_turns_dir)
            rep_turn_store.record(task_text, {"response": result_text,
                                              "scope_tags": task_scope_tags(task),
                                              "task_id": task.get("task_id") or task.get("id")})
        except Exception as e:  # noqa: BLE001 — best-effort; never fails the task
            log.info("rep turn record for %s failed (%s) — continuing", user_id, e)

    # --- quest <-> local folder sync (opt-in, keyed off cfg.quest_folder_map) --

    def _quest_folder_for(self, task: Dict[str, Any]) -> Optional[tuple]:
        """Return ``(quest_id, folder)`` when this task's goal/quest is in ``cfg.quest_folder_map``.

        Unlike the rep resolver, no callable is needed: the task already carries the exact id
        (``goal_id`` for a personal "goal is the hub" quest, else ``quest_id``) that the map is
        keyed on. Returns None when unconfigured or the task's quest isn't mapped."""
        folder_map = getattr(self.cfg, "quest_folder_map", None)
        if not folder_map:
            return None
        qid = task.get("goal_id") or task.get("quest_id")
        if not qid:
            return None
        folder = folder_map.get(str(qid))
        return (str(qid), folder) if folder else None

    def _sync_all_quest_folders(self) -> None:
        """Best-effort: sync EVERY entry in ``cfg.quest_folder_map``, independent of whether a
        task for that quest happens to be due this scan.

        The task-scoped hooks above (``_pull_quest_folder_for``/``_push_quest_folder_for``) only
        fire around a task that carries a mapped goal/quest id, so a folder whose quest never gets
        a task queued against it (e.g. someone just edits it on Quest directly) would otherwise
        never refresh. Calling this once per scan, from ``run_once()``, means every standing
        ``poll``/``run_forever`` process — the normal systemd/cron deployment — keeps every mapped
        folder current on its own poll cadence, with no task required. A per-entry failure (bad
        folder, API error) is logged and never blocks the other entries or the scan itself."""
        folder_map = getattr(self.cfg, "quest_folder_map", None)
        if not folder_map:
            return
        direction = self.cfg.quest_folder_sync_direction
        from .quest_folder_sync import sync_quest_folder
        for quest_id, folder in folder_map.items():
            try:
                sync_quest_folder(self.client, quest_id, folder, direction=direction,
                                  zones=getattr(self.cfg, 'quest_folder_zones', True))
                self._sync_quest_goals(quest_id, folder, direction)
            except Exception as e:  # noqa: BLE001 — one bad folder must not block the others/scan
                log.info("quest-folder periodic sync for %s failed (%s) — will retry next scan",
                         quest_id, e)

    def _sync_quest_goals(self, quest_id: str, folder: str, direction: str) -> None:
        """Best-effort GOALS.md sync for one mapped folder. Never raises.

        Separate from the folder sync rather than folded into it: a quest whose goals cannot be
        listed (an older backend, a permission gap) must still get its QUEST_SYNC.md, and one
        try/except around both would lose that.
        """
        if not getattr(self.cfg, "quest_goal_sync", True):
            return
        try:
            from .quest_goal_sync import sync_quest_goals
            # How many of each goal's check-ins the pulled GOALS.md carries. Passed explicitly
            # rather than left to the function default, or the config field would be dead: a
            # deployment that set it to 0 (or to 10) would still get the default.
            sync_quest_goals(self.client, quest_id, folder, direction=direction,
                             updates_per_goal=int(
                                 getattr(self.cfg, "quest_goal_updates_per_goal", 3) or 0))
        except Exception as e:  # noqa: BLE001 -- goals are additive; never block the scan
            log.info("goal sync for %s skipped (%s) — will retry next scan", quest_id, e)

    def _pull_quest_folder_for(self, task: Dict[str, Any]) -> None:
        """Best-effort PRE-run pull: refresh the mapped folder's QUEST_SYNC.md from Quest.

        Fires only when the task's goal/quest resolves via ``cfg.quest_folder_map`` AND
        ``cfg.quest_folder_sync_direction`` includes a pull ("pull" or "both"). Never raises — a
        sync failure is logged and the run proceeds with whatever was last synced."""
        target = self._quest_folder_for(task)
        if not target:
            return
        if self.cfg.quest_folder_sync_direction not in ("pull", "both"):
            return
        quest_id, folder = target
        try:
            from .quest_folder_sync import pull_quest_to_folder
            pull_quest_to_folder(self.client, quest_id, folder,
                                 zones=getattr(self.cfg, 'quest_folder_zones', True))
            self._sync_quest_goals(quest_id, folder, "pull")
        except Exception as e:  # noqa: BLE001 — best-effort, like the rep pull
            log.info("quest-folder pull for %s failed (%s) — folder left as last synced",
                     quest_id, e)

    def _push_quest_folder_for(self, task: Dict[str, Any]) -> None:
        """Best-effort POST-run push: post any locally-queued notes up to the mapped quest.

        Fires only when the task's goal/quest resolves via ``cfg.quest_folder_map`` AND
        ``cfg.quest_folder_sync_direction`` is "push" or "both". Never raises."""
        target = self._quest_folder_for(task)
        if not target:
            return
        if self.cfg.quest_folder_sync_direction not in ("push", "both"):
            return
        quest_id, folder = target
        try:
            from .quest_folder_sync import push_folder_to_quest
            push_folder_to_quest(self.client, quest_id, folder)
        except Exception as e:  # noqa: BLE001 — best-effort; a push failure never fails the task
            log.info("quest-folder push for %s failed (%s) — leaving Quest notes unchanged",
                     quest_id, e)
