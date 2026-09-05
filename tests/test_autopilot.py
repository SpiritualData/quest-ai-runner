"""Autopilot: gates, the scope label, who gets a batch, suggest/act status, dry-run.

Against a fake Quest client (no network), driving ``runner.autopilot.AutopilotPass`` and its
pure helper functions directly. See ``quest_autopilot_design.md`` (Part B) for the algorithm this
proves, and the qar-playbook invariant: every skipped quest must be logged with its gate reason
(``AutopilotResult.skipped``) — a silent skip is exactly the failure mode banned there.

Goals are CONTEXT throughout this file and never work: nothing selects a goal, assigns one, or
composes one as an instruction. What a pass produces is decided by who is on duty and what brief
they work to, and the goals only ever appear in the ladder.
"""
import time
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import (
    AUTOPILOT_WORK_KIND,
    BUNDLED_DEFAULT_INSTRUCTIONS,
    DEFAULT_TEAM_DAILY_BUDGET,
    PERSONA_HELD,
    AutopilotPass,
    cadence_due,
    current_scope_label,
    default_instructions_for,
    run_requested,
    compose_batch_text,
    resolve_persona,
    resolve_task_persona,
)
# The client's OWN period formats, so the fake below accepts exactly what the real one accepts
# instead of a shape invented here (the five quest-backend's period_utils parses).
from quest_ai_runner.runner.quest_client import _PERIOD_RE

NOW = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)  # a Sunday
MONDAY = datetime(2026, 7, 13, 9, 0, 0, tzinfo=timezone.utc)


def _now():
    return NOW


class FakeAutopilotClient:
    """An in-memory Quest client stand-in that mimics the REAL backend contract, verified against
    quest-backend's july branch. The fidelity here is the point -- several of these details are
    exactly where an assumed contract would have failed silently:

      * ``list_quests`` returns the team listing's SLIM rows (quest_id/outcome/completed/
        owner_user_ids) with NO ``autopilot`` block -- the pass must read it per quest via
        ``get_quest_autopilot`` (GET /api/quests/{id}/state), or it would see every quest as off.
      * ``list_quest_goals`` returns period groups whose goals carry NO ``description`` (the
        backend's handler builds a slim dict) -- the pass enriches via ``get_goal``.
      * ``list_tasks`` filters ONLY on the server-side params the real route implements
        (status/goal_id/team_id); ``source``/``task_kind`` are applied client-side, and there is
        no ``quest_id`` param at all (a task's ``goal_id`` IS its quest link).
      * ``create_task`` accepts an initial ``status`` (queued/suggested ONLY) and a structural
        ``assignee_rep_id``, and resolves ``goal_id`` as a QUEST id -- a per-goal id 404s.
      * ``update_quest_autopilot`` echoes back only the fields its schema ACCEPTS. The default
        here is the OLD schema (mode/planning/cadence/personas/env_id), which silently dropped
        ``last_pass_at``/``miss_streak``; ``accepts_bookkeeping=True`` models the fixed one, so the
        verify path is proven against both.
    """

    # Mirrors the real UpdateAutopilotRequest schema (app/api/endpoints/quests/autopilot.py).
    ACCEPTED_AUTOPILOT_FIELDS = {"mode", "planning", "cadence", "personas", "env_id"}

    def __init__(self, quests=None, goals_by_quest=None, tasks=None,
                 accepts_bookkeeping=False):
        self.quests = list(quests or [])            # full quest states, keyed for get_quest_autopilot
        self.goals_by_quest = dict(goals_by_quest or {})
        self.tasks = list(tasks or [])
        self.created_tasks = []
        # Every ``list_quest_goals`` call, in order. A gate that is meant to be cheap has to be
        # shown to run before the goal fetch, and this is the record that shows it.
        self.goal_list_calls = []
        self.task_updates = []        # (task_id, fields) -- the PATCHes
        self.autopilot_updates = []   # (quest_id, fields)
        self.open_decisions = {}      # quest_id -> bool
        self.goal_docs = {}           # goal_id -> full goal doc (with description)
        self.update_task_error = None  # set to an exception to make update_task raise
        # When True, the fake behaves like a FIXED backend whose autopilot PATCH also accepts the
        # scanner's bookkeeping fields, so the verify path is proven in both worlds.
        self.accepts_bookkeeping = accepts_bookkeeping
        self._autopilot_state = {
            str(q["quest_id"]): dict(q.get("autopilot") or {}) for q in self.quests
        }

    def list_quests(self, *, team_id=None):
        # The REAL team quest listing: no autopilot block.
        return [{"quest_id": q["quest_id"], "outcome": q.get("outcome"),
                 "completed": q.get("completed", False), "owner_user_ids": []}
                for q in self.quests]

    def get_quest_autopilot(self, quest_id):
        for q in self.quests:
            if q["quest_id"] == quest_id:
                return {"outcome": q.get("outcome"),
                        "autopilot": dict(self._autopilot_state.get(quest_id, {}))}
        return {}

    def list_quest_goals(self, quest_id, *, team_id=None):
        self.goal_list_calls.append(quest_id)
        return self.goals_by_quest.get(quest_id, {})

    def get_goal(self, goal_id, *, quest_id=None, team_id=None):
        return self.goal_docs.get(goal_id, {})

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, source=None, task_kind=None):
        out = []
        for t in self.tasks:
            if status is not None and t.get("status") != status:
                continue
            if goal_id is not None and t.get("goal_id") != goal_id:
                continue
            if source is not None and t.get("source") != source:
                continue
            if task_kind is not None and t.get("task_kind") != task_kind:
                continue
            out.append(t)
        return out

    def list_open_decisions_for_quest(self, quest_id):
        return [{"id": "dec_1", "status": "open"}] if self.open_decisions.get(quest_id) else []

    def create_task(self, text, **kwargs):
        # Mirrors the CURRENT real create route: it accepts an initial ``status`` (queued or
        # suggested only) and a structural ``assignee_rep_id``, but still has no ``quest_id``
        # field (the quest link is ``goal_id``) and no bare ``rep_id``.
        assert "quest_id" not in kwargs, "the real create route has no quest_id field"
        assert "rep_id" not in kwargs, "the persona field is named assignee_rep_id"
        assert kwargs.get("status", "queued") in ("queued", "suggested"), \
            "only queued/suggested may be asserted at creation"
        # The API resolves a task's goal_id as a QUEST id and 404s anything else, so the fake
        # holds the real route to that contract: a per-goal id here is a hard failure, not a
        # silently-accepted row that later goes missing from every per-quest lookup.
        goal_id = kwargs.get("goal_id")
        if goal_id is not None:
            assert goal_id in {str(q["quest_id"]) for q in self.quests}, (
                f"goal_id {goal_id!r} is not a quest id -- the real route would 404")
        task_id = f"autotask_{len(self.created_tasks) + 1}"
        record = {"id": task_id, "text": text, "status": "queued", **kwargs}
        self.created_tasks.append(record)
        return {"id": task_id}

    def update_task(self, task_id, fields):
        if self.update_task_error:
            raise self.update_task_error
        self.task_updates.append((task_id, dict(fields)))
        for t in self.created_tasks:
            if t["id"] == task_id:
                t.update(fields)
        return {"id": task_id, **fields}

    def update_quest_autopilot(self, quest_id, fields):
        self.autopilot_updates.append((quest_id, dict(fields)))
        current = self._autopilot_state.setdefault(quest_id, {})
        for k, v in fields.items():
            if self.accepts_bookkeeping or k in self.ACCEPTED_AUTOPILOT_FIELDS:
                current[k] = v
            # else: silently dropped, exactly like the real endpoint's Pydantic model.
        return {"quest_id": quest_id, "autopilot": dict(current)}


def _quest(quest_id, *, mode="act", cadence="weekly", last_pass_at=None, planning="work_only",
          env_id=None, personas=None, outcome="ship the thing", miss_streak=0,
          adopt_recurring=None, run_requested_at=None, default_instructions=None):
    autopilot = {"mode": mode, "cadence": cadence, "planning": planning,
                "miss_streak": miss_streak}
    if default_instructions is not None:
        # The read-only field a CURRENT backend derives and serves on the quest payload. The fake
        # omits it by default on purpose, so the bundled-fallback path is what most tests exercise.
        autopilot["default_instructions"] = default_instructions
    if last_pass_at is not None:
        autopilot["last_pass_at"] = last_pass_at
    if run_requested_at is not None:
        autopilot["run_requested_at"] = run_requested_at
    if env_id is not None:
        autopilot["env_id"] = env_id
    if personas is not None:
        autopilot["personas"] = personas
    if adopt_recurring is not None:
        autopilot["adopt_recurring"] = adopt_recurring
    return {"quest_id": quest_id, "outcome": outcome, "autopilot": autopilot}


