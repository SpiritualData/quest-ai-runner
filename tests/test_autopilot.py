"""Autopilot — gates, scope targeting, persona batching, suggest/act status, dry-run.

Against a fake Quest client (no network), driving ``runner.autopilot.AutopilotPass`` and its
pure helper functions directly. See ``quest_autopilot_design.md`` (Part B) for the algorithm this
proves, and the qar-playbook invariant: every skipped quest must be logged with its gate reason
(``AutopilotResult.skipped``) — a silent skip is exactly the failure mode banned there.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.autopilot import (
    DEFAULT_TEAM_DAILY_BUDGET,
    AutopilotPass,
    batch_by_persona,
    cadence_due,
    compose_batch_text,
    resolve_persona,
    select_target_goals,
)

NOW = datetime(2026, 7, 12, 9, 0, 0, tzinfo=timezone.utc)  # a Sunday


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
      * ``create_task`` accepts NO ``status`` (always created queued) and no persona field;
        ``suggested`` is reached by a follow-up ``update_task`` PATCH.
      * ``update_quest_autopilot`` echoes back only the fields its schema ACCEPTS
        (mode/planning/cadence/personas/env_id) -- ``last_pass_at``/``miss_streak`` are silently
        dropped, exactly as the real endpoint does today.
    """

    # Mirrors the real UpdateAutopilotRequest schema (app/api/endpoints/quests/autopilot.py).
    ACCEPTED_AUTOPILOT_FIELDS = {"mode", "planning", "cadence", "personas", "env_id"}

    def __init__(self, quests=None, goals_by_quest=None, tasks=None,
                 accepts_bookkeeping=False):
        self.quests = list(quests or [])            # full quest states, keyed for get_quest_autopilot
        self.goals_by_quest = dict(goals_by_quest or {})
        self.tasks = list(tasks or [])
        self.created_tasks = []
        self.task_updates = []        # (task_id, fields) -- the PATCHes
        self.autopilot_updates = []   # (quest_id, fields)
        self.open_decisions = {}      # quest_id -> bool
        self.goal_docs = {}           # goal_id -> full goal doc (with description)
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
        assert "status" not in kwargs, "the real create route accepts no status field"
        assert "quest_id" not in kwargs, "the real create route has no quest_id field"
        assert "rep_id" not in kwargs, "the real create route has no persona/rep field"
        task_id = f"autotask_{len(self.created_tasks) + 1}"
        record = {"id": task_id, "text": text, "status": "queued", **kwargs}
        self.created_tasks.append(record)
        return {"id": task_id}

    def update_task(self, task_id, fields):
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
          env_id=None, personas=None, outcome="ship the thing", miss_streak=0):
    autopilot = {"mode": mode, "cadence": cadence, "planning": planning,
                "miss_streak": miss_streak}
    if last_pass_at is not None:
        autopilot["last_pass_at"] = last_pass_at
    if env_id is not None:
        autopilot["env_id"] = env_id
    if personas is not None:
        autopilot["personas"] = personas
    return {"quest_id": quest_id, "outcome": outcome, "autopilot": autopilot}


def _goal(goal_id, name="A goal", *, completed=False, ai_help=True,
         assignee_rep_id=None, deadline=None):
    """A goal AS THE GROUPING ENDPOINT RETURNS IT: note there is NO ``description`` key (the real
    handler omits it), which is why the pass enriches target goals via ``get_goal``."""
    g = {"id": goal_id, "name": name, "completed": completed, "ai_help": ai_help}
    if assignee_rep_id is not None:
        g["assignee_rep_id"] = assignee_rep_id
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


def test_cadence_due_false_within_weekly_window():
    autopilot_cfg = {"cadence": "weekly", "last_pass_at": "2026-07-10T09:00:00Z"}  # 2 days ago
    assert cadence_due(autopilot_cfg, NOW) is False


def test_cadence_due_true_after_weekly_window_elapses():
    autopilot_cfg = {"cadence": "weekly", "last_pass_at": "2026-07-01T09:00:00Z"}  # 11 days ago
    assert cadence_due(autopilot_cfg, NOW) is True


def test_cadence_due_daily_and_monthly_windows():
    assert cadence_due({"cadence": "daily", "last_pass_at": "2026-07-11T09:00:00Z"}, NOW) is True
    assert cadence_due({"cadence": "daily", "last_pass_at": "2026-07-12T08:00:00Z"}, NOW) is False
    assert cadence_due({"cadence": "monthly", "last_pass_at": "2026-06-20T09:00:00Z"}, NOW) is False
    assert cadence_due({"cadence": "monthly", "last_pass_at": "2026-05-01T09:00:00Z"}, NOW) is True


