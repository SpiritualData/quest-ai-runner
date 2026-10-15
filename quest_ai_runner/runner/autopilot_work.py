"""Creating the work: tasks, batches, goals, assignee and proposals.

A mixin of ``AutopilotPass`` (see ``autopilot.py``): methods only, state lives on the instance.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from datetime import timezone

from .goal_handoff import choose_assignee, preferences_text
from .autopilot_defaults import (
    AUTOPILOT_TASK_SOURCE,
    AUTOPILOT_WORK_KIND,
    OPEN_TASK_STATUSES,
    PROPOSAL_TEXT_PREFIX,
    log,
)
from .autopilot_result import AutopilotResult, _task_label
from .autopilot_briefs import _batch_title, compose_batch_text, propose_next_goal
from .autopilot_periods import goal_period_for_scope


class WorkMixin:
    """Creating the work: tasks, batches, goals, assignee and proposals."""

    # --- side effects --------------------------------------------------------------------------

    def _create_autopilot_task(self, quest: Dict[str, Any], quest_id: str, text: str, mode: str,
                               persona: Optional[str] = None,
                               title: Optional[str] = None,
                               force_suggested: bool = False) -> Optional[str]:
        """Create ONE autopilot-authored task and land it in the right status.

        THE QUEST LINK IS ALWAYS ``quest_id``. A task's ``goal_id`` field holds a QUEST id -- the
        Quest API resolves it with ``storage.get_quest(goal_id)`` and 404s anything else -- so a
        per-goal id from ``list_quest_goals`` (a separate document with its own id) must never be
        passed here. Doing so failed every work-batch creation outright, and any that had survived
        would have been invisible to ``_has_backpressure``, which looks tasks up by quest id. This
        field never held a goal id and never will: a task is linked to its QUEST, and goals reach
        the run as context in its text (see ``compose_batch_text``).

        ``status`` is asserted AT CREATION rather than PATCHed afterwards. Creating a proposal
        ``queued`` and demoting it in a second call leaves a window in which the runner's poll can
        claim and EXECUTE it before the demotion lands -- exactly the approval that suggest mode
        exists to require. One atomic create closes that window.

        The created task carries ``task_kind="autopilot_work"`` (NOT the pass's own
        ``"autopilot"`` kind, which the executor routes into another pass -- that would spawn an
        infinite loop) and names its persona structurally in ``assignee_rep_id``.

        It also carries ``parent_task_id`` -- the id of the PASS that created it. That link is what
        makes "what did autopilot do today" answerable with the work itself: a consumer can roll
        the finished task's own output back onto the pass row, so the person reads the work instead
        of the scanner's bookkeeping. A client whose ``create_task`` predates the argument simply
        creates the task without it, exactly as before.

        When the quest's ``autopilot`` block carries a ``model``, it names which model the deep
        worker runs THIS quest's autopilot work on -- but that config field is itself overloaded
        (a human can set it in the app to either a QAR-call tier like "fast"/"balanced" or a bare
        Claude family alias like "opus"), while the backend's create-task API now validates those
        as two SEPARATE fields (``model`` = tier only, ``deep_run_model`` = literal pin only, split
        2026-09-22). So the configured value is classified here with ``_is_claude_model`` (Claude-
        shaped -> ``deep_run_model``, anything else -> ``model``) and routed to whichever create
        argument the backend actually validates it against. Unset means the lane's own default
        model ladder applies, exactly as before this field existed. This is the single choke point
        every autopilot-created task (work batches and goal proposals alike) passes through, so
        both inherit the setting.
        """
        # suggest mode (and every goal proposal, which is always a proposal for a human) must not
        # be runnable until a human approves it.
        needs_approval = force_suggested or mode != "act"
        kwargs: Dict[str, Any] = dict(
            # The QUEST'S own team, not the lane's home team: a lane can discover work
            # from several teams, and a batch filed on the wrong team is work the quest's own
            # people never see. Identical to the home team on a single-team lane.
            team_id=self._team_for(quest_id) or None,
            goal_id=quest_id,
            source=AUTOPILOT_TASK_SOURCE,
            task_kind=AUTOPILOT_WORK_KIND,
            status="suggested" if needs_approval else "queued",
        )
        if self._lane_user_id:
            # The lane that creates the work is the lane that runs it -- see ``_lane_user_id``.
            kwargs["assignee_user_id"] = self._lane_user_id
        if persona:
            # Structural persona routing. It also rides in the text (some consumers resolve from
            # prose), but a field a resolver can read beats one it has to parse.
            kwargs["assignee_rep_id"] = persona
        if title:
            kwargs["title"] = title
        autopilot_cfg = quest.get("autopilot") or {}
        env_id = autopilot_cfg.get("env_id")
        if env_id:
            kwargs["env_id"] = env_id
        model = autopilot_cfg.get("model")
        if model:
            from ..core.goal_runner import _is_claude_model
            if _is_claude_model(model):
                # A literal deep-run model pin (e.g. "opus"), not a QAR-call tier -- the field the
                # backend validates it against post-split. Sending it as ``model`` 400s.
                kwargs["deep_run_model"] = model
            else:
                kwargs["model"] = model
        if self._pass_task_id:
            kwargs["parent_task_id"] = self._pass_task_id
        try:
            created = self._client.create_task(text, **kwargs) or {}
        except TypeError:
            # An older/stand-in client without ``parent_task_id``/``model``/``deep_run_model``.
            # All three are improvements to how the pass reports/routes, never a requirement for
            # it to work, so lose the refinement rather than the task.
            kwargs.pop("parent_task_id", None)
            kwargs.pop("model", None)
            kwargs.pop("deep_run_model", None)
            created = self._client.create_task(text, **kwargs) or {}
        task_id = created.get("id") or created.get("task_id")
        if not task_id:
            return None
        return str(task_id)

    def _create_batch_task(self, quest: Dict[str, Any], quest_id: str, persona: Optional[str],
                           mode: str, *,
                           title: Optional[str] = None,
                           scope_label: Optional[str] = None,
                           adopted_tasks: Optional[List[Dict[str, Any]]] = None,
                           next_steps: Optional[str] = None,
                           previous: Optional[Dict[str, Any]] = None,
                           last_run: Optional[str] = None,
                           reflection: Optional[str] = None,
                           insights: Optional[str] = None,
                           context_updates: Optional[str] = None,
                           instructions: Optional[str] = None,
                           persona_instructions: Optional[str] = None,
                           default_quest_instructions: Optional[str] = None,
                           default_persona_instructions: Optional[str] = None,
                           goal_ladder: Optional[List[Dict[str, Any]]] = None,
                           experiments: Optional[str] = None,
                           asked: Optional[str] = None) -> Optional[str]:
        text = compose_batch_text(str(quest.get("outcome") or ""),
                                  self._persona_label(persona),
                                  scope_label=scope_label, adopted_tasks=adopted_tasks,
                                  next_steps=next_steps, previous=previous,
                                  last_run=last_run,
                                  reflection=reflection, insights=insights,
                                  context_updates=context_updates,
                                  instructions=instructions,
                                  persona_instructions=persona_instructions,
                                  default_quest_instructions=default_quest_instructions,
                                  default_persona_instructions=default_persona_instructions,
                                  goal_ladder=goal_ladder, experiments=experiments,
                                  asked=asked)
        try:
            return self._create_autopilot_task(
                quest, quest_id, text, mode, persona=persona,
                title=title if title is not None
                else _batch_title(adopted_tasks, instructions=instructions,
                                  persona_instructions=persona_instructions))
        except Exception as e:  # noqa: BLE001 -- surfaced to the caller's per-quest try/except
            log.error("autopilot: task creation failed for quest %s: %s", quest_id, e,
                      exc_info=True)
            raise

    def _maybe_create_goal(self, quest_id: str, title: str, description: str, mode: str,
                          scope_label: str) -> Optional[str]:
        """Create the REAL, typed Goal behind a proposal, when the client can and the quest allows.

        Two conditions, both unchanged: the client must expose ``create_goal`` (a client without
        one still gets the proposal TASK, just no goal object), and the quest must be in ``act``
        mode, since creating a goal on a ``suggest`` quest would be autopilot writing the plan the
        person asked to approve first.

        The call is ``create_goal(title, quest_id=..., period=..., description=...)``.
        ``period`` is REQUIRED by both the client and ``POST /api/planning/goals`` (the backend
        derives the goal's deadline and time_scope from it), so it is derived from this pass's own
        scope label by ``goal_period_for_scope`` rather than guessed. Nothing marks the goal as
        AI-workable, because a goal is not a unit of AI work: it is the plan the work serves, and
        the flag that used to say otherwise no longer exists on a goal at all.

        HISTORY, because the failure mode matters more than the fix. This method called
        ``create_goal(quest_id, name=title, description=..., ai_help=True, created_by="ai")``:
        the quest id landed in ``title``, ``name`` and ``created_by`` are not parameters at all,
        and the required ``period`` was missing. It could not bind, so every call raised TypeError
        the moment ``QuestClient.create_goal`` existed, and AI-proposed goals were silently never
        created. Nothing noticed for two reasons at once, and the pair is the lesson: the branch is
        UNREACHABLE for any quest carrying standing instructions (the always-work rule matched
        first, and now matches for every quest), so it effectively never ran, and the bare
        ``except`` below downgraded a
        programming error to one INFO-shaped warning line indistinguishable from a backend that
        simply lacks the endpoint. Absence of "create_goal failed" in the logs was therefore not
        evidence that it worked.

        Hence the split below: a TypeError is a bad call signature in THIS file, logged at ERROR
        with a traceback so the next one is visible on the day it lands, while any other exception
        stays a warning, because a missing or misbehaving optional endpoint genuinely must not fail
        the pass. Neither propagates: the proposal task is still created either way, and it carries
        the whole proposal in its text.

        ``created_by="ai"`` was DROPPED rather than fixed. The endpoint's request model
        (``CreateGoalRequest``) has no attribution field, its base config ignores unknown keys, and
        the handler hardcodes ``source="user"`` on every goal it creates. Passing an attribution
        field would have looked like attribution while carrying none.
        """
        create_goal = getattr(self._client, "create_goal", None)
        if not callable(create_goal) or mode != "act":
            return None
        period = goal_period_for_scope(scope_label, self._now())
        assignee, ai_can_do = self._pick_assignee(quest_id, title, description)
        try:
            kwargs: Dict[str, Any] = {"quest_id": quest_id, "period": period,
                                      "description": description}
            if assignee:
                kwargs["assigned_to_user_id"] = assignee
            try:
                created = create_goal(title, **kwargs) or {}
            except (TypeError, ValueError):
                raise
            except Exception:  # noqa: BLE001 -- a refused assignee must not cost the goal
                if not assignee:
                    raise
                log.info("autopilot: assigned create_goal refused for quest %s; retrying shared",
                         quest_id, exc_info=True)
                kwargs.pop("assigned_to_user_id")
                assignee = None
                created = create_goal(title, **kwargs) or {}
            goal_id = created.get("id") if isinstance(created, dict) else None
            if goal_id and assignee and ai_can_do:
                self._hand_to_rep(quest_id, goal_id, assignee)
            return goal_id
        except TypeError:
            # A call this file got wrong, not an endpoint the deployment lacks. Loud, with a
            # traceback, and still not fatal to the pass.
            log.error("autopilot: create_goal call is not compatible with this client's signature "
                      "(quest %s, period %s); no goal was created", quest_id, period,
                      exc_info=True)
            return None
        except Exception as e:  # noqa: BLE001 -- a missing/misbehaving optional endpoint must not fail the pass
            log.warning("autopilot: create_goal failed for quest %s: %s", quest_id, e)
            return None

    def _pick_assignee(self, quest_id: str, title: str, description: str) -> Tuple[Optional[str], bool]:
        """(assignee user id or None, ai_can_do) for a goal this pass is about to create."""
        lister = getattr(self._client, "list_assignable_members", None)
        if not callable(lister):
            return None, False
        try:
            members = lister(quest_id) or []
            quest = self._quest_context(quest_id)
            verdict = choose_assignee(
                self._judge, members, outcome=str(quest.get("outcome") or ""), title=title,
                description=description, current_state=str(quest.get("current_state") or ""),
                preferences=preferences_text(quest.get("preferences")))
            return verdict["assignee"], bool(verdict["ai_can_do"])
        except Exception:  # noqa: BLE001 -- assignment is an improvement, never a failure
            log.info("autopilot: assignee choice failed for quest %s", quest_id, exc_info=True)
            return None, False

    def _quest_context(self, quest_id: str) -> Dict[str, Any]:
        """The quest's outcome, current state and preferences for a judgment call; {} on failure."""
        getter = getattr(self._client, "get_quest", None)
        if not callable(getter):
            return {}
        try:
            quest = getter(quest_id)
            return quest if isinstance(quest, dict) else {}
        except Exception:  # noqa: BLE001 -- context is advice, never a failure
            log.info("autopilot: could not read quest %s for an assignee choice", quest_id, exc_info=True)
            return {}

    def _hand_to_rep(self, quest_id: str, goal_id: str, assignee: str) -> None:
        """Ask the backend to give an AI-doable goal to its assignee's rep. Best-effort."""
        try:
            from .goal_handoff import rep_for_user
            reps = self._client.list_team_reps(team_id=self._team_for(quest_id) or None) or []
            rep_id = rep_for_user(reps, assignee)
            if rep_id:
                self._client.set_goal_ai_handling(goal_id, "rep", rep_id=rep_id, decided_by="rep")
        except Exception:  # noqa: BLE001
            log.info("autopilot: rep handoff failed for goal %s", goal_id, exc_info=True)

    def _open_proposal(self, quest_id: str) -> Optional[Dict[str, Any]]:
        """A proposed-goal task from an earlier pass that is still waiting on the person, or None.

        A proposal is not work: it is one question ("shall this be a goal?"), and asking it again
        because it has not been answered yet is how a person ends up with the same suggestion in
        their list every single day, and the same "Waiting for your approval" line in every pass
        report. Reported 2026-08-22, on a quest whose proposals had been repeating: "not useful at
        all".

        This is NOT the backpressure gate (which is opt-in, off by default, and deliberately so:
        an unanswered question must not stop a quest from doing work that is independent of it).
        It is narrower and always on, because a duplicate of a pending question is never the work
        that was blocked; it is only more of the question.
        """
        try:
            tasks = self._client.list_tasks(
                team_id=self._team_for(quest_id) or None, goal_id=quest_id) or []
        except Exception:  # noqa: BLE001 -- fail OPEN: a bad read must not silence a proposal
            log.info("autopilot: could not check for an open proposal on quest %s", quest_id,
                     exc_info=True)
            return None
        for t in tasks:
            if not self._is_autopilot_authored(t):
                continue
            if str(t.get("status", "")).strip().lower() not in OPEN_TASK_STATUSES:
                continue
            if str(t.get("text") or "").lstrip().startswith(PROPOSAL_TEXT_PREFIX):
                return t
        return None

    def _handle_proposal(self, quest: Dict[str, Any], quest_id: str, quest_label: str, mode: str,
                         dry_run: bool, result: AutopilotResult,
                         scope_label: str = "") -> Optional[str]:
        """Propose the quest's next goal, unless the last pass's proposal is still unanswered.

        Returns the skip reason when nothing was proposed, so the caller can report it as a skip
        and leave the budget alone.

        ``scope_label`` is this pass's own scope (``current_scope_label``), carried through only so
        a goal created from the proposal is filed under the period the quest is actually working
        in. See ``_maybe_create_goal``: the period is required and cannot be defaulted centrally.
        """
        title, description = propose_next_goal(quest)
        pending = None if dry_run else self._open_proposal(quest_id)
        if pending is not None:
            asked = _task_label(pending)
            return (f"a proposed goal from an earlier pass is still waiting for your yes or no "
                    f"({asked})")
        if dry_run:
            result.proposals.append({
                "quest_id": quest_id, "quest_label": quest_label, "kind": "goal_proposal",
                "title": title, "description": description,
            })
            return None
        created_goal_id = self._maybe_create_goal(quest_id, title, description, mode, scope_label)
        task_text = f"{PROPOSAL_TEXT_PREFIX} {title}\n\n{description}"
        if created_goal_id:
            task_text += f"\n\n(Created as goal {created_goal_id} on this quest.)"
        # A proposed goal is ALWAYS surfaced for a human to accept, even on an `act` quest: the
        # design keeps AI-created goals reviewable ("attributed and editable"), so force suggested.
        task_id = self._create_autopilot_task(
            quest, quest_id, task_text, mode, title=title, force_suggested=True)
        if task_id:
            # ALWAYS awaiting approval: a proposed goal is surfaced for a human to accept even on
            # an ``act`` quest (see _create_autopilot_task's force_suggested).
            #
            # The title here is the proposal's own title, with no "Proposed goal:" prefix glued
            # on: the report prints proposals under their own heading, and prefixing them there
            # too produced "Proposed goal: Next step toward: <outcome> (on <outcome>)" -- the same
            # words three times in one line.
            result.created.append({
                "task_id": task_id, "kind": "goal_proposal", "quest_id": quest_id,
                "quest_label": quest_label, "title": title,
                "awaiting_approval": True,
            })
        return None

    def _update_pass_bookkeeping(self, quest_id: str, quest_label: str,
                                 autopilot_cfg: Dict[str, Any],
                                 produced: bool, result: AutopilotResult) -> None:
        """Stamp ``last_pass_at`` (and the ``miss_streak``) on the quest, then VERIFY it stuck.

        The verify is not paranoia. The quest autopilot PATCH endpoint's request schema currently
        accepts only mode/planning/cadence/personas/env_id; ``last_pass_at`` and ``miss_streak``
        exist on the stored model but are NOT in that schema, and its Pydantic model ignores
        unknown keys -- so this write can return 200 having persisted nothing. If we assumed
        success, ``last_pass_at`` would stay null forever, the cadence gate would consider every
        quest due on every pass, and the per-quest cadence (a core budget control) would be
        silently inert. So: read the echoed settings back, compare, and record a LOUD warning when
        a field did not stick. Never raises -- bookkeeping must not fail an otherwise-good pass --
        but it must never fail SILENTLY either.
        """
        now_iso = self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields: Dict[str, Any] = {"last_pass_at": now_iso}
        fields["miss_streak"] = 0 if produced else int(autopilot_cfg.get("miss_streak") or 0) + 1
        update = getattr(self._client, "update_quest_autopilot", None)
        if not callable(update):
            return
        try:
            resp = update(quest_id, fields) or {}
        except Exception as e:  # noqa: BLE001 -- bookkeeping must never fail an otherwise-good pass
            log.warning("autopilot: last_pass_at/miss_streak update failed for quest %s",
                       quest_id, exc_info=True)
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "quest_label": quest_label or quest_id,
                 "detail": f"update raised {type(e).__name__}: {e}"})
            return
        echoed = (resp or {}).get("autopilot")
        if not isinstance(echoed, dict):
            return  # nothing echoed back to check against (an older/mock client); assume nothing
        unpersisted = [k for k, v in fields.items() if echoed.get(k) != v]
        if unpersisted:
            detail = (f"the backend accepted the PATCH but did not persist {unpersisted} "
                      f"(its autopilot update schema does not accept these bookkeeping fields)")
            log.warning("autopilot: quest %s -- %s", quest_id, detail)
            result.bookkeeping_warnings.append(
                {"quest_id": quest_id, "quest_label": quest_label or quest_id, "detail": detail})
