"""One autopilot run: the executor's plan for an ``autopilot_run`` task the backend scheduled.

Against a fake Quest client and a scripted judge (no network, no model). What these pin:
  * the run reads its autopilot by the task's ``autopilot_id`` FIELD and skips, with a reason, when it does not run (paused, past the plan's cap, gone);
  * goals follow scope: the day's one judgment gives each goal to ONE on-duty autopilot or to none, a rep's goal stays with an autopilot of that rep,
    the judgment is made once per quest per day, and a run with no goals and no prompt is skipped "no goals relevant to this scope";
  * suggest parks the composed brief, act runs it; a source run (learning) refuses when nothing is due;
  * the brief carries the quest's shared brief before the autopilot's own words;
  * only AUTOMATIC EXTRA runs are bounded (3 per autopilot a day, the team budget), backpressure is per autopilot.
"""
import json
from datetime import datetime, timezone

from quest_ai_runner.intelligence.proactivity.autopilot import AutopilotPass
from quest_ai_runner.intelligence.proactivity.autopilot_result import RUN_BRIEF_HEADER
from quest_ai_runner.intelligence.proactivity.autopilot_sources import WorkSource, register_source

NOW = datetime(2026, 7, 12, 9, 0, tzinfo=timezone.utc)      # a Sunday
SUN, MON = "Sun", "Mon"


def goal(goal_id, name, **extra):
    return {"id": goal_id, "name": name, "ai_help": True, "completed": False, **extra}


def goals_payload(*goals):
    return {"period_groups": [{"time_scope": "day", "period": "2026-07-12", "goals": list(goals)}]}


def ap(ap_id, name, **fields):
    return {"id": ap_id, "name": name, "purpose": f"{name} purpose", "instructions": "", "enabled": True, "mode": "act", "days": [], **fields}


class Client:
    def __init__(self, autopilots, goals=None, tasks=None, quest_fields=None):
        self.cfg = {"autopilots": autopilots, "instructions": "Quest-wide brief.", "run_timezone": "UTC", **(quest_fields or {})}
        self.goals = goals or {}
        self.tasks = tasks or []
        self.goal_reads = 0

    def get_quest_autopilot(self, quest_id):
        return {"outcome": "ship it", "autopilot": dict(self.cfg)}

    def list_quest_goals(self, quest_id, *, team_id=None):
        self.goal_reads += 1
        return self.goals

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, created_after=None, **kw):
        return [t for t in self.tasks if (status is None or t.get("status") == status) and (goal_id is None or t.get("goal_id") == goal_id)]

    def list_open_decisions_for_quest(self, quest_id):
        return []

    def list_asks(self, *, quest_id, limit=50):
        return []

    def __getattr__(self, name):
        raise AttributeError(name)


def scripted_judge(assignments, calls):
    def judge(prompt):
        calls.append(prompt)
        return json.dumps({"assignments": [{"goal": g, "autopilot": a, "reason": "fits"} for g, a in assignments.items()]})
    return judge


def routed(plan):
    """The goals block of the brief that names what this run was GIVEN to work (the frame, which lists every goal, is separate)."""
    return plan.text.split("Goals routed to you today", 1)[1].split("\n\n", 1)[0] if "Goals routed to you today" in plan.text else ""


def plan_for(client, ap_id, judge=None, task=None, **kw):
    runner = AutopilotPass(client, team_id="team1", now=lambda: NOW, judge=judge, **kw)
    return runner.run({"id": "run1", "goal_id": "q1", "task_kind": "autopilot_run", "autopilot_id": ap_id, "text": "run", **(task or {})})


def test_a_run_reads_its_autopilot_from_the_task_field_and_composes_one_brief():
    client = Client([ap("ap-a", "Weekly review", instructions="Write the weekly review.")], goals_payload(goal("g1", "Draft the review")))
    plan = plan_for(client, "ap-a", scripted_judge({"g1": "ap-a"}, []))
    assert plan.status == "act" and plan.title == "Weekly review"
    assert plan.text.startswith(RUN_BRIEF_HEADER)
    assert plan.text.index("Quest-wide brief.") < plan.text.index("Write the weekly review.")
    assert "Draft the review" in plan.text


