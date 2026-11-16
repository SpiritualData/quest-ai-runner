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

from .autopilot_effective import AutopilotPass, effective_autopilots, stored_autopilots
from quest_ai_runner.intelligence.proactivity.autopilot_defaults import (
    AUTOPILOT_WORK_KIND, BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS, BUNDLED_DEFAULT_QUEST_INSTRUCTIONS, DEFAULT_ASSISTANT_REP_ID)
from quest_ai_runner.intelligence.proactivity.autopilot import (
    DEFAULT_TEAM_DAILY_BUDGET, current_scope_label,
    compose_batch_text)
from quest_ai_runner.intelligence.proactivity.autopilot_personas import default_persona_instructions_for, default_quest_instructions_for
from quest_ai_runner.intelligence.proactivity.quest_schedule import new_ask_since
from .autopilot_effective import due_trigger, run_pending


def is_due(autopilot_cfg, now, tz=None):
    """Whether the hub's Proactivity says the quest's pass is due for these settings."""
    return bool(due_trigger(autopilot_cfg, now, tz))


# The client's OWN period formats, so the fake below accepts exactly what the real one accepts
# instead of a shape invented here (the five quest-backend's period_utils parses).
from quest_ai_runner.body.internal_context.quest.client.quest_client import _PERIOD_RE
from quest_ai_runner.body.internal_context.quest.client.quest_client_support import WriteResult

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
                 accepts_bookkeeping=False, asks_by_quest=None, list_asks_fails_for=None):
        self.quests = list(quests or [])            # full quest states, keyed for get_quest_autopilot
        self.goals_by_quest = dict(goals_by_quest or {})
        self.tasks = list(tasks or [])
        self.asks_by_quest = dict(asks_by_quest or {})
        # quest_ids for which list_asks should mimic a failed read (returns None), the same
        # contract QuestClient.list_asks uses to distinguish a failed read from a real empty one.
        self.list_asks_fails_for = set(list_asks_fails_for or ())
        self.list_asks_calls = []
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
                cfg = dict(self._autopilot_state.get(quest_id, {}))
                cfg["autopilots"] = stored_autopilots(cfg)
                return {"outcome": q.get("outcome"), "autopilot": cfg}
        return {}

    def list_quest_goals(self, quest_id, *, team_id=None):
        self.goal_list_calls.append(quest_id)
        return self.goals_by_quest.get(quest_id, {})

    def get_goal(self, goal_id, *, quest_id=None, team_id=None):
        return self.goal_docs.get(goal_id, {})

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, source=None, task_kind=None, created_after=None, slim=False, without_progress=False):
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

    def list_asks(self, *, quest_id, limit=50):
        self.list_asks_calls.append(quest_id)
        if quest_id in self.list_asks_fails_for:
            return None
        return list(self.asks_by_quest.get(quest_id, []))

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
            if k == "autopilot_state" and self.accepts_bookkeeping:
                # Merged key by key AND field by field, like the real route.
                merged = current.setdefault("autopilot_state", {})
                for key, fields_of in v.items():
                    merged[key] = {**merged.get(key, {}), **fields_of}
            elif k == "routine_runs" and self.accepts_bookkeeping:
                current[k] = {**current.get(k, {}), **v}
            elif self.accepts_bookkeeping or k in self.ACCEPTED_AUTOPILOT_FIELDS:
                current[k] = v
            # else: silently dropped, exactly like the real endpoint's Pydantic model.
        echoed = dict(current)
        echoed["autopilots"] = stored_autopilots(echoed)
        return {"quest_id": quest_id, "autopilot": echoed}


def _quest(quest_id, *, mode="act", cadence="weekly", last_pass_at=None, planning="work_only",
          env_id=None, model=None, personas=None, outcome="ship the thing", miss_streak=0,
          adopt_recurring=None, run_requested_at=None, default_quest_instructions=None,
          default_persona_instructions=None):
    autopilot = {"mode": mode, "cadence": cadence, "planning": planning,
                "miss_streak": miss_streak}
    # The two read-only fields a CURRENT backend derives and serves on the quest payload, one per
    # brief slot. The fake omits both by default on purpose, so the bundled-fallback path is what
    # most tests exercise, and either can be set alone since the slots default independently.
    if default_quest_instructions is not None:
        autopilot["default_quest_instructions"] = default_quest_instructions
    if default_persona_instructions is not None:
        autopilot["default_persona_instructions"] = default_persona_instructions
    if last_pass_at is not None:
        autopilot["last_pass_at"] = last_pass_at
    if run_requested_at is not None:
        autopilot["run_requested_at"] = run_requested_at
    if env_id is not None:
        autopilot["env_id"] = env_id
    if model is not None:
        autopilot["model"] = model
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



