def test_cadence_unparsable_timestamp_fails_open_to_due():
    assert cadence_due({"cadence": "weekly", "last_pass_at": "not-a-date"}, NOW) is True


def test_gate_skips_quest_whose_cadence_is_not_due():
    q1 = _quest("q1", last_pass_at="2026-07-11T09:00:00Z")  # 1 day ago, weekly cadence
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert result.skipped == [{"quest_id": "q1", "reason": "cadence not due yet"}]


# --- gate: backpressure ---------------------------------------------------------------------

def test_gate_skips_quest_with_open_autopilot_task_still_pending():
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    existing = [{"id": "prev", "task_kind": "autopilot_work", "goal_id": "q1",
                "status": "needs_you"}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, tasks=existing)
    passer = AutopilotPass(client, team_id="team1", now=_now)
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

def test_gate_skips_quest_with_open_hold_decision():
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    client.open_decisions["q1"] = True
    passer = AutopilotPass(client, team_id="team1", now=_now)
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
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.skipped == [{"quest_id": "q1", "reason": "cadence not due yet"}]


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


# --- scope targeting ----------------------------------------------------------------------------

def test_scope_targets_todays_day_group_over_other_scopes():
    payload = _goals_payload(
        ("day", "2026-07-12", [_goal("today1"), _goal("today2")]),
        ("week", "2026_W28", [_goal("week1")]),
    )
    goals, label = select_target_goals(payload, NOW)
    assert [g["id"] for g in goals] == ["today1", "today2"]
    assert label == "day:2026-07-12"


def test_scope_falls_back_to_current_week_when_no_day_group_is_current():
    payload = _goals_payload(
        ("day", "2026-07-01", [_goal("stale_day")]),   # not today
        ("week", "2026_W28", [_goal("week1"), _goal("week2")]),
    )
    goals, label = select_target_goals(payload, NOW)
    assert [g["id"] for g in goals] == ["week1", "week2"]
    assert label == "week:2026_W28"


def test_scope_unscoped_falls_back_to_single_next_incomplete_goal():
    payload = _goals_payload(
        ("custom", "backlog", [
            _goal("done1", completed=True),
            _goal("no_ai_help", ai_help=False),
            _goal("next_up"),
            _goal("later_one"),
        ]),
    )
    goals, label = select_target_goals(payload, NOW)
    assert [g["id"] for g in goals] == ["next_up"]
    assert label == "unscoped"


def test_scope_ai_help_filtering_excludes_unflagged_and_completed_goals():
    payload = _goals_payload(
        ("day", "2026-07-12", [
            _goal("flagged", ai_help=True),
            _goal("unflagged", ai_help=False),
            _goal("done", ai_help=True, completed=True),
        ]),
    )
    goals, _label = select_target_goals(payload, NOW)
    assert [g["id"] for g in goals] == ["flagged"]


def test_scope_no_eligible_goals_in_current_scope_returns_empty_but_keeps_the_scope_label():
    """A CURRENT scope group was found (today), it just has nothing eligible in it -- the quest
    goes quiet for this pass rather than falling through to the unscoped next-goal fallback."""
    payload = _goals_payload(("day", "2026-07-12", [_goal("g", ai_help=False)]))
    goals, label = select_target_goals(payload, NOW)
    assert goals == []
    assert label == "day:2026-07-12"


def test_scope_no_groups_at_all_is_unscoped_with_nothing_eligible():
    goals, label = select_target_goals({"period_groups": []}, NOW)
    assert goals == []
    assert label == "unscoped"


# --- persona resolution + batching --------------------------------------------------------------

def test_persona_resolves_goal_assignee_first():
    goal = _goal("g1", assignee_rep_id="rep_bailey")
    autopilot_cfg = {"personas": [{"rep_id": "rep_other"}]}
    assert resolve_persona(goal, autopilot_cfg, NOW) == "rep_bailey"


def test_persona_resolves_quest_persona_matching_today_over_unrestricted():
    goal = _goal("g1")
    # NOW is a Sunday -> "%a" gives "Sun".
    autopilot_cfg = {"personas": [
        {"rep_id": "rep_unrestricted"},
        {"rep_id": "rep_sunday", "days": ["Sun"]},
    ]}
    assert resolve_persona(goal, autopilot_cfg, NOW) == "rep_sunday"