def _goal(goal_id, name="A goal", *, completed=False, deadline=None):
    """A goal AS THE GROUPING ENDPOINT RETURNS IT.

    There is no ``ai_help`` and no ``assignee_rep_id``: quest-backend removed both from a goal
    outright, so no goal can flag itself as AI work or name the character who does it. There is no
    ``description`` either (the real handler builds a slim per-goal dict), and nothing fetches one,
    because a goal never becomes a brief.
    """
    g = {"id": goal_id, "name": name, "completed": completed}
    if deadline is not None:
        g["deadline"] = deadline
    return g


def _goals_payload(*groups):
    """``groups`` is a list of (time_scope, period, [goals]) tuples."""
    return {"period_groups": [
        {"time_scope": scope, "period": period, "goals": goals}
        for scope, period, goals in groups
    ]}


# --- gate: team daily budget -----------------------------------------------------------------

def test_budget_gate_stops_further_quests_once_reached():
    q1 = _quest("q1")
    q2 = _quest("q2")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")])),
             "q2": _goals_payload(("day", "2026-07-12", [_goal("g2")]))}
    client = FakeAutopilotClient(quests=[q1, q2], goals_by_quest=goals)
    passer = AutopilotPass(client, team_id="team1", daily_budget=1, now=_now)
    result = passer.run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 1
    assert any(s["quest_id"] == "q2" and "budget" in s["reason"] for s in result.skipped)


def test_budget_gate_counts_existing_autopilot_tasks_created_today():
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    existing = [{"id": "t0", "task_kind": "autopilot_work", "goal_id": "other",
                "status": "done", "created_at": "2026-07-12T01:00:00Z"}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, tasks=existing)
    passer = AutopilotPass(client, team_id="team1", daily_budget=1, now=_now)
    result = passer.run({"text": "autopilot pass"})
    # The budget was already consumed by a task created earlier TODAY -> q1 is skipped outright.
    assert result.created_task_ids == []
    assert any(s["quest_id"] == "q1" and "budget" in s["reason"] for s in result.skipped)


def test_budget_gate_ignores_tasks_created_on_a_different_day():
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    existing = [{"id": "t0", "task_kind": "autopilot_work", "goal_id": "other",
                "status": "done", "created_at": "2026-07-01T01:00:00Z"}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, tasks=existing)
    passer = AutopilotPass(client, team_id="team1", daily_budget=1, now=_now)
    result = passer.run({"text": "autopilot pass"})
    assert len(result.created_task_ids) == 1


# --- gate: cadence -----------------------------------------------------------------------------

def test_cadence_due_true_when_never_run():
    assert cadence_due({}, NOW) is True


def test_cadence_due_false_within_the_same_week():
    autopilot_cfg = {"cadence": "weekly", "last_pass_at": "2026-07-10T09:00:00Z"}  # same ISO week
    assert cadence_due(autopilot_cfg, NOW) is False


def test_cadence_due_true_in_a_later_week():
    autopilot_cfg = {"cadence": "weekly", "last_pass_at": "2026-07-01T09:00:00Z"}  # W27, NOW is W28
    assert cadence_due(autopilot_cfg, NOW) is True


def test_cadence_is_calendar_based_not_elapsed_time():
    """"Daily" means "not yet today", NOT "24 hours have elapsed", and the difference loses days.

    The pass task fires at a fixed wall-clock time. Under an elapsed-time reading, one late pass
    (a backed-up queue, a restart, a manual run at noon) puts the next morning's pass inside the
    24-hour window, so it skips, and a daily quest quietly becomes every-other-day. The real case
    that surfaced this: a pass ran at 16:11 and the following 06:00 pass was gated out."""
    late_yesterday = {"cadence": "daily", "last_pass_at": "2026-07-11T16:11:00Z"}
    assert cadence_due(late_yesterday, NOW) is True        # 16.8 hours elapsed, but a NEW DAY
    assert cadence_due({"cadence": "daily", "last_pass_at": "2026-07-12T08:00:00Z"}, NOW) is False
    # Monthly is likewise once per calendar month, not every 30 days.
    assert cadence_due({"cadence": "monthly", "last_pass_at": "2026-06-20T09:00:00Z"}, NOW) is True
    assert cadence_due({"cadence": "monthly", "last_pass_at": "2026-07-01T09:00:00Z"}, NOW) is False


def test_cadence_across_a_year_boundary():
    """Comparing (year, month) or the ISO (year, week) pair, never the bare month or week number,
    which would read December as later than the following January and wedge the quest for a year."""
    assert cadence_due({"cadence": "monthly", "last_pass_at": "2025-12-31T09:00:00Z"},
                       datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)) is True
    assert cadence_due({"cadence": "weekly", "last_pass_at": "2025-12-29T09:00:00Z"},
                       datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)) is True


def test_cadence_unparsable_timestamp_fails_open_to_due():
    assert cadence_due({"cadence": "weekly", "last_pass_at": "not-a-date"}, NOW) is True


def test_cadence_due_with_tz_reads_the_evening_run_in_its_own_local_day():
    """A 05:00 UTC ``last_pass_at`` is the PREVIOUS evening in America/Los_Angeles (22:00 PDT,
    UTC-7): 2026-08-21T05:00:00Z is 2026-08-20T22:00 local. Checked from a moment that is still
    the SAME UTC calendar day (2026-08-21) but already the NEXT calendar day in Los Angeles
    (2026-08-21T13:00 local), the two readings disagree: as UTC days it is the same day as
    ``last_pass_at`` (not due), as LOCAL days a new day has begun (due). This is the exact bug --
    read as UTC days, an evening run makes the FOLLOWING day's pass look like it already ran.
    """
    autopilot_cfg = {"cadence": "daily", "last_pass_at": "2026-08-21T05:00:00Z"}
    now = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)  # 2026-08-21T13:00 America/Los_Angeles
    assert cadence_due(autopilot_cfg, now, tz="America/Los_Angeles") is True


def test_cadence_due_without_tz_uses_the_runner_clock_not_utc(monkeypatch):
    """No ``run_timezone`` degrades to the RUNNER'S clock, never UTC -- ``local_time``'s one rule.

    This cost a real brief. Joshua's dissertation quest set no ``run_timezone``; a catch-up pass
    ran at 20:26 on a US/Pacific runner and stamped ``last_pass_at`` 03:26Z the NEXT UTC day. The
    next morning's pass compared UTC days, saw "already ran today", and the daily brief never
    arrived. Same instants as the test above, with the zone supplied by the host instead of the
    quest: the runner is in Los Angeles, so it must reach the same answer (due).
    """
    autopilot_cfg = {"cadence": "daily", "last_pass_at": "2026-08-21T05:00:00Z"}
    now = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    assert cadence_due(autopilot_cfg, now, tz=None) is True
    # ...and an unresolvable zone name degrades down the same path rather than to UTC. (The name
    # is unique to this test on purpose: local_time's warn-once set is process-global, so reusing
    # another test's bad name would swallow the warning it asserts on.)
    assert cadence_due(autopilot_cfg, now, tz="Not/ACadenceZone") is True


def test_gate_skips_quest_whose_cadence_is_not_due():
    q1 = _quest("q1", last_pass_at="2026-07-11T09:00:00Z")  # 1 day ago, weekly cadence
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert result.skipped == [{"quest_id": "q1", "quest_label": "ship the thing",
                               "reason": "cadence not due yet"}]


# --- gate: backpressure ---------------------------------------------------------------------

def test_an_unanswered_question_does_not_stop_the_quest_by_default():
    """Backpressure is OFF unless a deployment asks for it. A pending decision is something to work
    around, not a reason to down tools: one unanswered message must not stall a quest for days."""
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    existing = [{"id": "prev", "task_kind": "autopilot_work", "goal_id": "q1",
                "status": "needs_you"}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, tasks=existing)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert not any("backpressure" in s["reason"] for s in result.skipped)