# --- new_ask_since (the reactive trigger, the hub's Proactivity over Quest's events) ------------------------------------------------------------------

def test_new_ask_since_true_when_never_run_and_something_exists():
    assert new_ask_since([{"occurred_at": "2026-01-01T00:00:00Z"}], None) is True


def test_new_ask_since_false_when_never_run_and_nothing_exists():
    assert new_ask_since([], None) is False


def test_new_ask_since_true_for_an_ask_after_the_last_pass():
    asks = [{"occurred_at": "2026-07-05T00:00:00Z"}]
    assert new_ask_since(asks, "2026-07-01T00:00:00Z") is True


def test_new_ask_since_false_for_an_ask_before_the_last_pass():
    asks = [{"occurred_at": "2026-06-20T00:00:00Z"}]
    assert new_ask_since(asks, "2026-07-01T00:00:00Z") is False


def test_new_ask_since_ignores_an_ask_with_no_parseable_timestamp():
    asks = [{"occurred_at": None}, {"occurred_at": "not a date"}]
    assert new_ask_since(asks, "2026-07-01T00:00:00Z") is False


def test_new_ask_since_true_when_any_one_of_several_is_new():
    asks = [{"occurred_at": "2026-06-01T00:00:00Z"}, {"occurred_at": "2026-07-10T00:00:00Z"}]
    assert new_ask_since(asks, "2026-07-01T00:00:00Z") is True


# --- gate: reactive mode ----------------------------------------------------------------------



def test_reactive_gate_runs_when_a_new_ask_landed_since_the_last_pass():
    q1 = _quest("q1", mode="reactive", last_pass_at="2026-07-01T09:00:00Z")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    asks = {"q1": [{"occurred_at": "2026-07-05T00:00:00Z"}]}  # after last_pass_at
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, asks_by_quest=asks)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert result.skipped == []




def test_reactive_gate_is_bypassed_by_an_explicit_run_now():
    """A person pressing 'Run now' gets a run regardless of whether Autopilot thinks anything is
    new -- the same override cadence itself already honors."""
    q1 = _quest("q1", mode="reactive", last_pass_at="2026-07-01T09:00:00Z",
               run_requested_at="2026-07-11T09:00:00Z")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, asks_by_quest={"q1": []})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    # The override short-circuits the gate, so the only read is the batch's What You Asked section.
    assert client.list_asks_calls == ["q1"]


def test_reactive_gate_fails_open_when_the_asks_read_fails():
    # QuestClient.list_asks returns None (never []) on a QuestApiError/QuestNotConfigured -- a
    # transient backend blip or an expired key must run the pass, not be read as "nothing new"
    # and silently wedge this quest shut forever.
    q1 = _quest("q1", mode="reactive", last_pass_at="2026-07-01T09:00:00Z")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals, list_asks_fails_for={"q1"})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    result = passer.run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert result.skipped == []
    assert client.list_asks_calls == ["q1"]


def test_non_reactive_modes_read_the_asks_once_for_the_brief_and_never_for_a_gate():
    q1 = _quest("q1", mode="act")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert client.list_asks_calls == ["q1"]


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











def test_every_character_on_duty_today_gets_one_batch():
    """With a default brief nobody on duty is idle, so the roster decides how many batches a pass
    makes. Three unrestricted personas produce three."""
    q1 = _quest("q1", personas=[{"rep_id": "rep_a"}, {"rep_id": "rep_b"}, {"rep_id": "rep_c"}])
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", daily_budget=5, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 3
    assert [t["assignee_rep_id"] for t in client.created_tasks] == ["rep_a", "rep_b", "rep_c"]