def test_persona_falls_back_to_unrestricted_quest_persona():
    goal = _goal("g1")
    autopilot_cfg = {"personas": [{"rep_id": "rep_mon", "days": ["Mon"]},
                                  {"rep_id": "rep_any"}]}
    assert resolve_persona(goal, autopilot_cfg, NOW) == "rep_any"


def test_persona_uses_fallback_resolver_when_nothing_else_matches():
    goal = _goal("g1")
    resolved = resolve_persona(goal, {}, NOW, fallback_resolver=lambda g: "rep_from_cards")
    assert resolved == "rep_from_cards"


def test_persona_none_when_no_resolver_matches():
    assert resolve_persona(_goal("g1"), {}, NOW) is None


def test_batching_groups_same_persona_goals_into_one_batch():
    goals = [_goal("g1", assignee_rep_id="rep_a"), _goal("g2", assignee_rep_id="rep_a")]
    batches = batch_by_persona(goals, {}, NOW)
    assert len(batches) == 1
    persona, batch_goals = batches[0]
    assert persona == "rep_a"
    assert [g["id"] for g in batch_goals] == ["g1", "g2"]


def test_batching_splits_different_personas_into_separate_batches():
    goals = [_goal("g1", assignee_rep_id="rep_a"), _goal("g2", assignee_rep_id="rep_b")]
    batches = batch_by_persona(goals, {}, NOW)
    assert len(batches) == 2
    personas = {p for p, _g in batches}
    assert personas == {"rep_a", "rep_b"}


def test_batching_groups_unassigned_goals_under_the_plain_assistant():
    goals = [_goal("g1"), _goal("g2")]
    batches = batch_by_persona(goals, {}, NOW)
    assert len(batches) == 1
    persona, batch_goals = batches[0]
    assert persona is None
    assert len(batch_goals) == 2