def test_suggest_mode_parks_and_act_mode_runs():
    client = Client([ap("ap-s", "Suggester", mode="suggest", instructions="Do it."), ap("ap-a", "Actor", mode="act", instructions="Do it.")])
    assert plan_for(client, "ap-s").status == "suggested"
    assert plan_for(client, "ap-a").status == "act"


def test_an_autopilot_that_does_not_run_is_skipped_with_its_reason():
    client = Client([ap("ap-a", "A", instructions="x", paused_by_plan=True), ap("ap-b", "B", instructions="x", enabled=False)])
    for ap_id in ("ap-a", "ap-b", "ap-gone"):
        plan = plan_for(client, ap_id)
        assert plan.status == "skipped" and "does not run" in plan.reason
    paused = Client([ap("ap-a", "A", instructions="x")], quest_fields={"paused": True})
    assert plan_for(paused, "ap-a").status == "skipped"


def test_no_relevant_goals_and_no_prompt_is_a_skip_with_the_scope_reason():
    client = Client([ap("ap-a", "Empty")], goals_payload(goal("g1", "Unrelated goal")))
    plan = plan_for(client, "ap-a", scripted_judge({"g1": None}, []))
    assert (plan.status, plan.reason) == ("skipped", "no goals relevant to this scope")
    with_prompt = Client([ap("ap-a", "Prompted", instructions="Check the inbox.")], goals_payload(goal("g1", "Unrelated goal")))
    assert plan_for(with_prompt, "ap-a", scripted_judge({"g1": None}, [])).status == "act"


def test_a_goal_belongs_to_one_autopilot_and_is_judged_once_a_day():
    autopilots = [ap("ap-a", "Writer", instructions="Write."), ap("ap-b", "Researcher", instructions="Research.")]
    client = Client(autopilots, goals_payload(goal("g1", "Write chapter"), goal("g2", "Find sources"), goal("g3", "Fix plumbing")))
    calls = []
    judge = scripted_judge({"g1": "ap-a", "g2": "ap-b", "g3": None}, calls)
    runner = AutopilotPass(client, team_id="team1", now=lambda: NOW, judge=judge)     # one runner, as the lane has: the day's judgment is cached on it
    first, second = (runner.run({"id": f"run{i}", "goal_id": "q1", "task_kind": "autopilot_run", "autopilot_id": a, "text": "run"}) for i, a in enumerate(("ap-a", "ap-b")))
    assert "Write chapter" in routed(first) and "Find sources" not in routed(first)
    assert "Find sources" in routed(second) and "Write chapter" not in routed(second)
    assert "Fix plumbing" not in routed(first) + routed(second)    # assigned to nobody: left alone
    assert len(calls) == 1                                          # one judgment for the quest's day


def test_a_goal_the_judge_gives_to_an_autopilot_off_duty_today_is_ignored():
    autopilots = [ap("ap-a", "Weekday", instructions="x", days=[MON]), ap("ap-b", "Everyday", instructions="y")]
    client = Client(autopilots, goals_payload(goal("g1", "Some goal")))
    plan = plan_for(client, "ap-b", scripted_judge({"g1": "ap-a"}, []))        # a Sunday: ap-a is off duty
    assert routed(plan) == ""


def test_a_goal_a_rep_holds_stays_with_an_on_duty_autopilot_of_that_rep_without_a_judgment():
    autopilots = [ap("ap-a", "Plain", instructions="x"), ap("ap-r", "Rep's", instructions="y", rep_id="rep_1")]
    held = goal("g1", "Rep's goal", ai_handling="rep", ai_handling_rep_id="rep_1")
    client = Client(autopilots, goals_payload(held))
    calls = []
    assert "Rep's goal" in routed(plan_for(client, "ap-r", scripted_judge({}, calls)))
    assert "Rep's goal" not in routed(plan_for(client, "ap-a", scripted_judge({}, calls)))
    assert calls == []