def test_default_quest_instructions_tell_a_run_to_iterate_rather_than_fragment():
    """A run should refine an existing source_of_truth/ (or otherwise established) document rather
    than writing a new one-off, and file scratch/dated updates in their own subfolder. See
    ``ai_driven/source_of_truth/`` in quest_folder_zones.py for the convention this reinforces."""
    assert "source_of_truth" in BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
    assert "instead of writing another" in BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
    assert "subfolder" in BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
    assert "—" not in BUNDLED_DEFAULT_QUEST_INSTRUCTIONS  # no em dashes, matches the file's own rule


def test_default_quest_instructions_no_longer_carry_spec_keeping():
    """Nothing read the spec an autopilot pass kept; the backend refreshes an intent on events instead."""
    for tool in ("refresh_spec", "place_quest", "list_intents"):
        assert tool not in BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
    assert "specification of the quest" not in BUNDLED_DEFAULT_QUEST_INSTRUCTIONS


# --- the two default briefs, each defaulting on its own -------------------------------------------
# The quest-wide slot (how to work here and how to deliver) and the persona slot (what this
# character works on) fall back INDEPENDENTLY: whatever the person wrote fills its own slot, the
# backend's default fills the other, and both always reach the run. Writing one level never
# silences the other, because the two say different things.

SERVED_QUEST_DEFAULT = "The server's own default brief for how to work on a quest."
SERVED_PERSONA_DEFAULT = "The server's own default brief for what a character works on."


def _defaulted_quest(quest_id="q1", **kwargs):
    """A quest whose backend serves both defaults and whose owner has written neither."""
    return _quest(quest_id, personas=[{"rep_id": "rep_a"}],
                  default_quest_instructions=SERVED_QUEST_DEFAULT,
                  default_persona_instructions=SERVED_PERSONA_DEFAULT, **kwargs)






def test_a_written_persona_brief_leaves_the_quest_slot_on_its_default():
    """The mirror: a character with their own job still gets the quest's standing rules about how
    to work and how to deliver, which is what the quest-wide default is."""
    q1 = _defaulted_quest()
    q1["autopilot"]["personas"] = [{"rep_id": "rep_a", "instructions": "Do Bailey's own job."}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Do Bailey's own job." in text
    assert SERVED_PERSONA_DEFAULT not in text
    assert SERVED_QUEST_DEFAULT in text


def test_both_levels_written_puts_no_default_text_in_the_run_at_all():
    q1 = _defaulted_quest()
    q1["autopilot"]["instructions"] = "Write the daily brief for this quest."
    q1["autopilot"]["personas"] = [{"rep_id": "rep_a", "instructions": "Do Bailey's own job."}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    text = client.created_tasks[0]["text"]
    assert "Write the daily brief for this quest." in text
    assert "Do Bailey's own job." in text
    for absent in (SERVED_QUEST_DEFAULT, SERVED_PERSONA_DEFAULT,
                   BUNDLED_DEFAULT_QUEST_INSTRUCTIONS, BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS):
        assert absent not in text
    assert "written by the person who owns it" in text
    assert "Nobody has written a brief" not in text






def test_a_written_brief_still_titles_its_own_batch():
    q1 = _defaulted_quest()
    q1["autopilot"]["personas"] = [
        {"rep_id": "rep_a", "instructions": "# Rewrite the launch page\nThen say what is left."}]
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={})
    AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert client.created_tasks[0]["title"] == "rep_a"      # the autopilot's own name, not a line of its brief


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








def test_a_quest_with_no_autopilot_model_creates_a_task_with_no_model_key_at_all():
    """Unset means the lane's own default model ladder applies -- the create call must OMIT both
    keys entirely rather than send ``model: None``, matching the create_task contract."""
    q1 = _quest("q1")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = FakeAutopilotClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "pass"})
    assert "model" not in client.created_tasks[0]
    assert "deep_run_model" not in client.created_tasks[0]


def test_a_client_whose_create_task_rejects_model_still_gets_its_task_created():
    """A stand-in ``create_task`` that predates ``model``/``deep_run_model``/``parent_task_id``
    must still create the task: each field is an improvement to routing, never a requirement to
    run."""
    class OlderClient(FakeAutopilotClient):
        def create_task(self, text, **kwargs):
            if "model" in kwargs or "deep_run_model" in kwargs or "parent_task_id" in kwargs:
                raise TypeError("create_task() got an unexpected keyword argument")
            return super().create_task(text, **kwargs)

    q1 = _quest("q1", model="opus")
    goals_payload = _goals_payload(("day", "2026-07-12", [_goal("g1")]))
    client = OlderClient(quests=[q1], goals_by_quest={"q1": goals_payload})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert "model" not in client.created_tasks[0]
    assert "deep_run_model" not in client.created_tasks[0]