def test_gate_skips_quest_with_open_autopilot_task_when_backpressure_is_enabled():
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    existing = [{"id": "prev", "task_kind": "autopilot_work", "goal_id": "q1",
                "status": "needs_you"}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, tasks=existing)
    passer = AutopilotPass(client, team_id="team1", backpressure=True, now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert any("backpressure" in s["reason"] for s in result.skipped)


def test_backpressure_gate_ignores_terminal_autopilot_tasks():
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    existing = [{"id": "prev", "task_kind": "autopilot_work", "goal_id": "q1",
                "status": "done"}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, tasks=existing)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1


# --- gate: open HOLD decision -----------------------------------------------------------------

def test_open_hold_decision_does_not_stop_a_pass_by_default():
    """An unresolved decision is a question the person has not answered yet. Treating it as a stop
    sign makes their silence an instruction to down tools, and everything independent of that
    question sits there workable while the quest stays frozen. The run sees the open decision in
    context and works around it."""
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    client.open_decisions["q1"] = True
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert not any("HOLD" in s["reason"] for s in result.skipped)


def test_gate_skips_quest_with_open_hold_decision_when_backpressure_is_enabled():
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    client.open_decisions["q1"] = True
    passer = AutopilotPass(client, team_id="team1", backpressure=True, now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert any("HOLD" in s["reason"] for s in result.skipped)


# --- gate ordering: cadence checked before backpressure before HOLD ----------------------------

def test_gate_order_cadence_before_backpressure_before_hold():
    # A quest failing ALL three gates reports the cadence reason (checked first).
    q1 = _quest("q1", last_pass_at="2026-07-11T09:00:00Z")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    existing = [{"id": "prev", "task_kind": "autopilot_work", "goal_id": "q1",
                "status": "queued"}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, tasks=existing)
    client.open_decisions["q1"] = True
    passer = AutopilotPass(client, team_id="team1", backpressure=True, now=_now)
    result = passer.run({"text": "pass"})
    assert result.skipped == [{"quest_id": "q1", "quest_label": "ship the thing",
                               "reason": "cadence not due yet"}]


# --- opt-in filtering: only suggest/act quests are scanned at all ------------------------------

def test_quests_with_mode_off_or_unset_are_never_touched():
    off_quest = {"quest_id": "qoff", "outcome": "x", "autopilot": {"mode": "off"}}
    unset_quest = {"quest_id": "qnone", "outcome": "x"}
    client = FakeAutopilotClient(quests=[off_quest, unset_quest])
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert result.skipped == []          # never-opted-in quests aren't even "skipped" with a gate reason
    assert client.autopilot_updates == []


# --- _eligible_quests -----------------------------------------------------------------------

def test_eligible_quests_with_only_quest_id_returns_just_that_quest_and_never_lists():
    q1 = _quest("q1", mode="act")
    q1["autopilot"]["run_time"] = "06:30"
    other = _quest("other", mode="act")   # must never be returned, and list_quests must never fire
    client = FakeAutopilotClient(quests=[q1, other])
    passer = AutopilotPass(client, team_id="team1", now=_now)

    def _must_not_be_called(**kw):
        raise AssertionError("a per-quest pass must never call list_quests")
    client.list_quests = _must_not_be_called

    eligible = passer._eligible_quests("q1")
    assert [q["quest_id"] for q in eligible] == ["q1"]


def test_eligible_quests_with_only_quest_id_no_longer_opted_in_returns_nothing_and_logs(caplog):
    q1 = _quest("q1", mode="off")
    q1["autopilot"]["run_time"] = "06:30"    # the pass series still exists; the quest opted out
    client = FakeAutopilotClient(quests=[q1])
    passer = AutopilotPass(client, team_id="team1", now=_now)
    with caplog.at_level("INFO"):
        eligible = passer._eligible_quests("q1")
    assert eligible == []
    assert "no longer opted in" in caplog.text


# --- the scope label ------------------------------------------------------------------------------
# It no longer selects anything. It says WHICH PERIOD the quest is planning in, which is what the
# reflection lookup, the previous-period summary, the ``Scope:`` line and a proposed goal's period
# all need to know.

def test_scope_label_names_todays_day_group_over_coarser_current_ones():
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("today1"), _goal("today2")]),
        ("week", "2026_W28", [_goal("week1")]),
    )
    assert current_scope_label(payload, NOW) == "day:2026-07-12"


def test_scope_label_falls_back_to_the_current_week_when_no_day_group_is_current():
    payload = _goals_payload(
        ("day", "2026-07-01", [_goal("stale_day")]),   # not today
        ("week", "2026_W28", [_goal("week1"), _goal("week2")]),
    )
    assert current_scope_label(payload, NOW) == "week:2026_W28"


def test_scope_label_is_about_the_calendar_not_about_what_is_left_to_do():
    """A day group whose goals are all finished still names the scope. Completing today's plan does
    not move the quest into next week, and this is a statement about the calendar."""
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("g", completed=True)]),
        ("week", "2026_W28", [_goal("w")]),
    )
    assert current_scope_label(payload, NOW) == "day:2026-07-12"


def test_scope_label_is_unscoped_when_no_group_is_current():
    assert current_scope_label({"period_groups": []}, NOW) == "unscoped"
    assert current_scope_label(_goals_payload(("custom", "backlog", [_goal("g")])), NOW) \
        == "unscoped"


# --- who gets a batch -----------------------------------------------------------------------------

def test_persona_resolves_quest_persona_matching_today_over_unrestricted():
    # NOW is a Sunday -> "%a" gives "Sun".
    autopilot_cfg = {"personas": [
        {"rep_id": "rep_unrestricted"},
        {"rep_id": "rep_sunday", "days": ["Sun"]},
    ]}
    assert resolve_persona(autopilot_cfg, NOW) == "rep_sunday"


def test_persona_falls_back_to_unrestricted_quest_persona():
    autopilot_cfg = {"personas": [{"rep_id": "rep_mon", "days": ["Mon"]},
                                  {"rep_id": "rep_any"}]}
    assert resolve_persona(autopilot_cfg, NOW) == "rep_any"


def test_persona_uses_fallback_resolver_when_nothing_else_matches():
    assert resolve_persona({}, NOW, fallback_resolver=lambda item: "rep_from_cards") \
        == "rep_from_cards"


def test_persona_none_when_no_resolver_matches():
    assert resolve_persona({}, NOW) is None


def test_an_adopted_tasks_own_rep_is_honoured_and_still_held_to_that_reps_days():
    """The only item that still names a character is a recurring task the person scheduled. Their
    naming wins over the roster's routing, and never over that character's own days."""
    task = {"id": "r1", "assignee_rep_id": "rep_bailey"}
    weekdays = {"personas": [{"rep_id": "rep_bailey", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]},
                             {"rep_id": "rep_batman", "days": ["Sun"]}]}
    assert resolve_task_persona(task, weekdays, NOW) is PERSONA_HELD      # NOW is a Sunday
    assert resolve_task_persona(task, weekdays, MONDAY) == "rep_bailey"
    # A character with no roster entry has no day setting to follow, so nothing holds them.
    assert resolve_task_persona({"assignee_rep_id": "rep_nobody_rostered"}, weekdays, NOW) \
        == "rep_nobody_rostered"
    # And an unnamed occurrence simply rides with whoever the quest routes to today.
    assert resolve_task_persona({"id": "r2"}, weekdays, NOW) == "rep_batman"


def test_every_character_on_duty_today_gets_one_batch():
    """With a default brief nobody on duty is idle, so the roster decides how many batches a pass
    makes. Three unrestricted personas produce three."""
    q1 = _quest("q1", personas=[{"rep_id": "rep_a"}, {"rep_id": "rep_b"}, {"rep_id": "rep_c"}])
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", daily_budget=5, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 3
    assert [t["assignee_rep_id"] for t in client.created_tasks] == ["rep_a", "rep_b", "rep_c"]


def test_the_daily_budget_is_what_bounds_the_batches_a_roster_can_produce():
    q1 = _quest("q1", personas=[{"rep_id": "rep_a"}, {"rep_id": "rep_b"}, {"rep_id": "rep_c"}])
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", daily_budget=2, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 2
    assert any("budget" in s["reason"] for s in result.skipped)


def test_a_quest_with_no_roster_gets_one_plain_assistant_batch():
    """An unconfigured quest still does something useful: one batch, no character, on the default
    brief."""
    client = FakeAutopilotClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", daily_budget=5, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert "assignee_rep_id" not in created
    assert BUNDLED_DEFAULT_INSTRUCTIONS in created["text"]


# --- the default brief ----------------------------------------------------------------------------
# A persona with no instructions of its own, on a quest with no quest-wide instructions, works to
# the brief the backend serves rather than producing nothing.

def test_a_persona_with_no_brief_on_a_quest_with_none_works_to_the_served_default():
    served = "The server's own default brief for a quest nobody has configured."
    q1 = _quest("q1", personas=[{"rep_id": "rep_a"}], default_instructions=served)
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    text = client.created_tasks[0]["text"]
    assert served in text
    assert "Neither this quest nor this character has a brief written for it" in text


def test_a_persona_with_its_own_instructions_does_not_get_the_default():
    served = "The server's own default brief."
    q1 = _quest("q1", personas=[{"rep_id": "rep_a", "instructions": "Do Bailey's own job."}],
                default_instructions=served)
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Do Bailey's own job." in text
    assert served not in text
    assert BUNDLED_DEFAULT_INSTRUCTIONS not in text


def test_a_quest_wide_brief_keeps_the_default_out_even_for_a_persona_with_none():
    """The quest's stand: its owner wrote what this quest is for, and a built-in brief alongside it
    would be a second specification for the same run with nothing ranking the two."""
    served = "The server's own default brief."
    q1 = _quest("q1", personas=[{"rep_id": "rep_a"}], default_instructions=served)
    q1["autopilot"]["instructions"] = "Write the daily brief for this quest."
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Write the daily brief for this quest." in text
    assert served not in text
    assert BUNDLED_DEFAULT_INSTRUCTIONS not in text