def test_a_failed_judgment_leaves_the_goals_alone():
    client = Client([ap("ap-a", "A", instructions="x")], goals_payload(goal("g1", "Some goal")))
    def broken(prompt):
        raise RuntimeError("model down")
    assert routed(plan_for(client, "ap-a", broken)) == ""


def test_a_source_run_refuses_when_nothing_is_due_and_states_its_bounds_when_something_is():
    register_source(WorkSource("demo", "keep the demo current", "- Do only that.", lambda now: DUE))
    client = Client([ap("ap-l", "Learning", source="demo")])
    global DUE
    DUE = []
    assert plan_for(client, "ap-l").reason == "nothing is due for demo"
    DUE = [{"id": "x", "what": "refresh the thing"}]
    plan = plan_for(client, "ap-l")
    assert plan.status == "act" and "keep the demo current" in plan.text and "refresh the thing" in plan.text and "Out of bounds" in plan.text
    assert "Do not" not in plan.reason


DUE = []


def run_task(task_id, ap_id, key, status="done", created="2026-07-12T01:00:00Z", quest="q1"):
    return {"id": task_id, "goal_id": quest, "task_kind": "autopilot_run", "autopilot_id": ap_id, "occurrence_key": key, "status": status, "created_at": created}


def test_only_automatic_extra_runs_are_bounded():
    client = Client([ap("ap-a", "A", instructions="x"), ap("ap-b", "B", instructions="x")],
                    tasks=[run_task(f"t{i}", "ap-a", f"reply:n{i}") for i in range(3)] + [run_task("tb", "ap-b", "reply:nb")])
    extra = {"occurrence_key": "reply:n9"}
    assert "extra runs" in plan_for(client, "ap-a", task=extra, daily_budget=10).reason                 # 3 extras of ap-a already today
    assert plan_for(client, "ap-b", task=extra, daily_budget=10).status == "act"                        # ap-b has had 1
    assert plan_for(client, "ap-a", task={"occurrence_key": "q1:ap-a:2026-07-12T09:00"}).status == "act"      # a scheduled run is never bounded
    assert plan_for(client, "ap-a", task={"occurrence_key": "run:r1"}).status == "act"                       # nor is Run now


def test_the_team_budget_counts_extras_only():
    client = Client([ap("ap-a", "A", instructions="x")], tasks=[run_task("t1", "ap-x", "reply:1"), run_task("t2", "ap-y", "reply:2"), run_task("t3", "ap-z", "q1:ap-z:d")])
    assert "budget" in plan_for(client, "ap-a", task={"occurrence_key": "reply:3"}, daily_budget=2).reason
    assert plan_for(client, "ap-a", task={"occurrence_key": "reply:3"}, daily_budget=3).status == "act"


def test_backpressure_is_per_autopilot_and_ignores_the_run_itself():
    open_run = run_task("t9", "ap-a", "q1:ap-a:d", status="needs_you")
    client = Client([ap("ap-a", "A", instructions="x"), ap("ap-b", "B", instructions="x")], tasks=[open_run])
    assert "backpressure" in plan_for(client, "ap-a", backpressure=True).reason
    assert plan_for(client, "ap-b", backpressure=True).status == "act"
    assert plan_for(client, "ap-a", backpressure=False).status == "act"
    assert plan_for(client, "ap-a", backpressure=True, task={"id": "t9"}).status == "act"      # the open row IS this run