# --- the built-in default assistant as a roster entry ------------------------------------------
#
# A quest with an empty roster has always produced one plain-assistant batch per due pass. That is
# the right work but nothing the person can point at: days and a brief live on a roster ENTRY, so
# neither was sayable on a quest with no AI reps to pick from (every personal quest). An entry
# naming DEFAULT_ASSISTANT_REP_ID is that same fallback written down, and these pin that writing it
# down changes WHEN and WITH WHAT it runs, never WHO runs it.


def test_the_default_assistant_entry_runs_as_the_plain_assistant():
    """Same batch an unrostered quest gets: no rep is stamped, because no such rep exists."""
    q1 = _quest("q1", personas=[{"rep_id": DEFAULT_ASSISTANT_REP_ID}])
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 1
    assert "assignee_rep_id" not in client.created_tasks[0]
    assert f"Act as {DEFAULT_ASSISTANT_REP_ID}" not in client.created_tasks[0]["text"]








def test_a_real_rep_alongside_the_default_assistant_still_gets_its_own_batch():
    q1 = _quest("q1", personas=[{"rep_id": DEFAULT_ASSISTANT_REP_ID}, {"rep_id": "rep_a"}])
    client = FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    result = AutopilotPass(client, team_id="team1", daily_budget=5, now=_now).run({"text": "pass"})
    assert len(result.created_task_ids) == 2
    assert [t.get("assignee_rep_id") for t in client.created_tasks] == [None, "rep_a"]


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
    from quest_ai_runner.intelligence.proactivity.autopilot import AutopilotResult

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
        return WriteResult.from_payload({"id": f"goal_{len(self.created_goals)}"}, 201)   # the real client's typed result














# --- dry-run creates nothing ---------------------------------------------------------------------







# --- compose_batch_text ---------------------------------------------------------------------

def test_compose_batch_text_states_the_quest_outcome_and_the_period_it_sits_in():
    text = compose_batch_text("Ship the launch", scope_label="day:2026-07-12")
    assert "Quest outcome: Ship the launch" in text
    assert "Scope: this quest's day:2026-07-12" in text
    assert "does not make the period's contents this run's workload" in text


def test_compose_batch_text_names_the_persona_when_one_resolved():
    text = compose_batch_text("Ship it", "bailey")
    assert "Act as bailey" in text


def test_each_brief_slot_defaults_on_its_own_and_both_always_reach_the_run():
    """THE LAYERING RULE, at the one place that decides it. Two slots, each filled by what the
    person wrote for it or by that level's own built-in brief, and both emitted either way. They
    are not alternatives: one says how to work here and how to deliver, the other says what to work
    on, so a run that lost the second because its owner wrote the first would not know what it is
    for."""
    quest_default = "The built-in quest brief."
    persona_default = "The built-in character brief."
    neither = compose_batch_text("Ship it", "bailey",
                                 default_quest_instructions=quest_default,
                                 default_persona_instructions=persona_default)
    assert quest_default in neither
    assert persona_default in neither

    with_quest = compose_batch_text("Ship it", "bailey", instructions="The quest's own brief.",
                                    default_quest_instructions=quest_default,
                                    default_persona_instructions=persona_default)
    assert "The quest's own brief." in with_quest
    assert quest_default not in with_quest
    assert persona_default in with_quest          # the other slot is untouched

    with_persona = compose_batch_text("Ship it", "bailey",
                                      persona_instructions="Bailey's own brief.",
                                      default_quest_instructions=quest_default,
                                      default_persona_instructions=persona_default)
    assert "Bailey's own brief." in with_persona
    assert persona_default not in with_persona
    assert quest_default in with_persona

    with_both = compose_batch_text("Ship it", "bailey", instructions="The quest's own brief.",
                                   persona_instructions="Bailey's own brief.",
                                   default_quest_instructions=quest_default,
                                   default_persona_instructions=persona_default)
    assert quest_default not in with_both
    assert persona_default not in with_both


