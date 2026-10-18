"""What a pass knows about a quest: reflections, insights, previous period, next steps, recurring tasks.

A mixin of ``AutopilotPass`` (see ``autopilot.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import timezone

from .reflections import DEFAULT_PERIODS, ReflectionContext, collect_reflections
from .insights import InsightsContext, collect_quest_insights
from .quest_folder_sync import publish_next_steps, read_next_steps
from .autopilot_personas import PERSONA_HELD, persona_entries_on_duty, resolve_task_persona
from .autopilot_defaults import (
    AUTOPILOT_PASS_KIND,
    AUTOPILOT_WORK_KIND,
    DEFAULT_ASSISTANT_REP_ID,
    _FINISHED_TASK_STATUSES,
    _MAX_PREVIOUS_TASKS,
    _SCOPE_ORDER,
    log,
)
from .autopilot_periods import (
    _parse_dt,
    _truthy,
    previous_period_bounds,
    previous_period_key,
    select_period_goals,
)
from .autopilot_result import AutopilotResult
from .autopilot_briefs import next_steps_from_pass, render_last_run_output, select_last_run_output


class PassContextMixin:
    """What a pass knows about a quest: reflections, insights, previous period, next steps, recurring tasks."""

    def _team_for(self, quest_id: str) -> str:
        """The team THIS quest's reads and writes belong on.

        The resolver when one was injected (a lane serving several teams), else the lane's single
        configured team -- which is what every call site used unconditionally before, so a
        consumer that injects nothing sees no change at all. A resolver that cannot answer, or
        raises, falls back the same way rather than guessing.
        """
        if self._team_resolver is not None and quest_id:
            try:
                return self._team_resolver(quest_id) or self._team_id
            except Exception:  # noqa: BLE001 -- never fail a pass over team resolution
                log.info("autopilot: could not resolve the team for quest %s; using the lane's "
                         "own team", quest_id, exc_info=True)
        return self._team_id

    def _persona_label(self, rep_id: Optional[str]) -> Optional[str]:
        """A rep's human display name, falling back to the raw id.

        "Act as rep_09d389aeb9ff" is what a task says when nobody looks up the name, and it is
        unreadable to the human reviewing the task and useless to a consumer that resolves personas
        by name. The id remains authoritative (it rides in ``assignee_rep_id``); this is purely for
        the prose. Cached per pass, and any lookup failure degrades to the id rather than failing
        the batch.
        """
        if not rep_id:
            return None
        rep_id = str(rep_id)
        if rep_id in self._persona_names:
            return self._persona_names[rep_id]
        label = rep_id
        getter = getattr(self._client, "get_ai_profile", None)
        if callable(getter):
            try:
                profile = getter(rep_id, team_id=self._team_id or None) or {}
                label = str(profile.get("display_name") or "").strip() or rep_id
            except Exception:  # noqa: BLE001 -- a name is cosmetic; never fail a batch over it
                log.info("autopilot: could not resolve a display name for %s", rep_id,
                         exc_info=True)
        self._persona_names[rep_id] = label
        return label

    def instructions_source(self, persona: Optional[str],
                             persona_instructions: Optional[str],
                             instructions: Optional[str]) -> str:
        """Which briefs this batch is carrying, in words, for the dry-run report.

        BOTH slots are always named, because both are always filled: the character's brief governs
        where the two disagree, but the quest's rides into the same run, and a report that hid it
        would be describing a shorter brief than the one that gets sent. A slot the person has not
        written is named as the built-in default for that level rather than as theirs, which is the
        whole value of this line on a quest nobody has configured yet, and the thing a person
        checking their setup is actually looking for: whether the text they wrote is the text that
        will run.
        """
        who = self._persona_label(persona) or "this character"
        return ", plus ".join((
            f"{who}'s own standing instructions" if persona_instructions
            else "the built-in default brief for a character",
            "this quest's standing instructions" if instructions
            else "the built-in default brief for a quest",
        ))

    def _reflection_periods(self, scope_label: str) -> Tuple[str, ...]:
        """Which period reviews to consult for a quest at ``scope_label``, finest match first.

        A quest working on a monthly target is best informed by the month review; a daily or
        unscoped quest has no matching period, so it falls back to the module default. The quest's
        own scope choosing the period is the point: nothing here hardcodes that a week matters more
        than a quarter, and a deployment that plans in quarters gets its quarter review read.
        """
        scope = (scope_label or "").split(":", 1)[0]
        defaults = tuple(DEFAULT_PERIODS)
        if scope in ("week", "month", "quarter", "year"):
            return (scope,) + tuple(p for p in defaults if p != scope)
        return defaults

    def _reflections(self, scope_label: str) -> ReflectionContext:
        """The person's latest PERIOD reflection, fetched once per pass per period order.

        Best-effort by construction (``collect_reflections`` never raises and returns an empty
        context for a client that has no reflection methods at all), so a backend or client without
        these endpoints composes exactly the batch text it composed before.

        ``include_daily=False`` on purpose: the daily plan's free text is the same for every quest
        this pass runs, so folding it into every quest's batch handed each quest the person's whole
        reflection regardless of what it concerned -- a quest about one area learning what they
        wrote about a completely different one. That text is now split into per-quest notes at
        reflection time (see quest-backend's daily-plan reflection-notes endpoint), and a quest's
        own notes already reach its own batch by the ordinary quest-reference path, correctly
        scoped to just that quest. The period review is left alone: unlike the daily entry it is
        one review of the whole period, not something with an obvious single quest to attach to.
        """
        periods = self._reflection_periods(scope_label)
        cached = self._reflection_cache.get(periods)
        if cached is None:
            cached = collect_reflections(self._client, periods=periods, now=self._now(),
                                          include_daily=False)
            self._reflection_cache[periods] = cached
            if cached.has_any():
                log.info("autopilot: read the person's reflection (daily=%s, period=%s)",
                         cached.daily_date or "none", cached.period or "none")
            else:
                log.info("autopilot: no reflection on record (checked %s)",
                         ", ".join(cached.checked_periods) or "no period")
        return cached

    def _insights(self, autopilot_cfg: Dict[str, Any], quest_id: str = "") -> InsightsContext:
        """The latest unacted insights ASSIGNED to this quest since its last pass.

        Quest-scoped by construction: the read asks the backend for this quest's insights only
        (``collect_quest_insights``), so a capture assigned to another quest, or to none, never
        reaches this quest's brief. The per-pass cache is keyed by quest id, so two quests in one
        pass each get their own set and never share one. A quest with no id gets no insights.

        ``since`` is the ``last_pass_at`` this pass is about to overwrite, the pass's own record of
        when it last looked at this quest. A quest that has never had a pass (no ``last_pass_at``,
        or an unparsable one) sees the whole default window rather than nothing.

        Best-effort by construction (``collect_quest_insights`` never raises), so a backend or
        client without the route composes exactly the batch text it composed before.
        """
        qid = str(quest_id or "").strip()
        if not qid:
            return InsightsContext()
        since = _parse_dt(autopilot_cfg.get("last_pass_at"))
        key = (qid, since)
        cached = self._insights_cache.get(key)
        if cached is None:
            cached = collect_quest_insights(self._client, qid, since=since, now=self._now())
            self._insights_cache[key] = cached
            log.info("autopilot: read %d unacted insight(s) assigned to quest %s",
                     len(cached.insights), qid)
        return cached

    def _adopts_recurring(self, autopilot_cfg: Dict[str, Any]) -> bool:
        """Whether to adopt this quest's recurring tasks: the QUEST's own setting when it states
        one, otherwise the consumer's default.

        The fallback is not just convenience. ``adopt_recurring`` is a newer field, so a backend
        that predates it stores nothing and every quest would silently read as off, with no way for
        a deployment to turn the behavior on at all until it upgrades.
        """
        stated = autopilot_cfg.get("adopt_recurring")
        if stated is None:
            return bool(self._adopt_recurring_default)
        return _truthy(stated)

    def _batches_for_pass(self, autopilot_cfg: Dict[str, Any], adopted: List[Dict[str, Any]]
                          ) -> List[Tuple[Optional[str], List[Dict[str, Any]]]]:
        """This pass's batches for one quest: ``[(persona, adopted tasks for them), ...]``.

        ONE PER CHARACTER ON DUTY TODAY, in the roster's own on-duty order (day-restricted entries
        before unrestricted ones). Being on duty is the whole condition, because every character on
        duty now has a brief to work to at both levels: what the person wrote there, or the
        built-in default for it. An ``instructions_only`` entry is included like any other -- the flag only keeps that
        character out of the routing that hands an UNASSIGNED recurring task to somebody.

        A quest with NO roster gets exactly ONE batch, for whoever the consumer's fallback resolver
        names, or for the plain assistant when it names nobody. A quest rostered to
        DEFAULT_ASSISTANT_REP_ID gets that same batch by the ordinary route, which is the point of
        that marker: it makes the fallback a roster entry, so it can carry days and a brief like
        any other. The caller resolves the marker to the fallback (``_batch_persona``) before the
        batch is created. That is the unconfigured quest, and
        it still does its default briefs rather than nothing. (An empty on-duty list can only mean
        an empty roster here: ``_gate_quest``'s day rule has already skipped a quest whose roster
        names people but puts none of them on duty today.)

        Adopted tasks are then filed into the batch of whoever ``resolve_task_persona`` names, so
        an unassigned occurrence rides along with the character already working the quest instead
        of spawning a second run for the same character, and one naming somebody else gets its own
        batch. Anything the day rule holds is skipped here rather than re-routed: the caller has
        already held and reported it, and this is the belt to that braces, so a held task can never
        become a batch keyed on a marker that names no character.
        """
        now = self._quest_now(autopilot_cfg)  # the roster's day, in the quest's own zone
        merged: Dict[Optional[str], List[Dict[str, Any]]] = {}
        order: List[Optional[str]] = []

        def slot(persona: Optional[str]) -> List[Dict[str, Any]]:
            if persona not in merged:
                merged[persona] = []
                order.append(persona)
            return merged[persona]

        on_duty = persona_entries_on_duty(autopilot_cfg, now)
        for entry in on_duty:
            rep_id = entry.get("rep_id")
            if rep_id:
                slot(str(rep_id))
        if not on_duty:
            slot(self._fallback_persona())
        for task in adopted:
            persona = resolve_task_persona(task, autopilot_cfg, now, self._persona_resolver)
            if persona is PERSONA_HELD:
                continue
            slot(str(persona) if persona else None).append(task)
        return [(persona, merged[persona]) for persona in order]

    def _batch_persona(self, persona: Optional[str]) -> Optional[str]:
        """The character a batch actually runs as, given the roster key it was slotted under.

        Only DEFAULT_ASSISTANT_REP_ID is translated: it is the person having written down the
        fallback this quest would have had anyway, so it resolves to exactly that and to nothing
        else. Every other key is a real rep_id and passes through.
        """
        if persona == DEFAULT_ASSISTANT_REP_ID:
            return self._fallback_persona()
        return persona

    def _fallback_persona(self) -> Optional[str]:
        """The consumer's own answer to "who is this quest's character", or None.

        Reached only for a quest whose roster names nobody, which is every quest that never
        configured one. A resolver that raises is treated as no match: a bad consumer callback must
        never break a pass.
        """
        if self._persona_resolver is None:
            return None
        try:
            resolved = self._persona_resolver({})
            return str(resolved) if resolved else None
        except Exception:  # noqa: BLE001 -- a bad fallback must never break a pass
            log.info("autopilot: persona fallback_resolver raised while resolving this quest's "
                     "batch; treating as no match", exc_info=True)
            return None

    def _close_adopted(self, tasks: List[Dict[str, Any]], batch_task_id: str, quest_id: str,
                       result: AutopilotResult) -> None:
        """Close each adopted occurrence, pointing at the batch that took over its work.

        Ordering is deliberate: this runs only AFTER the batch task was created successfully. If a
        close fails, the occurrence stays queued and runs on its own later -- duplicated work, but
        never LOST work, which is the correct direction to fail in. The reverse order would close
        the occurrence and then discover the batch could not be created, and the user's recurring
        task would simply have evaporated for the day.

        Note this does mark work done before the batch has actually run. If the batch later fails,
        it fails visibly as a failed task on the quest, and (for a daily series) the next
        occurrence arrives tomorrow.
        """
        for t in tasks:
            task_id = str(t.get("id") or t.get("task_id") or "")
            if not task_id:
                continue
            try:
                self._client.update_task(task_id, {
                    "status": "done",
                    "result": (f"Adopted by the autopilot pass and folded into task "
                               f"{batch_task_id}, which carries out this task's instructions as "
                               f"part of this period's batch for the quest."),
                })
            except Exception as e:  # noqa: BLE001 -- never fail a pass over bookkeeping
                log.warning("autopilot: could not close adopted task %s (%s); it stays queued "
                            "and will run on its own", task_id, e)
                result.bookkeeping_warnings.append({
                    "quest_id": quest_id,
                    "detail": (f"adopted task {task_id} was folded into {batch_task_id} but could "
                               f"not be closed ({type(e).__name__}), so it will ALSO run "
                               f"separately: expect duplicated work, not missing work"),
                })

    # --- the quest's canonical next-steps artifact --------------------------------------------

    def _read_next_steps(self, quest_id: str) -> Optional[str]:
        """The standing next-steps artifact in this quest's mapped folder, or None.

        Best-effort in both directions: a quest with no mapped folder simply has no artifact, and an
        unreadable file must not stop the pass, because a missing plan-of-record degrades the batch
        text rather than invalidating it.
        """
        folder = self._quest_folder_map.get(str(quest_id))
        if not folder:
            return None
        try:
            return read_next_steps(folder)
        except Exception:  # noqa: BLE001 -- reading the artifact is never worth failing a pass
            log.info("autopilot: could not read next steps for quest %s", quest_id, exc_info=True)
            return None

    def _refresh_next_steps(self, quest_id: str, current_goals: List[Dict[str, Any]],
                            adopted: List[Dict[str, Any]], scope_label: str,
                            previous: Optional[Dict[str, Any]],
                            result: AutopilotResult, *,
                            quest_label: str = "",
                            reflection_note: str = "",
                            insights_note: str = "") -> None:
        """Write this pass's conclusion as the quest's canonical next steps (folder + Quest).

        Only called when the pass actually PRODUCED work, and never on a dry run. A pass that
        produced nothing must leave the artifact alone: overwriting a considered answer with "no
        current target" on a day the quest happens to be gated or quiet would make the artifact less
        trustworthy than the guesswork it replaces.
        """
        folder = self._quest_folder_map.get(str(quest_id))
        if not folder:
            return
        updated = self._now().astimezone(timezone.utc).strftime("%Y-%m-%d")
        next_steps = next_steps_from_pass(current_goals, adopted, scope_label=scope_label,
                                          updated=updated, previous=previous,
                                          reflection_note=reflection_note,
                                          insights_note=insights_note)
        try:
            published = publish_next_steps(self._client, quest_id, folder, next_steps)
        except Exception as e:  # noqa: BLE001 -- the artifact must never fail an otherwise-good pass
            log.warning("autopilot: could not refresh next steps for quest %s", quest_id,
                        exc_info=True)
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "quest_label": quest_label or quest_id,
                 "detail": f"the next-steps artifact was not refreshed ({type(e).__name__}: {e})"})
            return
        result.next_steps_refreshed.append({
            "quest_id": quest_id, "path": published.sync_path,
            "quest_target": published.quest_target,
            "quest_label": quest_label or quest_id,
        })
        if published.detail:
            # The local file is current either way; what did not happen is the Quest-side write, and
            # a silently local-only artifact is how the two views drift apart again.
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "quest_label": quest_label or quest_id,
                 "detail": f"next-steps artifact written locally, but on Quest: {published.detail}"})

    def _previous_period_summary(self, quest_id: str, goals_payload: Dict[str, Any],
                                 scope_label: str,
                                 tasks: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        """Goals and finished tasks from the period before this one, for run-to-run continuity.

        Returns None for an unscoped quest (there is no previous period to speak of).

        ``tasks`` is this quest's task list, fetched ONCE by the caller (the per-quest pass body)
        and shared with ``_last_run_output`` rather than each of them calling
        ``list_tasks(goal_id=quest_id)`` for itself -- see the call site's own comment for why.
        ``None`` here means that shared fetch failed, and this degrades to a goals-only summary
        exactly as it did back when this method did its own fetching and catching: best-effort,
        never losing the goals half over a tasks-side failure."""
        scope = (scope_label or "").split(":", 1)[0]
        if scope not in _SCOPE_ORDER:
            return None
        previous_key = previous_period_key(scope, self._now())
        if not previous_key:
            return None
        summary: Dict[str, Any] = {
            "period": f"{scope}:{previous_key}",
            "goals": select_period_goals(goals_payload, scope, previous_key),
            "tasks": [],
        }
        if tasks is None:
            return summary
        bounds = previous_period_bounds(scope, self._now())
        if bounds is None:
            return summary
        window_start, window_end = bounds
        finished = []
        for t in tasks:
            if str(t.get("status", "")).strip().lower() not in _FINISHED_TASK_STATUSES:
                continue
            when = _parse_dt(t.get("worked_at") or t.get("updated_at") or t.get("created_at"))
            if when is None or not (window_start <= when < window_end):
                continue
            finished.append(t)
        summary["tasks"] = finished[-_MAX_PREVIOUS_TASKS:]
        return summary

    def _last_run_output(self, tasks: List[Dict[str, Any]],
                         previous: Optional[Dict[str, Any]]) -> Optional[str]:
        """The rendered last-run-output block for this quest's batch text, or None.

        Thin orchestration over the two module-level pure functions: ``select_last_run_output``
        picks the task (its own docstring has the selection rules -- no period window, most recent
        wins, a rolled-up pass row is preferred over its own child), ``render_last_run_output``
        renders it. The one thing done HERE, rather than in either pure function, is de-duplicating
        against ``previous``: the SAME task can be both "the most recent thing this quest ever
        produced" (selected here) and "a task that finished inside the previous calendar period"
        (already sitting in ``previous["tasks"]``, put there by ``_previous_period_summary`` --
        this is the common case for a quest running at least as often as it looks back, since the
        previous period's most recent finished task and the quest's most recent finished task are
        often the same row). Left alone, that task would print TWICE in one batch: once here in
        full, and once again a few lines later as its own 280-character stub in
        ``_summarize_previous``'s per-task line. Dropping it from ``previous["tasks"]`` (mutating
        the dict in place; the caller still needs the rest of it) is cheaper and more honest than
        shortening either block to paper over the overlap: the reader sees the work once, in full,
        exactly where the more specific of the two blocks puts it.
        """
        task = select_last_run_output(tasks)
        if task is None:
            return None
        if previous is not None:
            task_id = str(task.get("id") or task.get("task_id") or "")
            if task_id:
                previous["tasks"] = [t for t in previous.get("tasks") or []
                                     if str(t.get("id") or t.get("task_id") or "") != task_id]
        return render_last_run_output(task)

    # --- adopting the quest's own recurring tasks (opt-in per quest) --------------------------

    def _due_recurring_tasks(self, quest_id: str) -> List[Dict[str, Any]]:
        """This quest's queued recurring occurrences that are due now and are not autopilot's own.

        The ``task_kind`` exclusion is not a nicety. Adopting the recurring PASS task would fold
        the scanner into the very batch it is creating and then close it, killing the series that
        drives autopilot at all; adopting autopilot's own work batches would let a pass swallow its
        previous output. Neither is recoverable from inside a pass, so both are excluded here.
        """
        tasks = self._client.list_tasks(
            team_id=self._team_for(quest_id) or None, goal_id=quest_id,
            status="queued") or []
        today = self._now().date()
        due: List[Dict[str, Any]] = []
        for t in tasks:
            if self._is_autopilot_authored(t) or t.get("task_kind") in (AUTOPILOT_PASS_KIND,
                                                                        AUTOPILOT_WORK_KIND):
                continue
            if not (t.get("series_id") or t.get("recurrence")):
                continue  # a one-off task the user queued; not ours to take over
            scheduled = str(t.get("scheduled_date") or "").strip()
            if scheduled:
                parsed = _parse_dt(scheduled)
                if parsed is not None and parsed.date() > today:
                    continue  # scheduled for a later day
            due.append(t)
        return due