class RecordingClient(Client):
    """A backend that keeps the day's routing record and serves the autopilots from their own read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = {}

    def list_autopilots(self, quest_id):
        return {"autopilots": self.cfg["autopilots"], "shared": {k: v for k, v in self.cfg.items() if k != "autopilots"}}

    def get_autopilot_routing(self, quest_id, day):
        return {"routes": self.records.get((quest_id, day), [])}

    def put_autopilot_routing(self, quest_id, day, routes):
        self.records[(quest_id, day)] = routes


def test_the_days_routing_is_recorded_with_a_reason_and_read_back_instead_of_judged_again():
    autopilots = [ap("ap-a", "Writer", instructions="Write."), ap("ap-b", "Researcher", instructions="Research.")]
    client = RecordingClient(autopilots, goals_payload(goal("g1", "Write chapter"), goal("g2", "Fix plumbing")))
    calls = []
    first = plan_for(client, "ap-a", scripted_judge({"g1": "ap-a", "g2": None}, calls))
    record = client.records[("q1", "2026-07-12")]
    assert {r["goal_id"]: r["autopilot_id"] for r in record} == {"g1": "ap-a", "g2": None}      # each goal once, the ones left alone too
    assert all(set(r) == {"goal_id", "goal_title", "autopilot_id", "reason"} for r in record) and record[0]["reason"] == "fits"
    assert "Write chapter" in routed(first)
    again = plan_for(client, "ap-b", scripted_judge({}, calls))          # a restarted lane: a new runner reads the record
    assert len(calls) == 1 and routed(again) == ""


def test_a_scheduled_run_that_waited_past_its_grace_is_missed_but_run_now_and_extras_never_expire():
    client = Client([ap("ap-a", "A", instructions="x", grace_minutes=60)])
    late = {"created_at": "2026-07-12T07:00:00Z"}                            # queued two hours before NOW, grace is one
    assert plan_for(client, "ap-a", task={**late, "occurrence_key": "q1:ap-a:2026-07-12T07:00"}).reason == "missed: the lane was down"
    assert plan_for(client, "ap-a", task={**late, "occurrence_key": "run:r1"}).status == "act"
    assert plan_for(client, "ap-a", task={**late, "occurrence_key": "reply:n1"}).status == "act"
    assert plan_for(client, "ap-a", task={"created_at": "2026-07-12T08:30:00Z", "occurrence_key": "q1:ap-a:2026-07-12T08:30"}).status == "act"


def test_on_duty_is_the_backends_answer_and_only_without_it_the_weekly_days_rule():
    from quest_ai_runner.intelligence.proactivity.autopilot_units import on_duty
    assert on_duty({"status": {"runs_today": False}, "days": []}, NOW, "UTC") is False       # a monthly autopilot on a day that is not its day
    assert on_duty({"status": {"runs_today": True}, "days": [MON]}, NOW, "UTC") is True
    assert on_duty({"days": [MON]}, NOW, "UTC") is False and on_duty({"days": [SUN]}, NOW, None) is True


def test_the_routing_day_is_utc_when_the_quest_names_no_zone():
    late_utc = datetime(2026, 7, 12, 23, 30, tzinfo=timezone.utc)
    client = RecordingClient([ap("ap-a", "A", instructions="x")], goals_payload(goal("g1", "Write chapter")), quest_fields={"run_timezone": None})
    AutopilotPass(client, team_id="team1", now=lambda: late_utc, judge=scripted_judge({"g1": "ap-a"}, [])).run(
        {"id": "r", "goal_id": "q1", "task_kind": "autopilot_run", "autopilot_id": "ap-a", "text": "run"})
    assert ("q1", "2026-07-12") in client.records


def test_a_run_for_one_autopilot_sees_only_its_own_last_run():
    from quest_ai_runner.intelligence.proactivity.autopilot_briefs import select_last_run_output
    rows = [{"id": "a", "task_kind": "autopilot_run", "autopilot_id": "ap-a", "result": "A's result", "updated_at": "2026-07-11T09:00:00Z"},
            {"id": "b", "task_kind": "autopilot_run", "autopilot_id": "ap-b", "result": "B's result", "updated_at": "2026-07-12T08:00:00Z"}]
    assert select_last_run_output(rows, "ap-a")["id"] == "a" and select_last_run_output(rows, "ap-b")["id"] == "b"
    assert select_last_run_output(rows)["id"] == "b"


def test_a_lane_without_an_update_engine_still_reads_the_captures_since_the_autopilots_own_last_run():
    from quest_ai_runner.intelligence.higher_mind.guidance.insights import InsightsContext
    seen = []

    class Reading(AutopilotPass):
        def _insights(self, last_pass_at, quest_id=""):
            seen.append((last_pass_at, quest_id))
            return InsightsContext()
    client = Client([ap("ap-a", "A", instructions="x")], goals_payload(goal("g1", "G")),
                    tasks=[{"id": "t0", "goal_id": "q1", "task_kind": "autopilot_run", "autopilot_id": "ap-a", "status": "done", "created_at": "2026-07-11T09:00:00Z"},
                           {"id": "t1", "goal_id": "q1", "task_kind": "autopilot_run", "autopilot_id": "ap-b", "status": "done", "created_at": "2026-07-12T08:00:00Z"}])
    Reading(client, team_id="team1", now=lambda: NOW).run({"id": "r", "goal_id": "q1", "task_kind": "autopilot_run", "autopilot_id": "ap-a", "text": "run"})
    assert len(seen) == 1 and seen[0][1] == "q1" and seen[0][0].startswith("2026-07-11")      # ap-b's later run is not ap-a's window


def test_the_email_contract_follows_the_autopilots_own_email_setting():
    from quest_ai_runner.consciousness.request_intake.executor_context import ContextViewMixin

    class Quests:
        def get_quest(self, quest_id):
            return {"autopilot": {"email": {"enabled": False}}}

        def list_autopilots(self, quest_id):
            return {"autopilots": [{"id": "ap-a", "email": {"enabled": True}}, {"id": "ap-b", "email": {"enabled": False}}]}

    mixin = ContextViewMixin.__new__(ContextViewMixin)
    mixin._client = Quests()
    assert mixin._mailing_quest_id(None, "q1", "ap-a") == "q1" and mixin._mailing_quest_id(None, "q1", "ap-b") is None


def test_leftover_pass_tasks_are_retired_once_and_then_the_lane_stops_listing_for_them():
    from types import SimpleNamespace
    from quest_ai_runner.intelligence.proactivity.poller_passes import PassesMixin

    class Lane(PassesMixin):
        def __init__(self):
            self.cfg = SimpleNamespace(autopilot_ensure_pass_task=True, team_id="t", team_ids=[])
            self.client = SimpleNamespace(configured=True)
            self.open = [{"id": "p1", "status": "queued", "goal_id": "q1"}]
            self.lists, self.retired = 0, []

        def _list_owned_pass_tasks(self):
            self.lists += 1
            return list(self.open)

        def _quest_schedule_snapshot(self):
            return {"q1": {"team_id": "t"}}

        def _retire_team_pass(self, occurrences):
            pass

        def _ensure_quest_pass_tasks(self, open_by_series, snapshot, owned=None):
            self.retired += [o["id"] for o in open_by_series["q1"]]
            self.open = []

    lane = Lane()
    lane._ensure_autopilot_pass()                    # finds the leftover, retires it
    lane._ensure_autopilot_pass()                    # finds none: done
    lane._ensure_autopilot_pass()                    # no list at all
    assert lane.retired == ["p1"] and lane.lists == 2


def test_quest_improvements_source_spends_nothing_when_nothing_is_unacted_and_tells_suggesters_first():
    from quest_ai_runner.intelligence.proactivity.autopilot_purpose_improvements import improvements_due

    class Quest:
        def __init__(self, state):
            self.state, self.calls = state, []

        def send_request(self, method, path, **kw):
            self.calls.append(method)
            self.paths = getattr(self, "paths", []) + [(path, kw.get("body"))]
            return self.state

    quiet = Quest({"total": 0, "to_tell": 0, "items": []})
    assert improvements_due(quiet) == [] and quiet.calls == ["GET"]
    busy = Quest({"total": 2, "to_tell": 1, "items": [
        {"task_id": "t1", "kind": "awaiting_review", "what": "awaiting_review: Dark mode"},
        {"task_id": "t2", "kind": "tell_suggester", "what": "tell_suggester: Old idea"}]})
    due = improvements_due(busy)
    assert [d["id"] for d in due] == ["t1"] and busy.calls == ["GET", "POST", "POST"]
    # The idea awaiting review is noted, so the next pass does not start a model run on it again while it waits on a person.
    assert busy.paths[-1] == ("/api/assistant-tasks/suggestions/follow-up/noted", {"task_ids": ["t1"]})
    approved = Quest({"total": 1, "to_tell": 0, "items": [{"task_id": "t3", "kind": "approved_not_worked", "what": "approved: X"}]})
    assert [d["id"] for d in improvements_due(approved)] == ["t3"] and approved.calls == ["GET"]
    assert improvements_due(Quest(None)) == []


def test_quest_improvements_status_block_is_sent_and_the_template_line_never_is():
    from quest_ai_runner.intelligence.proactivity.autopilot_purpose_improvements import RULES, parse_statuses, report_statuses

    assert parse_statuses(RULES) == []                     # the brief's own example sends nothing if the run echoes it
    output = ("Worked two ideas.\n\nSUGGESTION STATUS\n- atask_abc123 | done | Added a dark theme switch in Settings. It follows the phone's setting.\n"
              "atask_def456 | failed | The calendar API refused the scope.\nEND SUGGESTION STATUS\n")
    assert [(i["task_id"], i["status"]) for i in parse_statuses(output)] == [("atask_abc123", "done"), ("atask_def456", "failed")]

    class Quest:
        def __init__(self):
            self.calls = []

        def send_request(self, method, path, **kw):
            self.calls.append((method, path, kw.get("body")))
            if "def456" in path:
                raise RuntimeError("409")
            return {"told": True}

    quest = Quest()
    assert report_statuses(quest, output) == 1
    assert quest.calls[0] == ("POST", "/api/assistant-tasks/suggestions/atask_abc123/status",
                              {"status": "done", "how": "Added a dark theme switch in Settings. It follows the phone's setting."})
    assert report_statuses(quest, "no block here") == 0


def test_also_checks_adds_due_ideas_to_the_autopilots_own_run_and_nothing_when_none_are_due():
    """The retention autopilot keeps its own work and ALSO checks quest_improvements: the check is one no-model read, and only due ideas
    join its brief. Nothing due (or an unknown slug) adds nothing, so the run is exactly what it was without the check."""
    from datetime import datetime, timezone
    from quest_ai_runner.intelligence.proactivity.autopilot_purpose_improvements import improvements_purpose
    from quest_ai_runner.intelligence.proactivity.autopilot_sources import SOURCES, also_checks_text, register_source

    class Quest:
        def __init__(self, state):
            self.state, self.calls = state, []

        def send_request(self, method, path, **kw):
            self.calls.append((method, path))
            return self.state

    now = datetime.now(timezone.utc)
    saved = SOURCES.get("quest_improvements")
    try:
        quiet = Quest({"total": 0, "items": []})
        register_source(improvements_purpose(quiet))
        assert also_checks_text({"also_checks": ["quest_improvements", "no_such_source"]}, now) is None
        assert quiet.calls == [("GET", "/api/assistant-tasks/suggestions/follow-up")]
        assert also_checks_text({}, now) is None
        busy = Quest({"total": 1, "to_tell": 0, "items": [{"task_id": "t9", "kind": "approved_not_worked", "what": "approved_not_worked: Dark mode"}]})
        register_source(improvements_purpose(busy))
        text = also_checks_text({"also_checks": ["quest_improvements"]}, now)
        assert text and text.startswith("ALSO ON THIS RUN (quest_improvements)") and "Dark mode (task id t9)" in text
        assert "SUGGESTION STATUS" in text
    finally:
        if saved is not None:
            SOURCES["quest_improvements"] = saved
        else:
            SOURCES.pop("quest_improvements", None)