def test_a_defaulted_block_never_claims_the_person_wrote_it():
    """The written preambles say "written by the person who owns it", which is false of a default.
    A run told that about words nobody wrote would report back against a standard nobody set."""
    text = compose_batch_text("Ship it", "bailey",
                              default_quest_instructions="The built-in quest brief.",
                              default_persona_instructions="The built-in character brief.")
    assert "written by the person who owns it" not in text
    assert "Nobody has written a brief for this quest yet" in text
    assert "Nobody has written a brief for this character here yet" in text
    # A defaulted persona block still states the precedence its written counterpart states: the
    # precedence belongs to the LEVEL, not to who happened to write the text.
    assert "MORE SPECIFIC" in text


def test_two_defaulted_blocks_are_layered_exactly_as_two_written_ones_are():
    quest_default = "The built-in quest brief."
    persona_default = "The built-in character brief."
    text = compose_batch_text("Ship it", "bailey", scope_label="day:2026-07-12",
                              default_quest_instructions=quest_default,
                              default_persona_instructions=persona_default,
                              goal_ladder=[{"scope": "day", "period": "2026-07-12",
                                            "goals": [{"name": "A goal", "deadline": ""}],
                                            "more": 0}])
    assert (text.index("Scope: this quest's day:2026-07-12") < text.index(quest_default)
            < text.index(persona_default) < text.index("THE PERSON'S CURRENT GOALS"))