def test_an_older_backend_that_serves_no_default_degrades_to_the_bundled_one():
    """The runner must degrade to a real brief rather than to silence. The bundled copy is a
    fallback only: the server's value wins whenever there is one."""
    assert default_instructions_for({}) == BUNDLED_DEFAULT_INSTRUCTIONS
    assert default_instructions_for({"default_instructions": "   "}) == BUNDLED_DEFAULT_INSTRUCTIONS
    assert default_instructions_for({"default_instructions": "served"}) == "served"

    q1 = _quest("q1", personas=[{"rep_id": "rep_a"}])       # the fake serves no such field
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert BUNDLED_DEFAULT_INSTRUCTIONS in client.created_tasks[0]["text"]


# --- goals reach a run through the ladder and no other way ---------------------------------------

def test_a_current_goal_is_named_as_context_and_never_composed_as_an_instruction():
    q1 = _quest("q1")
    goals_payload = _goals_payload(("day", "2026-07-12", [
        _goal("g1", name="Draft chapter three"), _goal("g2", name="Read the Thagard chapter"),
    ]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "THE PERSON'S CURRENT GOALS" in text
    assert "- Draft chapter three" in text
    assert "- Read the Thagard chapter" in text
    assert "Goal: " not in text
    assert "Done when:" not in text
    assert "This run does not own these goals" in text


# --- suggest vs act status + env_id stamping -----------------------------------------------------

def test_suggest_mode_lands_the_task_suggested_atomically_at_creation():
    """A suggestion must NEVER exist in a runnable state, not even briefly.

    This used to be a create-then-PATCH: the task was created queued and demoted afterwards. In
    between those two calls the runner's poll could claim and EXECUTE it, which is exactly the
    human approval suggest mode exists to require. The status is asserted at creation now, so
    there is no window and no follow-up PATCH."""
    q1 = _quest("q1", mode="suggest")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert client.created_tasks[0]["status"] == "suggested"
    assert client.task_updates == []                       # no demotion window to close


def test_act_mode_creates_the_task_queued():
    q1 = _quest("q1", mode="act")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert client.task_updates == []
    assert client.created_tasks[0]["status"] == "queued"


def test_created_task_links_to_the_QUEST_not_a_per_goal_id():
    """A task's ``goal_id`` holds a QUEST id (the API resolves it with get_quest and 404s
    anything else). Autopilot used to pass the first target goal's own id from
    ``list_quest_goals`` -- a different document with a different id -- so every work-batch
    creation failed outright, and any that survived would have been invisible to the
    backpressure gate, which looks tasks up by quest id. Which goals a batch covers lives in
    the task TEXT."""
    q1 = _quest("q1", env_id="env-personal")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1", name="Draft chapter 2")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    created = client.created_tasks[0]
    assert created["env_id"] == "env-personal"
    assert created["goal_id"] == "q1"          # the QUEST, never the per-goal id
    assert "Draft chapter 2" in created["text"]
    # The autopilot-authored marker is the PERSISTENT task_kind, and it must be the WORK kind,
    # never the pass kind (which the executor would route into another autopilot pass: a loop).
    assert created["task_kind"] == "autopilot_work"
    from quest_ai_runner.runner.autopilot import AUTOPILOT_PASS_KIND
    assert created["task_kind"] != AUTOPILOT_PASS_KIND
    # source must be a value the API's closed enum accepts, or the create 400s.
    assert created["source"] in ("chat", "reflection", "review")


def test_resolved_persona_is_stamped_structurally_not_only_in_the_prose():
    """The create route carries the persona in ``assignee_rep_id``. It still rides in the text
    too (some consumers resolve from prose), but a resolver reading a field beats one parsing
    a sentence."""
    q1 = _quest("q1", personas=[{"rep_id": "rep_bailey", "days": ["Sun"]}])  # _now is a Sunday
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    created = client.created_tasks[0]
    assert created["assignee_rep_id"] == "rep_bailey"
    assert "Act as rep_bailey" in created["text"]


def test_no_resolved_persona_sends_no_rep_field():
    q1 = _quest("q1")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert "assignee_rep_id" not in client.created_tasks[0]


# --- planning: the goal proposal --------------------------------------------------------------
#
# The proposal fires when a pass produced NO work batch for a plan_and_work quest. Since the
# default brief landed, a due pass always has at least one character with an effective brief, so
# the branch is not reachable through ``run`` any more -- the first test below is what pins that,
# and the rest drive ``_handle_proposal`` directly, because how a proposal is made is a live
# surface that quest-backend still renders and re-declares ``PROPOSAL_TEXT_PREFIX`` against.

def _propose(client, quest, *, mode="act", dry_run=False, scope_label="day:2026-07-12",
             result=None):
    """Run the proposal path for one quest, the way ``_run_one_quest`` would with no batches."""
    from quest_ai_runner.runner.autopilot import AutopilotResult

    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = result if result is not None else AutopilotResult(ran_at=NOW, dry_run=dry_run)
    label = quest.get("outcome") or quest["quest_id"]
    reason = passer._handle_proposal(quest, quest["quest_id"], label, mode, dry_run, result,
                                     scope_label=scope_label)
    return result, reason


def test_a_due_pass_always_produces_work_so_no_goal_is_proposed():
    """The consequence of the default brief, stated as a test: a plan_and_work quest with no goals
    and no roster now gets its plain-assistant batch, where it used to get a proposal."""
    q1 = _quest("q1", planning="plan_and_work")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert not client.created_tasks[0]["text"].startswith("Proposed goal:")
    assert not any(c.get("kind") == "goal_proposal" for c in result.created)


def test_a_proposal_is_created_as_a_suggested_task_on_the_quest():
    q1 = _quest("q1", planning="plan_and_work")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result, reason = _propose(client, q1)
    assert reason is None
    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["text"].startswith("Proposed goal:")
    # A proposal is ALWAYS surfaced for a human, even on an `act` quest -> created suggested.
    assert created["status"] == "suggested"
    assert client.task_updates == []
    # The proposal lands on the QUEST (goal_id = quest id).
    assert created["goal_id"] == "q1"


def test_a_proposal_still_waiting_on_the_person_is_not_proposed_again():
    """Reported 2026-08-22: the same proposed goal, and the same "waiting for your approval" line,
    in pass after pass ("over and over, not useful at all"). A proposal is one question; asking it
    again because it has not been answered is noise, and it piles up duplicate rows in the
    person's task list."""
    q1 = _quest("q1", planning="plan_and_work", outcome="I have a PhD")
    pending = {"id": "atask_earlier", "goal_id": "q1", "status": "suggested",
               "task_kind": AUTOPILOT_WORK_KIND, "title": "Next step toward: I have a PhD",
               "text": "Proposed goal: Next step toward: I have a PhD\n\nPropose and take..."}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={}, tasks=[pending])
    result, reason = _propose(client, q1)

    assert client.created_tasks == []
    assert "still waiting for your yes or no" in reason
    # Nothing was created, so nothing is claimed: the report must not say a proposal is waiting
    # for approval as though this pass had just made one.
    assert "accept or reject" not in result.summary_text()


def test_a_proposal_the_person_already_answered_does_not_block_the_next_one():
    """The guard is about a question still open, not about ever proposing again. Once the earlier
    proposal is resolved (accepted, declined, or otherwise closed), the quest can be proposed for
    again."""
    q1 = _quest("q1", planning="plan_and_work")
    answered = {"id": "atask_earlier", "goal_id": "q1", "status": "done",
                "task_kind": AUTOPILOT_WORK_KIND,
                "text": "Proposed goal: Next step toward: ship the thing"}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={}, tasks=[answered])
    _result, reason = _propose(client, q1)

    assert reason is None
    assert len(client.created_tasks) == 1
    assert client.created_tasks[0]["text"].startswith("Proposed goal:")


def test_an_open_work_task_is_not_mistaken_for_an_open_proposal():
    """Only a PROPOSAL blocks a proposal. An ordinary autopilot work task sitting queued on the
    quest is the backpressure gate's business (opt-in, off by default), never this one's."""
    q1 = _quest("q1", planning="plan_and_work")
    work = {"id": "atask_work", "goal_id": "q1", "status": "queued",
            "task_kind": AUTOPILOT_WORK_KIND, "text": "Act as bailey.\n\nWrite the daily brief."}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={}, tasks=[work])
    _result, reason = _propose(client, q1)

    assert reason is None
    assert len(client.created_tasks) == 1


def test_a_persons_own_task_that_mentions_a_proposal_never_blocks_one():
    """The marker is autopilot's own authorship plus its own text prefix. A task the PERSON wrote
    is not autopilot's pending question, whatever it happens to say."""
    q1 = _quest("q1", planning="plan_and_work")
    theirs = {"id": "atask_human", "goal_id": "q1", "status": "queued",
              "text": "Proposed goal: something I typed myself"}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={}, tasks=[theirs])
    _result, reason = _propose(client, q1)

    assert reason is None
    assert len(client.created_tasks) == 1


