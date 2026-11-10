"""Routines: a named single-purpose autopilot beside the quest's ordinary one (autopilot_routines.py, autopilot_purpose_learning.py).

What these tests pin, all against fakes (no network, no model):
  * the gate: a pass with nothing due is refused, creates no task, and spends the occasion with one bookkeeping stamp;
  * the schedule: due once per occasion on its own day and hour (the hub's Proactivity), not on other days, not again after its stamp;
  * the pass does only its purpose: one work task and its own ``routine_runs`` stamp, never a goal or task read or write;
  * the work text carries the note content and the decision-request path, bounds the work, and forbids em dashes;
  * a second autopilot on the same quest (the ordinary pass, another routine) neither runs nor blocks the routine, and vice versa.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

from quest_ai_runner.the_intelligence.proactivity.autopilot import AutopilotPass
from quest_ai_runner.the_intelligence.proactivity.autopilot_purpose_learning import learning_purpose
from quest_ai_runner.the_intelligence.proactivity.autopilot_routines import RoutinePass, RoutinePurpose, compose_routine_text
from quest_ai_runner.the_intelligence.proactivity.poller_routines import RoutinesMixin

from .test_autopilot import FakeAutopilotClient, _quest

SUNDAY_0630_PT = datetime(2026, 11, 15, 14, 30, tzinfo=timezone.utc)
SUNDAY_0500_PT = datetime(2026, 11, 15, 13, 0, tzinfo=timezone.utc)
MONDAY_0630_PT = datetime(2026, 11, 16, 14, 30, tzinfo=timezone.utc)
NEXT_SUNDAY_0630_PT = datetime(2026, 11, 22, 14, 30, tzinfo=timezone.utc)

LEARNING = {"name": "learning", "purpose": "learning", "instructions": "Review what is due in the hub repository and record it.",
            "days": ["Sun"], "run_time": "06:00", "run_timezone": "America/Los_Angeles", "enabled": True}
DUE = [{"id": "the-planner", "what": "the-planner: last researched 120 days ago"}, {"id": "the-reader", "what": "the-reader: no research recorded"}]


class MergingClient(FakeAutopilotClient):
    """The fixed backend: ``routine_runs`` merges key by key, everything else is a plain write."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def update_quest_autopilot(self, quest_id, fields):
        self.autopilot_updates.append((quest_id, dict(fields)))
        current = self._autopilot_state.setdefault(quest_id, {})
        for key, value in fields.items():
            current[key] = {**current.get(key, {}), **value} if key == "routine_runs" else value
        return {"quest_id": quest_id, "autopilot": dict(current)}


def quest_with(routines, **kwargs):
    quest = _quest("q1", **kwargs)
    quest["autopilot"]["routines"] = routines
    quest["autopilot"]["run_timezone"] = "America/Los_Angeles"
    return quest


def routine_pass(client, now, items=DUE):
    asked = []
    purposes = {"learning": learning_purpose(lambda day: asked.append(day) or items)}
    return RoutinePass(client, team_id="team_1", lane_user_id="lane_user", purposes=purposes, now=lambda: now), asked


def test_a_due_routine_creates_one_work_task_and_nothing_else():
    client = MergingClient([quest_with([LEARNING], mode="act")], goals_by_quest={"q1": {"week": [{"goal_id": "g1", "title": "A goal"}]}})
    runner, asked = routine_pass(client, SUNDAY_0630_PT)
    outcomes = runner.run_quest("q1")
    assert [(o.routine, o.status) for o in outcomes] == [("learning", "created")]
    assert asked == ["2026-11-15"]
    # Only the purpose's own work: one task on the quest, one stamp of its own bookkeeping.
    assert len(client.created_tasks) == 1
    task = client.created_tasks[0]
    assert task["goal_id"] == "q1" and task["status"] == "queued" and task["task_kind"] == "autopilot_work"
    assert task["assignee_user_id"] == "lane_user" and task["title"] == "Routine: learning"
    assert client.autopilot_updates == [("q1", {"routine_runs": {"learning": "2026-11-15T14:30:00Z"}})]
    # No goal read or write, no task update, no persona routing.
    assert client.goal_list_calls == [] and client.task_updates == [] and "assignee_rep_id" not in task


