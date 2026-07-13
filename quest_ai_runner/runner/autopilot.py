"""Autopilot — the recurring "autopilot pass" task that scans opted-in quests and makes progress.

Design of record: ``quest_autopilot_design.md`` (Part B). In one sentence: Autopilot is itself a
recurring assistant task (``handler == "autopilot"``, routed by ``runner.executor`` before the
normal deep-run path). Each pass:

  1. Lists the team's quests; keeps the ones opted in (``autopilot.mode`` in ``suggest``/``act``).
  2. Gates each, cheapest first: a team-wide daily budget, per-quest cadence, backpressure (an
     open autopilot-created task already sitting on the quest), and an open HOLD decision on the
     quest.
  3. Picks the quest's CURRENT-SCOPE target goals (today's, this period's, or the single next
     incomplete one when the quest is unscoped) among the ones flagged ``ai_help``.
  4. Resolves a persona per goal (goal assignee -> quest persona-for-today -> a consumer-injected
     fallback) and batches goals sharing a persona into ONE task (one budget unit).
  5. Creates each batch as a real task (``status="suggested"`` in suggest mode, ``"queued"`` in
     act mode), or -- when planning allows and nothing is eligible -- proposes the quest's next
     goal instead of a work task.
  6. Updates the quest's ``autopilot.last_pass_at`` (and ``miss_streak`` when nothing was
     produced) via the quest update route.

A DRY RUN (the pass task's own text contains "dry-run") reports what WOULD be created and creates
nothing: no tasks, no goals, no bookkeeping writes.

Per-quest failures are isolated: one quest's error never aborts the rest of the pass (recorded in
the result instead), and every skipped quest is logged with its gate reason -- silent skips are
exactly the failure mode the qar playbook bans.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("quest-ai-runner.autopilot")

# Team-wide daily cap on autopilot-created tasks (batches + goal proposals both count as one
# unit each). Overridable via ``RunnerConfig.autopilot_daily_budget``.
DEFAULT_TEAM_DAILY_BUDGET = 3

# Cadence name -> minimum days between passes for a given quest. An unrecognized cadence string
# falls back to "weekly" rather than erroring, so a bad/unknown value never wedges a quest either
# always-due or never-due.
_CADENCE_DAYS = {"daily": 1, "weekly": 7, "monthly": 30}

# A previous autopilot task in any of these states is still "open" -- its quest is under
# backpressure and gets no more autonomous work piled on until it resolves.
_OPEN_TASK_STATUSES = {"queued", "in_progress", "needs_you", "suggested"}

# Time-scope granularities recognized in a quest's ``list_quest_goals`` period grouping, checked
# FINEST first (day) so "today" wins over a same-quarter month/quarter/year group that also
# happens to be current.
_SCOPE_ORDER = ("day", "week", "month", "quarter", "year")


# --- small, pure helpers (kept module-level and dependency-free for direct unit testing) --------

def _parse_dt(raw: Any) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string (tolerating a trailing ``Z``). None on any failure."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def cadence_due(autopilot_cfg: Dict[str, Any], now: datetime) -> bool:
    """Whether a quest's cadence gate is DUE (True) or should skip this pass (False).

    Never run before (no ``last_pass_at``) is always due. An unparsable timestamp fails OPEN to
    due -- a corrupt/missing stamp must never permanently wedge a quest as "not due yet".
    """
    last_pass = autopilot_cfg.get("last_pass_at")
    if not last_pass:
        return True
    parsed = _parse_dt(last_pass)
    if parsed is None:
        return True
    cadence = str(autopilot_cfg.get("cadence") or "weekly").strip().lower()
    days = _CADENCE_DAYS.get(cadence, _CADENCE_DAYS["weekly"])
    return (now - parsed) >= timedelta(days=days)


def _current_period_key(scope: str, now: datetime) -> Optional[str]:
    """The canonical period string for ``scope`` at ``now`` (e.g. day -> "2026-07-12")."""
    if scope == "day":
        return now.date().isoformat()
    if scope == "week":
        iso_year, iso_week, _ = now.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"
    if scope == "month":
        return now.strftime("%Y-%m")
    if scope == "quarter":
        q = (now.month - 1) // 3 + 1
        return f"{now.year}-Q{q}"
    if scope == "year":
        return str(now.year)
    return None


def _goal_ai_help(goal: Dict[str, Any]) -> bool:
    """Missing ``ai_help`` counts as False (human-only, invisible to autopilot) per the design."""
    return bool(goal.get("ai_help"))


def _incomplete_ai_goals(goals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [g for g in goals if not g.get("completed") and _goal_ai_help(g)]


def select_target_goals(goals_payload: Dict[str, Any],
                        now: datetime) -> Tuple[List[Dict[str, Any]], str]:
    """Pick this pass's target goals from a quest's ``list_quest_goals`` period grouping.

    Returns ``(goals, scope_label)``:
      * a CURRENT period group exists (day beats week beats month beats quarter beats year) ->
        ALL its incomplete + ``ai_help`` goals, ``scope_label`` like ``"day:2026-07-12"``. This is
        deliberately ALL of them (not just one): the resolved 2026-07-12 scope question in the
        design doc is "a pass works ALL incomplete AI-enabled goals in the quest's current scope".
      * no scope is current (an unscoped quest, or only "custom"-scoped goals) -> the SINGLE next
        incomplete + ``ai_help`` goal in the payload's own order, ``scope_label="unscoped"``.
      * nothing eligible either way -> ``([], "unscoped")``.
    """
    groups = goals_payload.get("period_groups") or []
    for scope in _SCOPE_ORDER:
        key = _current_period_key(scope, now)
        for group in groups:
            if str(group.get("time_scope", "")).strip().lower() != scope:
                continue
            period = str(group.get("period", "")).strip()
            if period == key:
                return _incomplete_ai_goals(group.get("goals") or []), f"{scope}:{key}"
    flattened: List[Dict[str, Any]] = []
    for group in groups:
        flattened.extend(group.get("goals") or [])
    for goal in flattened:
        if not goal.get("completed") and _goal_ai_help(goal):
            return [goal], "unscoped"
    return [], "unscoped"


def _weekday_abbrev(now: datetime) -> str:
    return now.strftime("%a")  # "Mon", "Tue", ...


def resolve_persona(goal: Dict[str, Any], autopilot_cfg: Dict[str, Any], now: datetime,
                    fallback_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
                    ) -> Optional[str]:
    """Resolver order (explicit always beats inferred), per the design's persona-routing section:

      1. The goal's own ``assignee_rep_id`` (a per-goal override).
      2. A quest ``autopilot.personas`` entry whose ``days`` include today (checked first, so an
         explicit day-restricted assignment wins over an unrestricted one for the same day).
      3. A quest ``autopilot.personas`` entry with NO ``days`` restriction (applies any day).
      4. A consumer-injected fallback (e.g. the existing card-vote resolver), given the goal dict.
      5. ``None`` -- the plain assistant persona (no character voice).
    """
    assignee = goal.get("assignee_rep_id")
    if assignee:
        return str(assignee)
    personas = autopilot_cfg.get("personas") or []
    today = _weekday_abbrev(now)
    for persona in personas:
        days = persona.get("days")
        if days and today in days:
            rep_id = persona.get("rep_id")
            if rep_id:
                return str(rep_id)
    for persona in personas:
        if not persona.get("days"):
            rep_id = persona.get("rep_id")
            if rep_id:
                return str(rep_id)
    if fallback_resolver is not None:
        try:
            resolved = fallback_resolver(goal)
            if resolved:
                return str(resolved)
        except Exception:  # noqa: BLE001 -- a bad fallback must never break a pass
            log.info("autopilot: persona fallback_resolver raised; treating as no match",
                     exc_info=True)
    return None


def batch_by_persona(goals: List[Dict[str, Any]], autopilot_cfg: Dict[str, Any], now: datetime,
                     fallback_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None
                     ) -> List[Tuple[Optional[str], List[Dict[str, Any]]]]:
    """Group ``goals`` by resolved persona, preserving first-seen order. Same persona (including
    ``None``, the plain assistant) -> ONE batch; different personas -> separate batches."""
    order: List[Optional[str]] = []
    batches: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for goal in goals:
        persona = resolve_persona(goal, autopilot_cfg, now, fallback_resolver)
        if persona not in batches:
            batches[persona] = []
            order.append(persona)
        batches[persona].append(goal)
    return [(persona, batches[persona]) for persona in order]


def _definition_of_done(goal: Dict[str, Any]) -> str:
    name = (goal.get("name") or "this goal").strip()
    deadline = (goal.get("deadline") or "").strip()
    dod = (f'"{name}" is complete: the work matches the goal\'s description above and is ready '
          "for a human to read as-is.")
    if deadline:
        dod += f" Target: {deadline}."
    return dod


def compose_batch_text(quest_outcome: str, goals: List[Dict[str, Any]]) -> str:
    """Quest outcome + each goal's name/description (the description IS the brief) + a per-goal
    Definition of Done, per the design's task-composition rule."""
    parts: List[str] = []
    if quest_outcome:
        parts.append(f"Quest outcome: {quest_outcome}")
    for goal in goals:
        name = (goal.get("name") or "(untitled goal)").strip()
        description = (goal.get("description") or "").strip()
        block = [f"Goal: {name}"]
        if description:
            block.append(f"Brief: {description}")
        block.append(f"Definition of Done: {_definition_of_done(goal)}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def propose_next_goal(quest: Dict[str, Any]) -> Tuple[str, str]:
    """A deterministic (LLM-free, so this stays fast/offline-testable) proposed next-goal (title,
    description) in service of the quest's stated outcome, used when ``planning=="plan_and_work"``
    and no AI-enabled goal is eligible this pass."""
    outcome = (quest.get("outcome") or "this quest's outcome").strip()
    title = f"Next step toward: {outcome}"
    description = (
        f'Propose and take the next concrete step toward "{outcome}". No AI-enabled goal was '
        "eligible this pass, and this quest allows autopilot to plan as well as work. Define a "
        "specific, checkable outcome for this goal before starting on it."
    )
    return title, description


@dataclass
class AutopilotResult:
    """What one autopilot pass did -- the pass task's own reported result (see ``summary_text``)."""
    ran_at: datetime
    dry_run: bool = False
    created_task_ids: List[str] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)      # {quest_id, reason}
    proposals: List[Dict[str, Any]] = field(default_factory=list)    # dry-run "would create" items
    errors: List[Dict[str, Any]] = field(default_factory=list)       # {quest_id, error}

    def summary_text(self) -> str:
        lines: List[str] = []
        lines.append("Autopilot dry-run: nothing was created. Here is what WOULD happen:"
                     if self.dry_run else "Autopilot pass complete.")
        if self.created_task_ids:
            lines.append(f"Created {len(self.created_task_ids)} task(s): "
                        + ", ".join(self.created_task_ids))
        if self.proposals:
            lines.append(f"Would create {len(self.proposals)} item(s):")
            for p in self.proposals:
                if p.get("kind") == "goal_proposal":
                    lines.append(f"  - Proposed goal on quest {p.get('quest_id')}: {p.get('title')}")
                else:
                    lines.append(
                        f"  - Work batch on quest {p.get('quest_id')} "
                        f"(persona={p.get('persona') or 'assistant'}, scope={p.get('scope')}): "
                        f"goal(s) {p.get('goal_ids')}"
                    )
        if self.skipped:
            lines.append(f"Skipped {len(self.skipped)} quest(s):")
            for s in self.skipped:
                lines.append(f"  - {s.get('quest_id')}: {s.get('reason')}")
        if self.errors:
            lines.append(f"Errors on {len(self.errors)} quest(s):")
            for e in self.errors:
                lines.append(f"  - {e.get('quest_id')}: {e.get('error')}")
        if not (self.created_task_ids or self.proposals or self.skipped or self.errors):
            lines.append("No opted-in quests found.")
        return "\n".join(lines)