def test_a_proposed_goal_is_reported_once_and_without_repeating_the_quest():
    """The reported line was "Proposed goal: Next step toward: <outcome> (on <outcome>)": the
    quest's outcome three times, under a heading about work that can "run" when no work exists
    yet."""
    q1 = _quest("q1", planning="plan_and_work",
                outcome="I've completed my dissertation and have a PhD")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result, _reason = _propose(client, q1)
    text = result.summary_text()

    assert "A goal proposed for you to accept or reject:" in text
    assert "Next step toward: I've completed my dissertation and have a PhD" in text
    assert "(on I've completed my dissertation and have a PhD)" not in text
    assert "Proposed goal: Next step toward:" not in text
    assert "before it can run" not in text
    assert text.count("I've completed my dissertation and have a PhD") == 1


def test_work_awaiting_approval_and_a_proposal_are_reported_as_different_things():
    q1 = _quest("q1", mode="suggest", outcome="Ship the launch")
    q1["autopilot"]["instructions"] = "Draft the rubric"
    q2 = _quest("q2", mode="suggest", planning="plan_and_work", outcome="Learn Spanish")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1, q2], goals_by_quest=goals, accepts_bookkeeping=True)
    result = AutopilotPass(client, team_id="team1", daily_budget=5,
                           now=_now).run({"text": "pass"})
    # The work batches are real; the proposal is driven onto the same result, since a pass that
    # produced batches cannot reach the proposal path on its own any more.
    _propose(client, q2, mode="suggest", result=result)
    text = result.summary_text()

    assert "Waiting for your approval before" in text
    assert "Draft the rubric" in text
    assert "A goal proposed for you to accept or reject:" in text
    assert "Next step toward: Learn Spanish" in text


def test_act_mode_goal_proposal_is_still_only_suggested():
    q1 = _quest("q1", mode="act", planning="plan_and_work")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    _propose(client, q1, mode="act")
    assert client.created_tasks[0]["status"] == "suggested"


# --- the proposal's REAL goal object (QuestClient.create_goal) -----------------------------------