def test_batch_of_two_same_persona_goals_creates_exactly_one_task():
    q1 = _quest("q1")
    goals_payload = _goals_payload(("day", "2026-07-12", [
        _goal("g1", assignee_rep_id="rep_a"), _goal("g2", assignee_rep_id="rep_a"),
    ]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    # The persona rides in the TEXT (the create route has no persona field), and it is what
    # decided that these two goals share ONE task.
    assert "Act as rep_a" in created["text"]
    assert created["text"].count("Goal: ") == 2
    assert "Goal: A goal" in created["text"]


def test_batch_of_two_different_persona_goals_creates_two_tasks():
    q1 = _quest("q1")
    goals_payload = _goals_payload(("day", "2026-07-12", [
        _goal("g1", assignee_rep_id="rep_a"), _goal("g2", assignee_rep_id="rep_b"),
    ]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", daily_budget=5, now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 2
    assert any("Act as rep_a" in t["text"] for t in client.created_tasks)
    assert any("Act as rep_b" in t["text"] for t in client.created_tasks)


# --- suggest vs act status + env_id stamping -----------------------------------------------------

def test_suggest_mode_lands_the_task_in_suggested_via_a_follow_up_patch():
    """The create route has NO status field (every task is created queued), so suggest mode must
    create-then-PATCH. Proving the PATCH is what makes the task non-runnable is the point: without
    it, a suggestion the human never approved would sit queued and EXECUTE."""
    q1 = _quest("q1", mode="suggest")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert client.task_updates == [("autotask_1", {"status": "suggested"})]
    assert client.created_tasks[0]["status"] == "suggested"


def test_act_mode_leaves_the_task_queued_with_no_status_patch():
    q1 = _quest("q1", mode="act")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert client.task_updates == []                       # nothing to demote: queued is correct
    assert client.created_tasks[0]["status"] == "queued"


def test_created_task_stamps_env_id_task_kind_and_the_primary_goal_as_its_quest_link():
    q1 = _quest("q1", env_id="joshua-personal")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    created = client.created_tasks[0]
    assert created["env_id"] == "joshua-personal"
    assert created["goal_id"] == "g1"          # goal_id IS the task's quest/goal link
    # The autopilot-authored marker is the PERSISTENT task_kind, and it must be the WORK kind,
    # never the pass kind (which the executor would route into another autopilot pass: a loop).
    assert created["task_kind"] == "autopilot_work"
    from quest_ai_runner.runner.autopilot import AUTOPILOT_PASS_KIND
    assert created["task_kind"] != AUTOPILOT_PASS_KIND
    # source must be a value the API's closed enum accepts, or the create 400s.
    assert created["source"] in ("chat", "reflection", "review")


# --- planning: plan_and_work proposes a next goal when nothing is eligible ---------------------

def test_plan_and_work_proposes_next_goal_when_no_eligible_goal():
    q1 = _quest("q1", planning="plan_and_work")
    goals_payload = _goals_payload(("day", "2026-07-12", []))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    created = client.created_tasks[0]
    assert created["text"].startswith("Proposed goal:")
    # A proposal is ALWAYS surfaced for a human, even on an `act` quest -> demoted to suggested.
    assert client.task_updates == [("autotask_1", {"status": "suggested"})]
    assert created["status"] == "suggested"
    # With no real goal object created, the proposal still lands on the QUEST (goal_id = quest id).
    assert created["goal_id"] == "q1"


def test_act_mode_goal_proposal_is_still_only_suggested():
    q1 = _quest("q1", mode="act", planning="plan_and_work")
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", []))})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert client.created_tasks[0]["status"] == "suggested"


def test_work_only_planning_goes_quiet_when_nothing_eligible():
    q1 = _quest("q1", planning="work_only")
    goals_payload = _goals_payload(("day", "2026-07-12", []))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert client.created_tasks == []
    # Bookkeeping still records the pass (a miss), it just creates nothing.
    assert client.autopilot_updates == [("q1", {"last_pass_at": "2026-07-12T09:00:00Z",
                                                "miss_streak": 1})]


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
    goals_payload = _goals_payload(("day", "2026-07-12", []))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "please dry-run this"})
    assert client.created_tasks == []
    assert len(result.proposals) == 1
    assert result.proposals[0]["kind"] == "goal_proposal"
    assert "outcome" in result.proposals[0]["description"] or result.proposals[0]["title"]


# --- compose_batch_text: quest outcome + goal name/description + Definition of Done -------------

def test_compose_batch_text_includes_outcome_goal_and_dod():
    goals = [{"id": "g1", "name": "Draft the plan", "description": "write section 2",
              "deadline": "2026-08-01"}]
    text = compose_batch_text("Ship the launch", goals)
    assert "Quest outcome: Ship the launch" in text
    assert "Goal: Draft the plan" in text
    assert "Brief: write section 2" in text
    assert "Definition of Done" in text
    assert "2026-08-01" in text


def test_compose_batch_text_names_the_persona_when_one_resolved():
    text = compose_batch_text("Ship it", [{"id": "g1", "name": "A goal"}], "bailey")
    assert "Act as bailey" in text


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


def test_goal_description_is_fetched_because_the_grouping_payload_omits_it():
    """The grouping endpoint returns a slim goal dict with NO description, but the description IS
    the AI's brief. The pass must enrich target goals via get_goal or every task would ship
    briefless."""
    q1 = _quest("q1")
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    client.goal_docs["g1"] = {"id": "g1", "name": "A goal",
                              "description": "the real brief from the goal doc"}
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert "Brief: the real brief from the goal doc" in client.created_tasks[0]["text"]


def test_missing_goal_description_still_creates_the_task_without_a_brief():
    q1 = _quest("q1")
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    # No goal_docs entry: get_goal returns {} -> no description available.
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert "Brief:" not in client.created_tasks[0]["text"]
    assert "Definition of Done" in client.created_tasks[0]["text"]


def test_backpressure_matches_a_task_by_goal_id_because_tasks_have_no_quest_id():
    """A task's link to its quest IS its goal_id (the API resolves that field as a quest id);
    there is no quest_id field on a task at all."""
    q1 = _quest("q1")
    existing = [{"id": "prev", "task_kind": "autopilot_work", "goal_id": "q1",
                "status": "in_progress"}]
    client = FakeAutopilotClient(
        quests=[q1], tasks=existing,
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    passer = AutopilotPass(client, team_id="team1", now=_now)
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


def test_current_month_group_is_targeted_with_the_real_underscore_period():
    q1 = _quest("q1")
    client = FakeAutopilotClient(
        quests=[q1],
        goals_by_quest={"q1": _goals_payload(("month", "2026_07", [_goal("m1"), _goal("m2")]))})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1                 # both month goals, one persona-less batch
    assert client.created_tasks[0]["text"].count("Goal: ") == 2


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


def test_no_opted_in_quests_reports_clean_empty_summary():
    client = FakeAutopilotClient(quests=[])
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert result.created_task_ids == []
    assert "No opted-in quests found." in result.summary_text()


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

    # handler has been overwritten by the claim label ("joshua-personal"); task_kind survives.
    out = ex.execute({"id": "pass-3", "text": "autopilot pass",
                      "task_kind": "autopilot", "handler": "joshua-personal"})
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