class AutopilotPass:
    """Runs ONE autopilot pass against a Quest client. See the module docstring for the algorithm.

    ``client`` is a ``QuestClient`` (or any object with the same methods this class calls:
    ``list_quests``, ``list_quest_goals``, ``list_tasks``, ``list_open_decisions_for_quest``,
    ``create_task``, ``update_quest_autopilot``, ``create_goal`` [optional]).

    ``persona_resolver`` is the consumer-injected fallback (step 4 of ``resolve_persona``) -- e.g.
    the personal lane's card-vote resolver. Given a goal dict, returns a rep_id or ``None``.
    """

    def __init__(self, client: Any, *, team_id: str = "",
                 persona_resolver: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None,
                 daily_budget: int = DEFAULT_TEAM_DAILY_BUDGET,
                 now: Optional[Callable[[], datetime]] = None):
        self._client = client
        self._team_id = team_id or ""
        self._persona_resolver = persona_resolver
        self._daily_budget = daily_budget if daily_budget and daily_budget > 0 else DEFAULT_TEAM_DAILY_BUDGET
        self._now = now or (lambda: datetime.now(timezone.utc))

    # --- the pass --------------------------------------------------------------------------

    def run(self, task: Dict[str, Any]) -> AutopilotResult:
        text = str(task.get("text") or task.get("title") or "")
        dry_run = "dry-run" in text.lower()
        result = AutopilotResult(ran_at=self._now(), dry_run=dry_run)

        quests = self._eligible_quests()
        budget_used = 0 if dry_run else self._count_autopilot_tasks_today()

        for quest in quests:
            quest_id = str(quest.get("quest_id") or quest.get("id") or "")
            if not quest_id:
                continue
            try:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id,
                              f"team daily budget reached ({budget_used}/{self._daily_budget})")
                    continue
                gate_reason = self._gate_quest(quest, quest_id)
                if gate_reason:
                    self._skip(result, quest_id, gate_reason)
                    continue
                budget_used = self._run_one_quest(quest, quest_id, dry_run, budget_used, result)
            except Exception as e:  # noqa: BLE001 -- one quest's failure never aborts the pass
                log.error("autopilot: quest %s pass failed: %s", quest_id, e, exc_info=True)
                result.errors.append({"quest_id": quest_id, "error": f"{type(e).__name__}: {e}"})
        return result

    def _run_one_quest(self, quest: Dict[str, Any], quest_id: str, dry_run: bool,
                       budget_used: int, result: AutopilotResult) -> int:
        autopilot_cfg = quest.get("autopilot") or {}
        mode = str(autopilot_cfg.get("mode") or "off")
        planning = str(autopilot_cfg.get("planning") or "work_only")

        goals_payload = self._client.list_quest_goals(quest_id, team_id=self._team_id or None) or {}
        target_goals, scope_label = select_target_goals(goals_payload, self._now())

        produced = False
        if target_goals:
            batches = batch_by_persona(target_goals, autopilot_cfg, self._now(),
                                       self._persona_resolver)
            for persona, goals in batches:
                if budget_used >= self._daily_budget:
                    self._skip(result, quest_id, "team daily budget reached mid-pass")
                    break
                if dry_run:
                    result.proposals.append({
                        "quest_id": quest_id, "kind": "work_batch", "persona": persona,
                        "goal_ids": [g.get("id") for g in goals], "scope": scope_label,
                    })
                    produced = True
                    # A dry-run still SIMULATES budget consumption (one unit per batch that
                    # WOULD be created), so the report honestly shows a later quest going quiet
                    # once the budget is exhausted -- exactly what a real pass would do.
                    budget_used += 1
                    continue
                task_id = self._create_batch_task(quest, quest_id, persona, goals, mode)
                if task_id:
                    result.created_task_ids.append(task_id)
                    budget_used += 1
                    produced = True
        elif planning == "plan_and_work":
            if budget_used < self._daily_budget:
                self._handle_proposal(quest, quest_id, mode, dry_run, result)
                produced = True
                budget_used += 1
            else:
                self._skip(result, quest_id, "team daily budget reached mid-pass")

        if not dry_run:
            self._update_pass_bookkeeping(quest_id, autopilot_cfg, produced)
        return budget_used

    # --- gates -------------------------------------------------------------------------------

    def _eligible_quests(self) -> List[Dict[str, Any]]:
        quests = self._client.list_quests(team_id=self._team_id or None) or []
        eligible = []
        for quest in quests:
            mode = str((quest.get("autopilot") or {}).get("mode") or "off")
            quest_id = quest.get("quest_id") or quest.get("id")
            if mode in ("suggest", "act"):
                eligible.append(quest)
            else:
                log.info("autopilot: quest %s mode=%r -- not opted in, skipping", quest_id, mode)
        return eligible

    def _gate_quest(self, quest: Dict[str, Any], quest_id: str) -> Optional[str]:
        """Per-quest gates, cheapest first. Returns a skip reason, or None if the quest passes."""
        autopilot_cfg = quest.get("autopilot") or {}
        if not cadence_due(autopilot_cfg, self._now()):
            return "cadence not due yet"
        if self._has_backpressure(quest_id):
            return "backpressure: a previous autopilot task for this quest is still open"
        if self._has_open_hold_decision(quest_id):
            return "an open HOLD decision is pending on this quest"
        return None

    def _count_autopilot_tasks_today(self) -> int:
        tasks = self._client.list_tasks(team_id=self._team_id or None, source="autopilot") or []
        today = self._now().date()
        count = 0
        for t in tasks:
            created = _parse_dt(t.get("created_at") or t.get("scheduled_at"))
            if created is not None and created.date() == today:
                count += 1
        return count

    def _has_backpressure(self, quest_id: str) -> bool:
        tasks = self._client.list_tasks(
            team_id=self._team_id or None, quest_id=quest_id, source="autopilot") or []
        return any(str(t.get("status", "")).strip().lower() in _OPEN_TASK_STATUSES for t in tasks)

    def _has_open_hold_decision(self, quest_id: str) -> bool:
        lister = getattr(self._client, "list_open_decisions_for_quest", None)
        if not callable(lister):
            return False
        try:
            return bool(lister(quest_id))
        except Exception:  # noqa: BLE001 -- fail open: never let this check block a pass
            log.info("autopilot: list_open_decisions_for_quest failed for %s", quest_id,
                     exc_info=True)
            return False

    # --- side effects --------------------------------------------------------------------------

    def _create_batch_task(self, quest: Dict[str, Any], quest_id: str, persona: Optional[str],
                           goals: List[Dict[str, Any]], mode: str) -> Optional[str]:
        text = compose_batch_text(str(quest.get("outcome") or ""), goals)
        status = "queued" if mode == "act" else "suggested"
        kwargs: Dict[str, Any] = dict(
            team_id=self._team_id or None,
            goal_id=goals[0].get("id"),
            quest_id=quest_id,
            source="autopilot",
            status=status,
        )
        env_id = (quest.get("autopilot") or {}).get("env_id")
        if env_id:
            kwargs["env_id"] = env_id
        if persona:
            kwargs["rep_id"] = persona
        try:
            created = self._client.create_task(text, **kwargs) or {}
            return created.get("id") or created.get("task_id")
        except Exception as e:  # noqa: BLE001 -- surfaced to the caller's per-quest try/except
            log.error("autopilot: create_task failed for quest %s: %s", quest_id, e, exc_info=True)
            raise

    def _maybe_create_goal(self, quest_id: str, title: str, description: str,
                          mode: str) -> Optional[str]:
        """Create the REAL goal object only when the client has a create_goal endpoint AND mode is
        "act" (per the design: creating goals stays manual in v1 unless the client already
        supports it). Generic/adapter-agnostic: works today (no-op, since QuestClient has no
        create_goal yet) and picks up automatically once one is added."""
        create_goal = getattr(self._client, "create_goal", None)
        if not callable(create_goal) or mode != "act":
            return None
        try:
            created = create_goal(quest_id, name=title, description=description,
                                  ai_help=True, created_by="ai") or {}
            return created.get("id") if isinstance(created, dict) else None
        except Exception as e:  # noqa: BLE001 -- a missing/misbehaving optional endpoint must not fail the pass
            log.warning("autopilot: create_goal failed for quest %s: %s", quest_id, e)
            return None

    def _handle_proposal(self, quest: Dict[str, Any], quest_id: str, mode: str, dry_run: bool,
                         result: AutopilotResult) -> None:
        title, description = propose_next_goal(quest)
        if dry_run:
            result.proposals.append({
                "quest_id": quest_id, "kind": "goal_proposal",
                "title": title, "description": description,
            })
            return
        created_goal_id = self._maybe_create_goal(quest_id, title, description, mode)
        task_text = f"Proposed goal: {title}\n\n{description}"
        kwargs: Dict[str, Any] = dict(
            team_id=self._team_id or None, quest_id=quest_id,
            source="autopilot", status="suggested",
        )
        if created_goal_id:
            kwargs["goal_id"] = created_goal_id
        env_id = (quest.get("autopilot") or {}).get("env_id")
        if env_id:
            kwargs["env_id"] = env_id
        created = self._client.create_task(task_text, **kwargs) or {}
        task_id = created.get("id") or created.get("task_id")
        if task_id:
            result.created_task_ids.append(task_id)

    def _update_pass_bookkeeping(self, quest_id: str, autopilot_cfg: Dict[str, Any],
                                 produced: bool) -> None:
        now_iso = self._now().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fields: Dict[str, Any] = {"last_pass_at": now_iso}
        if produced:
            fields["miss_streak"] = 0
        else:
            fields["miss_streak"] = int(autopilot_cfg.get("miss_streak") or 0) + 1
        update = getattr(self._client, "update_quest_autopilot", None)
        if callable(update):
            try:
                update(quest_id, fields, team_id=self._team_id or None)
            except Exception:  # noqa: BLE001 -- bookkeeping must never fail an otherwise-good pass
                log.warning("autopilot: last_pass_at/miss_streak update failed for quest %s",
                           quest_id, exc_info=True)

    def _skip(self, result: AutopilotResult, quest_id: str, reason: str) -> None:
        log.info("autopilot: skipping quest %s (%s)", quest_id, reason)
        result.skipped.append({"quest_id": quest_id, "reason": reason})