class StrictGoalClient(FakeAutopilotClient):
    """A client whose ``create_goal`` mirrors ``QuestClient.create_goal``'s signature EXACTLY.

    The fidelity is the whole test. The old call was
    ``create_goal(quest_id, name=..., description=..., ai_help=True, created_by="ai")``: the quest
    id landed in ``title``, ``name`` and ``created_by`` are not parameters at all, and the required
    keyword-only ``period`` was missing. Against a stub that swallowed ``**kwargs`` that call would
    have passed a test happily while raising TypeError against the real client on every pass, which
    is exactly what it did in production. So this raises the same TypeError the real client raises,
    and validates ``period`` against the same five formats.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.created_goals = []

    def create_goal(self, title, *, period, quest_id=None, description=None, criteria=None,
                    goal_type=None, parent_goal_id=None, target_value=None, target_unit=None,
                    ai_help=None, assignee_rep_id=None):
        assert _PERIOD_RE.match(period), f"the real client rejects period {period!r}"
        self.created_goals.append({"title": title, "period": period, "quest_id": quest_id,
                                   "description": description, "ai_help": ai_help})
        return {"id": f"goal_{len(self.created_goals)}"}


def test_an_act_mode_proposal_creates_the_real_goal_with_the_right_arguments():
    """This was broken from the moment ``QuestClient.create_goal`` landed: every call raised
    TypeError into a bare ``except``, which logged one warning and returned None, so an AI-proposed
    goal was never actually created. It went unnoticed because the branch is unreachable for any
    quest carrying standing instructions, so no log line ever appeared to contradict it."""
    q1 = _quest("q1", mode="act", planning="plan_and_work", outcome="Get the PhD")
    client = StrictGoalClient(quests=[q1], goals_by_quest={})
    _propose(client, q1, mode="act")

    assert len(client.created_goals) == 1
    created = client.created_goals[0]
    assert created["title"] == "Next step toward: Get the PhD"   # the TITLE, positionally
    assert created["quest_id"] == "q1"                           # ...and the quest id as a keyword
    assert created["period"] == "2026-07-12"                     # from this pass's own scope
    # Nothing flags the goal as AI-workable, because that flag no longer exists on a goal: a goal
    # is the plan the work serves, never a unit of AI work.
    assert created["ai_help"] is None
    assert created["description"].startswith("Propose and take the next concrete step")
    # The proposal task still points at the goal it created, which is how a person finds it.
    assert "(Created as goal goal_1 on this quest.)" in client.created_tasks[0]["text"]


def test_the_created_goals_period_follows_the_passs_scope_for_every_scope_shape():
    """``period`` is required and has no universal default, so it is derived from the scope label
    ``current_scope_label`` already returns. An unscoped quest has no current period at all and
    falls back to the current month, in the ``YYYY_MM`` form the client validates."""
    client = StrictGoalClient(quests=[_quest("q1", mode="act")])
    passer = AutopilotPass(client, team_id="team1", now=_now)
    for scope_label, expected in (("day:2026-07-12", "2026-07-12"),
                                  ("week:2026_W28", "2026_W28"),
                                  ("month:2026_09", "2026_09"),
                                  ("quarter:2026_Q3", "2026_Q3"),
                                  ("year:2026", "2026"),
                                  ("unscoped", "2026_07"),
                                  ("", "2026_07")):
        assert passer._maybe_create_goal("q1", "A title", "A description", "act",
                                         scope_label) is not None
        assert client.created_goals[-1]["period"] == expected


def test_suggest_mode_proposes_the_task_but_creates_no_goal_object():
    """Creating the goal on a quest whose owner asked to approve first would be autopilot writing
    the plan it is meant to be suggesting."""
    q1 = _quest("q1", mode="suggest", planning="plan_and_work")
    client = StrictGoalClient(quests=[q1], goals_by_quest={})
    _propose(client, q1, mode="suggest")

    assert client.created_goals == []
    assert len(client.created_tasks) == 1
    assert client.created_tasks[0]["text"].startswith("Proposed goal:")
    assert "Created as goal" not in client.created_tasks[0]["text"]


def test_a_client_without_create_goal_still_proposes_the_task():
    """The goal object is an optional extra; the proposal itself never depends on it."""
    q1 = _quest("q1", mode="act", planning="plan_and_work")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result, _reason = _propose(client, q1, mode="act")

    assert len(result.created_task_ids) == 1
    assert client.created_tasks[0]["text"].startswith("Proposed goal:")


def test_a_bad_create_goal_signature_is_logged_loudly_and_never_fails_the_pass(caplog):
    """A TypeError here is a programming error in THIS file, not an endpoint the deployment lacks,
    and the two must not look the same in the logs: the old code logged both as one warning line,
    which is how a call that could never bind survived unnoticed. The proposal task is still
    created."""
    class MismatchedGoalClient(FakeAutopilotClient):
        def create_goal(self, title, *, period, description=None, ai_help=None):
            raise AssertionError("unreachable: quest_id cannot bind, so this never runs")

    q1 = _quest("q1", mode="act", planning="plan_and_work")
    client = MismatchedGoalClient(quests=[q1], goals_by_quest={})
    with caplog.at_level("ERROR"):
        result, _reason = _propose(client, q1, mode="act")

    assert len(result.created_task_ids) == 1                    # the proposal still lands
    assert "Created as goal" not in client.created_tasks[0]["text"]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("create_goal call is not compatible" in r.getMessage() for r in errors)
    assert any(r.exc_info for r in errors)                      # with the traceback attached


def test_a_failing_create_goal_endpoint_stays_a_warning(caplog):
    """The other half of the split: an endpoint that exists and misbehaves is an operational
    condition, not a bug in the call, so it stays a warning and the proposal carries on."""
    class FailingGoalClient(StrictGoalClient):
        def create_goal(self, title, **kwargs):
            raise RuntimeError("the API said no")

    q1 = _quest("q1", mode="act", planning="plan_and_work")
    client = FailingGoalClient(quests=[q1], goals_by_quest={})
    with caplog.at_level("WARNING"):
        result, _reason = _propose(client, q1, mode="act")

    assert len(result.created_task_ids) == 1
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert "create_goal failed for quest q1" in caplog.text


# --- dry-run creates nothing ---------------------------------------------------------------------

def test_dry_run_creates_no_tasks_and_reports_proposals():
    q1 = _quest("q1")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1"), _goal("g2")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "autopilot dry-run please"})
    assert result.dry_run is True
    assert result.created_task_ids == []
    assert client.created_tasks == []
    assert client.autopilot_updates == []          # no bookkeeping writes either
    assert len(result.proposals) == 1
    assert result.proposals[0]["kind"] == "work_batch"
    assert result.proposals[0]["quest_id"] == "q1"
    # With no brief of either kind written, the report says which brief the run would work to.
    assert result.proposals[0]["instructions_from"] == "the built-in default brief"


def test_dry_run_does_not_consume_or_check_daily_budget_from_prior_tasks():
    """A dry-run must never touch the real budget count (no network-ish 'today' listing consulted
    for gating in a way that could itself be mistaken for consumption); it always starts fresh so
    the reported proposals reflect what the NEXT real pass could do."""
    q1 = _quest("q1")
    q2 = _quest("q2")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")])),
             "q2": _goals_payload(("day", "2026-07-12", [_goal("g2")]))}
    client = FakeAutopilotClient(quests=[q1, q2], goals_by_quest=goals)
    passer = AutopilotPass(client, team_id="team1", daily_budget=1, now=_now)
    result = passer.run({"text": "dry-run"})
    # Both quests get a proposal in dry-run mode; the budget gate only starts biting on the
    # SECOND quest within this same dry pass (mirrors the real per-quest counting behavior).
    assert len(result.proposals) == 1
    assert any("budget" in s["reason"] for s in result.skipped)


def test_dry_run_proposes_goal_proposal_without_creating_a_goal_or_task():
    q1 = _quest("q1", planning="plan_and_work")
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    result, _reason = _propose(client, q1, dry_run=True)
    assert client.created_tasks == []
    assert len(result.proposals) == 1
    assert result.proposals[0]["kind"] == "goal_proposal"
    assert "outcome" in result.proposals[0]["description"] or result.proposals[0]["title"]


# --- compose_batch_text ---------------------------------------------------------------------

def test_compose_batch_text_states_the_quest_outcome_and_the_scope_it_serves():
    text = compose_batch_text("Ship the launch", scope_label="day:2026-07-12")
    assert "Quest outcome: Ship the launch" in text
    assert "Scope: this quest's day:2026-07-12" in text
    assert "that PERIOD's target, not this single run's" in text


def test_compose_batch_text_names_the_persona_when_one_resolved():
    text = compose_batch_text("Ship it", "bailey")
    assert "Act as bailey" in text


def test_the_default_brief_is_emitted_only_when_neither_written_brief_exists():
    """THE LAYERING RULE, at the one place that decides it. The default is a floor, not a layer:
    anything the person wrote at either level replaces it outright, and emitting both would hand
    the run two specifications for the same job with nothing ranking them."""
    default = "The built-in brief."
    alone = compose_batch_text("Ship it", default_instructions=default)
    assert default in alone
    assert "Neither this quest nor this character has a brief written for it" in alone

    with_quest = compose_batch_text("Ship it", instructions="The quest's own brief.",
                                    default_instructions=default)
    assert "The quest's own brief." in with_quest
    assert default not in with_quest

    with_persona = compose_batch_text("Ship it", "bailey",
                                      persona_instructions="Bailey's own brief.",
                                      default_instructions=default)
    assert "Bailey's own brief." in with_persona
    assert default not in with_persona


def test_no_default_brief_composes_byte_identically_to_before_the_parameter_existed():
    assert (compose_batch_text("Ship it", "bailey", default_instructions=None)
            == compose_batch_text("Ship it", "bailey"))


def test_previous_block_marks_runs_the_person_cleared_from_their_feed():
    """A dismissal is the only unprompted signal the feed produces. If the pass cannot see it, the
    person clears the same kind of run every morning and nothing ever changes."""
    previous = {"period": "2026-08-18", "tasks": [
        {"status": "done", "title": "Daily brief", "dismissed_at": "2026-08-18T15:00:00Z"},
        {"status": "done", "title": "Gap 3 review"},
    ]}
    text = compose_batch_text("Ship it", previous=previous)
    assert "Daily brief [they cleared this from their feed]" in text
    assert "Gap 3 review [they cleared" not in text
    assert "feedback, not a failure and not a request" in text


def test_previous_block_stays_quiet_about_dismissals_when_there_are_none():
    previous = {"period": "2026-08-18", "tasks": [{"status": "done", "title": "Gap 3 review"}]}
    text = compose_batch_text("Ship it", previous=previous)
    assert "cleared this from their feed" not in text


def test_compose_batch_text_always_states_who_confirms_work_is_done():
    """A pass cannot observe whether the person did the thing, so left to infer it treats its own
    assignment as the event and issues something new each period while the first item is still
    untouched. The rule is unconditional: it must be there with no previous-period rows to read
    it against, since a first pass can hand out work just as blindly as a tenth."""
    text = compose_batch_text("Ship it")
    assert "WHAT COUNTS AS DONE" in text
    assert "Only the person confirms their own work" in text
    assert "repeat THAT item rather than replacing it" in text


def test_compose_batch_text_confirmation_rule_survives_a_previous_period_block():
    """The two belong together: the previous block says what happened, the rule says what may be
    concluded from it. Emitting the rows without the rule is what let 're-sequence' get read as
    'swap in a fresh item'."""
    previous = {"period": "2026-08-18", "goals": [{"name": "Read Thagard", "completed": False}]}
    text = compose_batch_text("Ship it", previous=previous)
    assert "Goals left INCOMPLETE" in text
    assert "WHAT COUNTS AS DONE" in text
    assert "never means swapping an untouched item" in text


# --- REAL-CONTRACT fidelity: the shapes verified against quest-backend's july branch -------------

def test_autopilot_mode_is_read_from_the_quest_state_not_the_team_listing():
    """The team quest LISTING carries no `autopilot` block (only quest_id/outcome/completed/
    owner_user_ids). If the pass read the opt-in off those rows it would see every quest as "off"
    and silently do nothing forever. It must read the full quest state per quest."""
    q1 = _quest("q1", mode="act")
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    # Sanity: the listing genuinely has no autopilot block, like the real endpoint.
    assert "autopilot" not in client.list_quests()[0]
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1   # it still found the opted-in quest


def test_no_per_goal_document_is_ever_fetched():
    """The pass used to read each target goal's full document for its description, because that
    description was the run's brief. Nothing is briefed from a goal any more, so that read is gone
    and a quest costs exactly one goals call."""
    q1 = _quest("q1")
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    client.goal_docs["g1"] = {"id": "g1", "name": "A goal", "description": "a per-goal brief"}
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert "a per-goal brief" not in client.created_tasks[0]["text"]
    assert client.goal_list_calls == ["q1"]


def test_backpressure_matches_a_task_by_goal_id_because_tasks_have_no_quest_id():
    """A task's link to its quest IS its goal_id (the API resolves that field as a quest id);
    there is no quest_id field on a task at all."""
    q1 = _quest("q1")
    existing = [{"id": "prev", "task_kind": "autopilot_work", "goal_id": "q1",
                "status": "in_progress"}]
    client = FakeAutopilotClient(
        quests=[q1], tasks=existing,
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    passer = AutopilotPass(client, team_id="team1", backpressure=True, now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert any("backpressure" in s["reason"] for s in result.skipped)


def test_budget_counts_a_legacy_source_autopilot_row_too():
    """Forward/backward compatible: if the backend's source enum ever gains "autopilot", rows
    stamped that way still count against the budget and are never miscounted as human work."""
    q1 = _quest("q1")
    existing = [{"id": "t0", "source": "autopilot", "goal_id": "other", "status": "done",
                "created_at": "2026-07-12T01:00:00Z"}]
    client = FakeAutopilotClient(
        quests=[q1], tasks=existing,
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    passer = AutopilotPass(client, team_id="team1", daily_budget=1, now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert any("budget" in s["reason"] for s in result.skipped)


def test_a_human_task_on_the_quest_does_not_trigger_backpressure():
    """Backpressure is about OUR unread output, not the human's own tasks."""
    q1 = _quest("q1")
    existing = [{"id": "human", "source": "chat", "goal_id": "q1", "status": "queued"}]
    client = FakeAutopilotClient(
        quests=[q1], tasks=existing,
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1


def test_bookkeeping_that_the_backend_silently_drops_is_reported_not_swallowed():
    """The quest autopilot PATCH endpoint does not accept last_pass_at/miss_streak today: it
    returns 200 and persists nothing. Assuming success would leave last_pass_at null forever and
    make the cadence gate permanently inert. The pass must VERIFY the echo and report loudly."""
    q1 = _quest("q1")
    client = FakeAutopilotClient(              # accepts_bookkeeping=False -> drops them, like prod
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1              # the work still happened
    assert len(result.bookkeeping_warnings) == 1
    warning = result.bookkeeping_warnings[0]
    assert warning["quest_id"] == "q1"
    assert "last_pass_at" in warning["detail"]
    assert "did not persist" in warning["detail"]
    assert "WARNING" in result.summary_text()


def test_bookkeeping_that_persists_raises_no_warning():
    """The same code path against a backend whose autopilot PATCH DOES accept the bookkeeping
    fields: the write sticks, and nothing is reported."""
    q1 = _quest("q1")
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))},
        accepts_bookkeeping=True)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.bookkeeping_warnings == []
    assert client.autopilot_updates == [("q1", {"last_pass_at": "2026-07-12T09:00:00Z",
                                                "miss_streak": 0})]
    # And the stamp is now readable back, so the cadence gate can actually hold next time.
    assert client.get_quest_autopilot("q1")["autopilot"]["last_pass_at"] == "2026-07-12T09:00:00Z"