def test_nothing_due_refuses_the_pass_and_spends_the_occasion():
    client = MergingClient([quest_with([LEARNING])])
    runner, asked = routine_pass(client, SUNDAY_0630_PT, items=[])
    (outcome,) = runner.run_quest("q1")
    assert (outcome.status, outcome.reason) == ("refused", "nothing is due for learning")
    assert client.created_tasks == []
    assert client.autopilot_updates == [("q1", {"routine_runs": {"learning": "2026-11-15T14:30:00Z"}})]
    # Spent: the same occasion is neither asked nor run again by this process or by the next read of the stamp.
    assert runner.run_quest("q1") == [] and asked == ["2026-11-15"]
    fresh, fresh_asked = routine_pass(client, SUNDAY_0630_PT, items=DUE)
    assert fresh.run_quest("q1") == [] and fresh_asked == [] and client.created_tasks == []


def test_the_routine_is_due_only_on_its_own_day_and_hour_and_once_a_week():
    client = MergingClient([quest_with([LEARNING])])
    for early_or_wrong_day in (SUNDAY_0500_PT, MONDAY_0630_PT):
        runner, asked = routine_pass(client, early_or_wrong_day)
        assert runner.run_quest("q1") == [] and asked == []
    runner, _ = routine_pass(client, SUNDAY_0630_PT)
    assert [o.status for o in runner.run_quest("q1")] == ["created"]
    # A new process on the same Sunday reads the stamp and does nothing; the next Sunday runs again.
    again, _ = routine_pass(client, SUNDAY_0630_PT)
    assert again.run_quest("q1") == []
    next_week, _ = routine_pass(client, NEXT_SUNDAY_0630_PT)
    assert [o.status for o in next_week.run_quest("q1")] == ["created"]


def test_the_gate_skips_off_quests_disabled_routines_and_unknown_purposes():
    off = MergingClient([quest_with([LEARNING], mode="off")])
    assert [o.status for o in routine_pass(off, SUNDAY_0630_PT)[0].run_quest("q1")] == ["skipped"]
    disabled = MergingClient([quest_with([{**LEARNING, "enabled": False}])])
    assert [o.reason for o in routine_pass(disabled, SUNDAY_0630_PT)[0].run_quest("q1")] == ["the routine is disabled"]
    unknown = MergingClient([quest_with([{**LEARNING, "purpose": "gardening"}])])
    (outcome,) = routine_pass(unknown, SUNDAY_0630_PT)[0].run_quest("q1")
    assert outcome.status == "skipped" and "gardening" in outcome.reason
    for client in (off, disabled, unknown):
        assert client.created_tasks == [] and client.autopilot_updates == []


def test_dry_run_reads_and_composes_but_writes_nothing_and_spends_nothing():
    client = MergingClient([quest_with([LEARNING])])
    runner, _ = routine_pass(client, SUNDAY_0630_PT)
    (outcome,) = runner.run_quest("q1", dry_run=True)
    assert outcome.status == "dry-run" and "DRY RUN" in outcome.text and len(outcome.items) == 2
    assert client.created_tasks == [] and client.autopilot_updates == []
    assert [o.status for o in runner.run_quest("q1")] == ["created"]
    # A supervised dry pass may look at a routine that is not due yet.
    early, _ = routine_pass(MergingClient([quest_with([LEARNING])]), MONDAY_0630_PT)
    assert early.run_quest("q1", dry_run=True) == []
    assert early.run_quest("q1", dry_run=True, ignore_schedule=True)[0].status == "dry-run"


def test_the_work_text_states_the_note_the_decision_path_and_the_bounds():
    quest = quest_with([LEARNING])
    text = compose_routine_text("Evaluate the runner", LEARNING, learning_purpose(lambda day: DUE), DUE)
    assert "Evaluate the runner" in text and "the-planner: last researched 120 days ago" in text
    assert LEARNING["instructions"] in text
    # The note: what was reviewed, what changed, adopted or rejected and why, what is due next week.
    assert "ONE short note" in text and "add_quest_note" in text
    for part in ("what was reviewed", "what changed in the registry", "adopted or rejected and why", "what is due next week"):
        assert part in text
    # The decision path: only for a choice a person must make.
    assert "create_decision" in text and "ONLY for a choice a person must make" in text and "licence" in text and "promoting an arm" in text
    # The bounds: one kind of work, only two Quest writes, repository rules, no em dashes.
    assert "ONE kind of work and nothing else" in text
    assert "planning, creating or editing goals, creating or updating tasks" in text
    assert "only Quest writes you may make are: add_quest_note, create_decision" in text
    assert "never push, never deploy" in text and "by explicit path" in text
    assert "Write no em dashes" in text and "—" not in text
    assert quest["autopilot"]["routines"][0]["name"] == "learning"


def test_suggest_mode_leaves_the_routine_task_for_approval():
    client = MergingClient([quest_with([LEARNING], mode="suggest")])
    routine_pass(client, SUNDAY_0630_PT)[0].run_quest("q1")
    assert client.created_tasks[0]["status"] == "suggested"