def test_no_default_briefs_composes_byte_identically_to_before_the_parameters_existed():
    assert (compose_batch_text("Ship it", "bailey", default_quest_instructions=None,
                               default_persona_instructions=None)
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


def test_compose_batch_text_improves_one_solution_even_under_a_written_brief():
    """Joshua, 2026-10-19: every autopilot run improves at least one solution. The rule first lived
    in the built-in quest brief, which a quest's own brief replaces outright, so any quest whose
    owner had written one never saw it. It belongs to the brief's always-required part instead,
    with or without a written brief, and once, not again inside the default brief."""
    from quest_ai_runner.intelligence.proactivity.autopilot_defaults import BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
    written = compose_batch_text("Ship it", instructions="Work only on the eval tracker.",
                                 default_quest_instructions=BUNDLED_DEFAULT_QUEST_INSTRUCTIONS)
    assert "Work only on the eval tracker." in written
    assert "IMPROVE ONE SOLUTION" in written
    assert "improve at least one solution" not in BUNDLED_DEFAULT_QUEST_INSTRUCTIONS
    defaulted = compose_batch_text("Ship it",
                                   default_quest_instructions=BUNDLED_DEFAULT_QUEST_INSTRUCTIONS)
    assert defaulted.count("improve at least one solution") == 1
    assert "IMPROVE ONE SOLUTION" in compose_batch_text("Ship it")


def test_every_batch_brief_carries_the_standing_work_standard_once():
    from quest_ai_runner.intelligence.proactivity.autopilot_briefs import STANDING_WORK_STANDARD
    assert STANDING_WORK_STANDARD.startswith("STANDING WORK STANDARD.")
    text = compose_batch_text("Ship it")
    assert text.count("STANDING WORK STANDARD.") == 1
    assert text.index("IMPROVE ONE SOLUTION") < text.index("STANDING WORK STANDARD.")


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






def test_period_keys_match_the_backends_underscore_format():
    """quest-backend's period ids use an UNDERSCORE (period_utils.get_current_period): day is a
    plain ISO date, but week/month/quarter are 2026_W28 / 2026_07 / 2026_Q3. A hyphen here would
    never match the current period, so every quest would silently fall through to the unscoped
    fallback and today's goals would never be worked."""
    from quest_ai_runner.intelligence.proactivity.autopilot_periods import _current_period_key

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



# --- what the pass REPORTS ---------------------------------------------------------------------
# The pass row is read by a person, in their quest. "Created 1 task(s): atask_d2014273cff6" names an
# internal id instead of the work and presents the scanner's bookkeeping as the outcome.







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




def test_daily_budget_defaults_to_the_documented_default():
    client = FakeAutopilotClient(quests=[])
    passer = AutopilotPass(client, team_id="team1")
    assert passer._daily_budget == DEFAULT_TEAM_DAILY_BUDGET == 3


# --- executor routing: handler == "autopilot" replaces the normal deep-run path ------------------









def test_autopilot_created_work_is_never_routed_back_into_another_pass():
    """The loop guard. Work batches carry task_kind="autopilot_work", NOT the pass's "autopilot"
    kind -- otherwise the executor would route each created task into another autopilot pass,
    which would create more tasks, forever."""
    from quest_ai_runner.consciousness.request_intake.executor import _is_autopilot_pass
    from quest_ai_runner.intelligence.proactivity.autopilot_defaults import AUTOPILOT_WORK_KIND

    assert _is_autopilot_pass({"task_kind": "autopilot"}) is True
    assert _is_autopilot_pass({"task_kind": AUTOPILOT_WORK_KIND}) is False


def test_executor_ordinary_handler_value_is_not_routed_to_autopilot():
    """A normal rep-label handler (e.g. stamped by the poller's claim()) must NOT be mistaken for
    the autopilot route — only the exact value "autopilot" triggers it. Proof: with NO
    autopilot_pass wired, a misrouted task would report failed ("autopilot is not configured");
    instead it completes normally through the answer path."""
    from quest_ai_runner.consciousness.request_intake.executor import TaskExecutor

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
    from quest_ai_runner.intelligence.higher_mind.objectives.quest_folder_sync import NextSteps, write_next_steps

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




def test_a_dry_run_does_not_touch_the_artifact(tmp_path):
    client = _ContextEntryClient(
        quests=[_quest("q1")],
        goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    AutopilotPass(client, team_id="team1", now=_now,
                  quest_folder_map={"q1": str(tmp_path)}).run({"text": "dry-run pass"})
    assert not (tmp_path / "QUEST_SYNC.md").exists()
    assert client.entries == []






# --- "Run now": a pending request satisfies the cadence gate -------------------------------------



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


# --- one lane, several teams (team_resolver / team_ids) ------------------------------------

class TeamRecordingClient(FakeAutopilotClient):
    """Records the ``team_id`` every read and write was scoped to, and scopes ``list_tasks`` by it
    the way the real route does, so a per-team budget can be told apart from a lane-wide one."""

    def __init__(self, *, team_by_quest=None, **kw):
        super().__init__(**kw)
        self.team_by_quest = dict(team_by_quest or {})
        self.list_task_teams = []

    def list_tasks(self, *, team_id=None, status=None, goal_id=None, source=None, task_kind=None, created_after=None, slim=False, without_progress=False):
        self.list_task_teams.append(team_id)
        rows = super().list_tasks(status=status, goal_id=goal_id, source=source,
                                  task_kind=task_kind)
        if team_id is None:
            return rows
        return [t for t in rows if t.get("team_id", team_id) == team_id]


def _resolver(team_by_quest):
    return lambda quest_id: team_by_quest.get(quest_id, "")










# --- the work a pass creates must be runnable by the lane that created it -----------------------



def test_created_work_has_no_assignee_when_the_lane_names_no_account():
    """Unconfigured lanes keep the old default-executor behaviour, byte for byte."""
    q1 = _quest("q1")
    goals = {"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))}
    client = FakeAutopilotClient(quests=[q1], goals_by_quest=goals)
    passer = AutopilotPass(client, team_id="team1", now=_now)
    passer.run({"text": "autopilot pass"})
    assert client.created_tasks
    assert all("assignee_user_id" not in t for t in client.created_tasks)


# --- two passes for one quest due at the same minute (incident 2026-09-25) ----------------------



# --- the budget stop is visible ---------------------------------------------------------------

class RecordingSink:
    """An escalation sink stand-in that keeps one open ask per dedupe key, like the real one."""

    def __init__(self):
        self.escalations = []
        self.open_keys = set()

    def escalate(self, escalation):
        if escalation.dedupe_key in self.open_keys:
            return "existing"
        self.open_keys.add(escalation.dedupe_key)
        self.escalations.append(escalation)
        return "dec_1"


def _three_persona_client():
    q1 = _quest("q1", personas=[{"rep_id": "rep_a"}, {"rep_id": "rep_b"}, {"rep_id": "rep_c"}])
    return FakeAutopilotClient(
        quests=[q1], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})






def test_no_budget_stop_raises_no_ask():
    sink = RecordingSink()
    AutopilotPass(_three_persona_client(), team_id="team1", daily_budget=5, now=_now,
                  escalation=sink).run({"text": "pass"})
    assert sink.escalations == []