def test_period_keys_match_the_backends_underscore_format():
    """quest-backend's period ids use an UNDERSCORE (period_utils.get_current_period): day is a
    plain ISO date, but week/month/quarter are 2026_W28 / 2026_07 / 2026_Q3. A hyphen here would
    never match the current period, so every quest would silently fall through to the unscoped
    fallback and today's goals would never be worked."""
    from quest_ai_runner.runner.autopilot import _current_period_key

    assert _current_period_key("day", NOW) == "2026-07-12"
    assert _current_period_key("week", NOW) == "2026_W28"
    assert _current_period_key("month", NOW) == "2026_07"
    assert _current_period_key("quarter", NOW) == "2026_Q3"
    assert _current_period_key("year", NOW) == "2026"


def test_the_current_month_group_is_recognized_with_the_real_underscore_period():
    q1 = _quest("q1")
    client = FakeAutopilotClient(
        quests=[q1],
        goals_by_quest={"q1": _goals_payload(("month", "2026_07",
                                              [_goal("m1", name="Submit the chapter"),
                                               _goal("m2", name="Book the committee")]))})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    text = client.created_tasks[0]["text"]
    assert "Scope: this quest's month:2026_07" in text
    assert "This month (2026_07):" in text
    assert "- Submit the chapter" in text
    assert "- Book the committee" in text


# --- per-quest error isolation: one quest's failure never aborts the pass -----------------------

def test_one_quest_error_is_isolated_and_others_still_run():
    q_bad = _quest("qbad")
    q_good = _quest("qgood")

    class BoomOnGoalsClient(FakeAutopilotClient):
        def list_quest_goals(self, quest_id, *, team_id=None):
            if quest_id == "qbad":
                raise RuntimeError("backend exploded")
            return super().list_quest_goals(quest_id, team_id=team_id)

    goals = {"qgood": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = BoomOnGoalsClient(quests=[q_bad, q_good], goals_by_quest=goals)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1          # the good quest still ran
    assert any(e["quest_id"] == "qbad" for e in result.errors)


# --- what the pass REPORTS ---------------------------------------------------------------------
# The pass row is read by a person, in their quest. "Created 1 task(s): atask_d2014273cff6" names an
# internal id instead of the work and presents the scanner's bookkeeping as the outcome.

def test_the_report_names_the_work_and_the_quest_never_a_task_id():
    q1 = _quest("q1")
    q1["outcome"] = "Get 20 psychics certified"
    q1["autopilot"]["mode"] = "act"
    q1["autopilot"]["instructions"] = "Draft the certification rubric"
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, accepts_bookkeeping=True)
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})

    text = result.summary_text()
    assert "Draft the certification rubric" in text
    assert "Get 20 psychics certified" in text
    assert "task(s)" not in text
    assert not any(task_id in text for task_id in result.created_task_ids)


def test_work_awaiting_approval_is_reported_as_waiting_not_as_started():
    """suggest mode creates nothing that runs. Saying "started" would be a plain untruth, and the
    person would never learn that the work is sitting there waiting for them."""
    q1 = _quest("q1", mode="suggest")
    q1["outcome"] = "Get 20 psychics certified"
    q1["autopilot"]["instructions"] = "Draft the rubric"
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, accepts_bookkeeping=True)
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})

    text = result.summary_text()
    assert "Waiting for your approval" in text
    assert "Autopilot started" not in text


def test_the_pass_stamps_itself_as_the_parent_of_the_work_it_creates():
    """The link that lets a consumer report the WORK as what autopilot did: the finished task's own
    output can be rolled back onto the pass row that created it."""
    q1 = _quest("q1")
    q1["autopilot"]["mode"] = "act"
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, accepts_bookkeeping=True)
    AutopilotPass(client, team_id="team1", now=_now).run(
        {"id": "atask_thepass", "text": "pass"})

    assert [t.get("parent_task_id") for t in client.created_tasks] == ["atask_thepass"]


def test_a_client_whose_create_task_predates_parent_task_id_still_gets_its_task():
    """The link is an improvement to how a pass reports, never a requirement for it to work."""
    class OlderClient(FakeAutopilotClient):
        def create_task(self, text, **kwargs):
            if "parent_task_id" in kwargs:
                raise TypeError("create_task() got an unexpected keyword argument")
            return super().create_task(text, **kwargs)

    q1 = _quest("q1")
    q1["autopilot"]["mode"] = "act"
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = OlderClient(quests=[q1], goals_by_quest=goals, accepts_bookkeeping=True)
    result = AutopilotPass(client, team_id="team1", now=_now).run(
        {"id": "atask_thepass", "text": "pass"})

    assert len(result.created_task_ids) == 1


def test_no_opted_in_quests_reports_clean_empty_summary():
    client = FakeAutopilotClient(quests=[])
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert "No quest has autopilot switched on" in result.summary_text()


def test_daily_budget_defaults_to_the_documented_default():
    client = FakeAutopilotClient(quests=[])
    passer = AutopilotPass(client, team_id="team1")
    assert passer._daily_budget == DEFAULT_TEAM_DAILY_BUDGET == 3


# --- executor routing: handler == "autopilot" replaces the normal deep-run path ------------------

def test_executor_routes_autopilot_handler_to_the_wired_pass():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator
    from quest_ai_runner.runner.executor import TaskExecutor

    from .conftest import StubProvider, StubRetrieval
    from .test_runner import MockQuestClient

    q1 = _quest("q1")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    autopilot_client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(autopilot_client, team_id="team1", now=_now)

    provider = StubProvider(decisions=[])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider))
    task_client = MockQuestClient([])
    ex = TaskExecutor(task_client, orch, autopilot_pass=passer)

    out = ex.execute({"id": "pass-1", "text": "autopilot pass", "handler": "autopilot"})
    assert out.status == "done"
    assert provider.plan_calls == 0            # never touched the normal plan/answer/deep loop
    assert len(autopilot_client.created_tasks) == 1
    assert task_client.reports[0][:2] == ("pass-1", "done")


def test_executor_reports_failed_when_autopilot_handler_but_no_pass_wired():
    from quest_ai_runner.core.model_registry import ModelRegistry
    from quest_ai_runner.core.orchestrator import Orchestrator
    from quest_ai_runner.runner.executor import TaskExecutor

    from .conftest import StubProvider, StubRetrieval
    from .test_runner import MockQuestClient

    provider = StubProvider(decisions=[])
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider))
    client = MockQuestClient([])
    ex = TaskExecutor(client, orch)  # no autopilot_pass wired

    out = ex.execute({"id": "pass-2", "text": "autopilot pass", "handler": "autopilot"})
    assert out.status == "failed"
    assert "not configured" in out.result


def test_executor_routes_on_task_kind_even_after_a_claim_overwrote_the_handler():
    """THE ROUTING SOUNDNESS TEST. The poller stamps ``handler`` on EVERY claim with the claiming
    worker's own label, overwriting whatever was there. So a pass task that is re-polled, retried,
    or resumed after a claim no longer has handler == "autopilot" -- routing on handler alone would
    silently run the pass task as an ordinary deep task. ``task_kind`` is persistent and is never
    touched by the claim path, so it must be what decides the route."""
    from quest_ai_runner.runner.executor import TaskExecutor

    from .conftest import StubProvider
    from .test_runner import MockQuestClient, _brain

    q1 = _quest("q1")
    autopilot_client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    passer = AutopilotPass(autopilot_client, team_id="team1", now=_now)

    provider = StubProvider(decisions=[])
    task_client = MockQuestClient([])
    ex = TaskExecutor(task_client, _brain(provider), autopilot_pass=passer)

    # handler has been overwritten by the claim label ("env-personal"); task_kind survives.
    out = ex.execute({"id": "pass-3", "text": "autopilot pass",
                      "task_kind": "autopilot", "handler": "env-personal"})
    assert out.status == "done"
    assert provider.plan_calls == 0                    # never ran the normal deep path
    assert len(autopilot_client.created_tasks) == 1    # the pass really ran


def test_executor_still_routes_a_legacy_handler_only_pass_task():
    """Back-compat: a pass task queued BEFORE the backend gained task_kind still routes."""
    from quest_ai_runner.runner.executor import TaskExecutor

    from .conftest import StubProvider
    from .test_runner import MockQuestClient, _brain

    q1 = _quest("q1")
    autopilot_client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    passer = AutopilotPass(autopilot_client, team_id="team1", now=_now)
    ex = TaskExecutor(MockQuestClient([]), _brain(StubProvider(decisions=[])),
                      autopilot_pass=passer)
    out = ex.execute({"id": "pass-4", "text": "autopilot pass", "handler": "autopilot"})
    assert out.status == "done"
    assert len(autopilot_client.created_tasks) == 1


def test_autopilot_created_work_is_never_routed_back_into_another_pass():
    """The loop guard. Work batches carry task_kind="autopilot_work", NOT the pass's "autopilot"
    kind -- otherwise the executor would route each created task into another autopilot pass,
    which would create more tasks, forever."""
    from quest_ai_runner.runner.executor import _is_autopilot_pass
    from quest_ai_runner.runner.autopilot import AUTOPILOT_WORK_KIND

    assert _is_autopilot_pass({"task_kind": "autopilot"}) is True
    assert _is_autopilot_pass({"task_kind": AUTOPILOT_WORK_KIND}) is False