def test_a_second_routine_on_the_same_quest_runs_on_its_own_schedule():
    review = {**LEARNING, "name": "weekly-review", "purpose": "review", "days": ["Wed"], "run_time": "09:00"}
    client = MergingClient([quest_with([LEARNING, review])])
    purposes = {"learning": learning_purpose(lambda day: DUE), "review": RoutinePurpose("review", "review things.", "- be brief", lambda now: [{"id": "x", "what": "x"}])}
    sunday = RoutinePass(client, purposes=purposes, now=lambda: SUNDAY_0630_PT)
    assert [(o.routine, o.status) for o in sunday.run_quest("q1")] == [("learning", "created")]
    wednesday = datetime(2026, 11, 18, 17, 30, tzinfo=timezone.utc)
    midweek = RoutinePass(client, purposes=purposes, now=lambda: wednesday)
    assert [(o.routine, o.status) for o in midweek.run_quest("q1")] == [("weekly-review", "created")]
    assert [t["title"] for t in client.created_tasks] == ["Routine: learning", "Routine: weekly-review"]
    assert client._autopilot_state["q1"]["routine_runs"] == {"learning": "2026-11-15T14:30:00Z", "weekly-review": "2026-11-18T17:30:00Z"}


def test_the_ordinary_pass_on_the_same_quest_ignores_routines_and_keeps_its_own_bookkeeping():
    quest = quest_with([LEARNING], mode="act", cadence="daily", personas=[{"rep_id": "rep_worker"}])
    client = MergingClient([quest], goals_by_quest={"q1": {}})
    ordinary = AutopilotPass(client, team_id="team_1", now=lambda: SUNDAY_0630_PT)
    ordinary.run({"text": "pass", "goal_id": "q1"})
    # The ordinary pass never creates the routine's task, and its stamps never touch routine_runs.
    assert all(t.get("title") != "Routine: learning" for t in client.created_tasks)
    assert all("routine_runs" not in fields for _, fields in client.autopilot_updates)
    ordinary_tasks = len(client.created_tasks)
    runner, _ = routine_pass(client, SUNDAY_0630_PT)
    assert [o.status for o in runner.run_quest("q1")] == ["created"]
    assert len(client.created_tasks) == ordinary_tasks + 1
    # And the routine's stamp left the ordinary pass's last_pass_at alone.
    assert client._autopilot_state["q1"]["routine_runs"] == {"learning": "2026-11-15T14:30:00Z"}


class LaneStub(RoutinesMixin):
    def __init__(self, client, snapshot, now):
        self.cfg = SimpleNamespace(autopilot_ensure_pass_task=True, team_id="team_1", lane_user_id="lane_user")
        self.client, self._snapshot, self._now = client, snapshot, lambda: now
        self.client.configured = True

    def autopilot_team_for(self, quest_id):
        return "team_1"

    def _quest_schedule_snapshot(self):
        return self._snapshot


def snapshot_of(quest, routine_runs=None):
    cfg = quest["autopilot"]
    return {quest["quest_id"]: {"mode": cfg["mode"], "run_timezone": "America/Los_Angeles", "routines": cfg["routines"], "routine_runs": routine_runs or {}}}


def test_the_poller_asks_the_hub_only_for_quests_with_a_due_routine():
    quest = quest_with([LEARNING])
    client = MergingClient([quest])
    assert LaneStub(client, snapshot_of(quest), MONDAY_0630_PT).due_routine_names(snapshot_of(quest)) == {}
    sunday = LaneStub(client, snapshot_of(quest), SUNDAY_0630_PT)
    assert sunday.due_routine_names(snapshot_of(quest)) == {"q1": ["learning"]}
    stamped = snapshot_of(quest, {"learning": "2026-11-15T14:30:00Z"})
    assert sunday.due_routine_names(stamped) == {}
    quest_off = quest_with([LEARNING], mode="off")
    assert sunday.due_routine_names(snapshot_of(quest_off)) == {}


def test_the_poller_runs_a_due_routine_through_the_registered_learning_purpose(monkeypatch):
    quest = quest_with([LEARNING])
    client = MergingClient([quest])
    purposes = {"learning": learning_purpose(lambda day: DUE)}
    monkeypatch.setattr("quest_ai_runner.the_intelligence.proactivity.autopilot_routines.PURPOSES", purposes)
    lane = LaneStub(client, snapshot_of(quest), SUNDAY_0630_PT)
    lane.run_due_routines()
    assert [t["title"] for t in client.created_tasks] == ["Routine: learning"]
    lane.run_due_routines()
    assert len(client.created_tasks) == 1