def test_executor_ordinary_handler_value_is_not_routed_to_autopilot():
    """A normal rep-label handler (e.g. stamped by the poller's claim()) must NOT be mistaken for
    the autopilot route — only the exact value "autopilot" triggers it. Proof: with NO
    autopilot_pass wired, a misrouted task would report failed ("autopilot is not configured");
    instead it completes normally through the answer path."""
    from quest_ai_runner.runner.executor import TaskExecutor

    from .conftest import StubProvider
    from .test_runner import MockQuestClient, _brain

    provider = StubProvider(decisions=[{"action": "answer", "rationale": "ok"}])
    client = MockQuestClient([])
    ex = TaskExecutor(client, _brain(provider))  # no autopilot_pass wired

    out = ex.execute({"id": "t1", "text": "say hi", "handler": "alex"})
    assert out.status == "done"
    assert client.reports[0][1] == "done"


# --- the quest's canonical next-steps artifact --------------------------------------------------
#
# One answer to "what is next for this quest", written by whoever last refreshed it and read by
# everyone else. Before this, a pass and an attended session each reconstructed their own from
# whatever context happened to surface, and neither could tell it had drifted from the other.

class _ContextEntryClient(FakeAutopilotClient):
    """Adds the quest context-entry surface (the one Quest object that can be REPLACED in place)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries = []
        self.notes = []

    def list_context_entries(self, quest_id):
        return [{"id": e["id"], "name": e["name"]} for e in self.entries]

    def create_context_entry(self, quest_id, name, content):
        entry = {"id": f"entry_{len(self.entries) + 1}", "name": name, "content": content}
        self.entries.append(entry)
        return dict(entry)

    def update_context_entry(self, quest_id, entry_id, *, name=None, content=None):
        for e in self.entries:
            if e["id"] == entry_id:
                if content is not None:
                    e["content"] = content
                return dict(e)
        return {}

    def add_quest_note(self, quest_id, text):
        self.notes.append((quest_id, text))
        return [{"note_id": "note_1", "text": text}]


def test_a_pass_writes_its_conclusion_as_the_quests_next_steps(tmp_path):
    from quest_ai_runner.runner.quest_folder_sync import NEXT_STEPS_ENTRY_NAME, read_next_steps

    client = _ContextEntryClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(
            ("day", "2026-07-12", [_goal("g1", name="Draft chapter 2", deadline="2026-07-20")]))})
    result = AutopilotPass(client, team_id="team1", now=_now,
                           quest_folder_map={"q1": str(tmp_path)}).run({"text": "pass"})
    body = read_next_steps(str(tmp_path))
    assert "Draft chapter 2 (target 2026-07-20)" in body
    assert result.next_steps_refreshed == [
        {"quest_id": "q1", "path": str(tmp_path / "QUEST_SYNC.md"),
         "quest_target": "context_entry", "quest_label": "ship the thing"}]
    # And on Quest as ONE upserted entry, not another timestamped note.
    assert [e["name"] for e in client.entries] == [NEXT_STEPS_ENTRY_NAME]
    assert client.notes == []


def test_a_second_pass_replaces_the_artifact_instead_of_adding_another(tmp_path):
    client = _ContextEntryClient(
        quests=[_quest("q1", cadence="daily")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1", name="First")]))})
    passer = AutopilotPass(client, team_id="team1", now=_now,
                           quest_folder_map={"q1": str(tmp_path)})
    passer.run({"text": "pass"})
    client.tasks = []            # clear backpressure so the next pass runs
    client.goals_by_quest["q1"] = _goals_payload(
        ("day", "2026-07-12", [_goal("g2", name="Second")]))
    passer.run({"text": "pass"})
    content = (tmp_path / "QUEST_SYNC.md").read_text()
    assert "Second" in content and "First" not in content
    assert len(client.entries) == 1
    assert "Second" in client.entries[0]["content"]


def test_the_batch_reads_the_standing_artifact_as_the_plan_of_record(tmp_path):
    """An attended session may have refreshed it since the last pass, so the pass must work from
    the same answer rather than its own reconstruction."""
    from quest_ai_runner.runner.quest_folder_sync import NextSteps, write_next_steps

    write_next_steps(str(tmp_path), "q1", NextSteps(
        steps=["Send the revised draft to the reader who is waiting on it"],
        source="an attended session", updated="2026-07-11"))
    client = _ContextEntryClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    AutopilotPass(client, team_id="team1", now=_now,
                  quest_folder_map={"q1": str(tmp_path)}).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Send the revised draft to the reader who is waiting on it" in text
    assert "an attended session" in text
    assert "plan of record" in text


def test_a_quest_with_no_mapped_folder_is_untouched(tmp_path):
    client = _ContextEntryClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", now=_now,
                           quest_folder_map={"other_quest": str(tmp_path)}).run({"text": "pass"})
    assert len(result.created_task_ids) == 1                  # the pass itself is unaffected
    assert result.next_steps_refreshed == []
    assert client.entries == []
    assert not (tmp_path / "QUEST_SYNC.md").exists()


def test_a_dry_run_does_not_touch_the_artifact(tmp_path):
    client = _ContextEntryClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    AutopilotPass(client, team_id="team1", now=_now,
                  quest_folder_map={"q1": str(tmp_path)}).run({"text": "dry-run pass"})
    assert not (tmp_path / "QUEST_SYNC.md").exists()
    assert client.entries == []


def test_a_pass_that_produced_nothing_leaves_the_existing_artifact_alone(tmp_path):
    """Overwriting a considered answer on a day the quest was gated would make the artifact less
    trustworthy than the guesswork it replaces. The gate here is the cadence: a weekly quest that
    already ran today does no work, and must not rewrite its own conclusion either."""
    from quest_ai_runner.runner.quest_folder_sync import (NextSteps, read_next_steps,
                                                          write_next_steps)

    write_next_steps(str(tmp_path), "q1", NextSteps(steps=["The considered answer"]))
    client = _ContextEntryClient(
        quests=[_quest("q1", last_pass_at="2026-07-12T06:00:00Z")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", now=_now,
                           quest_folder_map={"q1": str(tmp_path)}).run({"text": "pass"})
    assert result.created_task_ids == []
    assert "The considered answer" in read_next_steps(str(tmp_path))
    assert result.next_steps_refreshed == []


def test_a_failed_artifact_refresh_warns_loudly_and_never_fails_the_pass(tmp_path):
    class _BoomClient(_ContextEntryClient):
        def list_context_entries(self, quest_id):
            raise RuntimeError("entries endpoint down")

    client = _BoomClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", now=_now,
                           quest_folder_map={"q1": str(tmp_path)}).run({"text": "pass"})
    assert len(result.created_task_ids) == 1                  # the work still landed
    assert (tmp_path / "QUEST_SYNC.md").exists()               # and so did the local artifact
    assert any("next-steps artifact" in w["detail"] for w in result.bookkeeping_warnings)
    assert "next-steps artifact" in result.summary_text()


# --- "Run now": a pending request satisfies the cadence gate -------------------------------------

def test_run_requested_is_pending_only_while_newer_than_the_last_pass():
    """The request is spent by the pass that answers it -- there is no separate clear.

    Which is exactly why "newer than last_pass_at" is the test and a bare boolean flag is not: a
    flag needs someone to reset it, and whoever forgets leaves the quest either passing forever or
    silently ignoring the button.
    """
    assert run_requested({}) is False
    assert run_requested({"run_requested_at": "2026-07-12T09:00:00Z"}) is True   # never run
    assert run_requested({"run_requested_at": "2026-07-12T09:00:00Z",
                          "last_pass_at": "2026-07-12T08:00:00Z"}) is True       # after the pass
    assert run_requested({"run_requested_at": "2026-07-12T08:00:00Z",
                          "last_pass_at": "2026-07-12T09:00:00Z"}) is False      # already answered
    # A corrupt request stamp is not pending: it must not wedge the quest into passing forever.
    assert run_requested({"run_requested_at": "not-a-date"}) is False
    # A corrupt last_pass_at leaves a REAL request pending -- fail toward what the user asked for.
    assert run_requested({"run_requested_at": "2026-07-12T09:00:00Z",
                          "last_pass_at": "not-a-date"}) is True


def test_a_pending_run_request_runs_the_quest_the_cadence_gate_would_have_skipped():
    """Same quest and same instant as ``test_gate_skips_quest_whose_cadence_is_not_due``, with a
    request added. Anything else would make "Run now" a button that reports success and does
    nothing on the day someone is most likely to press it."""
    q1 = _quest("q1", last_pass_at="2026-07-11T09:00:00Z",
                run_requested_at="2026-07-12T08:59:00Z")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert result.skipped == []
    assert len(result.created_task_ids) == 1


def test_a_run_request_does_not_override_mode_off():
    """Mode is the outer gate: a quest that is not opted in is never reached by a request."""
    q1 = _quest("q1", mode="off", run_requested_at="2026-07-12T08:59:00Z")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert result.created_task_ids == []
