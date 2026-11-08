"""Offline tests: a status-shaped turn is answered from one code-composed, dated snapshot, with no planner loop, for the three turn shapes (a status question, a status
script to read out, a question about the asker's own priority)."""
from types import SimpleNamespace
from typing import Any, List

from hubofhubs.api import Mode

from .conftest import StubProvider, StubRetrieval
from .test_answer_explanation import RecordingSink
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import status_snapshot
from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
from quest_ai_runner.the_intelligence.mind.planner.orchestrator_decisions import parse_goal_condition_reply, take_status_flags
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

TABLES = {"solutions": {"s_top": {"name": "Plan", "solution_ids": ["s_a"], "authoritative_metric_id": "m"}, "s_a": {"name": "First"}, "s_b": {"name": "Second"}},
          "metrics": {"m": {"name": "Paid subscribers", "solution_ids": ["s_top"], "source": "paid"}}}
READING = {"value": 0, "as_of": "2026-11-07", "source": "Revenue analytics", "detail": {"paid": 0, "trialing": 0}}
GOALS = [
    {"id": "g1", "title": "Second milestone next step: week of 2026-11-02", "text": "x\nCurrent value: 4 (as of 2026-11-06, from Source)", "due": "2026-11-08", "spec_solution_id": "s_b", "assignee_user_id": "u2"},
    {"id": "g2", "title": "First milestone next step: week of 2026-11-02", "text": "", "due": "2026-11-09", "spec_solution_id": "s_a", "assignee_user_id": "u1"},
    {"id": "g3", "title": "Unrelated task", "text": "", "due": "2026-10-01", "spec_solution_id": None, "assignee_user_id": "u1"},
]


class Registry:
    def __init__(self):
        self.calls: List[Any] = []

    def get(self, name):
        return object()

    def invoke(self, name, args, ctx):
        self.calls.append((name, dict(args)))
        if name == "read_spec_records":
            return SimpleNamespace(ok=True, data={"records": TABLES, "time_zone": ""}, text="")
        if name == "find_quest":
            return SimpleNamespace(ok=True, data=[{"quest_id": "q1", "outcome": "Reach the target"}, {"quest_id": "q2", "outcome": "Another aim"}], text="")
        if name == "read_metric_source":
            return SimpleNamespace(ok=True, data=READING, text="")
        rows = [g for g in GOALS if not args.get("assignee_user_id") or g["assignee_user_id"] == args["assignee_user_id"]]
        if args.get("text_contains"):
            rows = [g for g in rows if args["text_contains"] in g["title"]]
        return SimpleNamespace(ok=True, data=[dict(g, completed=False, abandoned=False) for g in rows], text="")


def test_the_snapshot_holds_the_live_key_measure_the_priorities_in_ladder_order_and_for_a_my_question_the_askers_goals():
    registry = Registry()
    snap = status_snapshot.build(registry, "q1", "u1", own=True)
    text = snap["text"]
    assert snap["locator"] == "status_snapshot" and "Paid subscribers: 0 (as of 2026-11-07, from Revenue analytics)" in text
    first, second = text.index("First milestone next step"), text.index("Second milestone next step")
    assert first < second and "Current value: 4 (as of 2026-11-06, from Source)" in text and "due 2026-11-08" in text
    assert "Unrelated task" not in text.split("Active priorities")[1].split("The asker's own")[0]
    assert "The asker's own open goals" in text and "Unrelated task (due 2026-10-01)" in text.split("The asker's own")[1]
    assert "The asker's own" not in status_snapshot.build(registry, "q1", "u1", own=False)["text"]
    assert status_snapshot.build(None, "q1") is None


def test_the_analyser_reports_the_status_shape_and_the_spoken_delivery_as_fields_not_filters():
    _, constraints = parse_goal_condition_reply('A script.\n{"asks_status": true, "read_aloud": true, "actor": "me"}')
    flags, rest = take_status_flags(constraints)
    assert flags == {"asks_status": True, "read_aloud": True} and rest == {"actor": "me"}
    assert take_status_flags(None)[0] == {"asks_status": False, "read_aloud": False}


class StatusProvider(StubProvider):
    def __init__(self, analyser):
        super().__init__(decisions=[{"action": "read", "reads": [{"rel_path": "n.md"}], "rationale": "must not run"}])
        self.analyser, self.planner_calls, self.writer_prompts = analyser, 0, []

    def plan(self, prompt, *, model, tool_schema):
        self.planner_calls += 1 if "You are the PLANNER" in prompt else 0      # a judge call also goes through plan()
        return super().plan(prompt, model=model, tool_schema=tool_schema)

    def answer(self, messages, *, model=None, system=None):
        text = "\n".join(str(m.get("content")) for m in messages) + (system or "")
        if "request analyser" in text:
            return self.analyser
        self.writer_prompts.append(text)
        return "There are 0 paid subscribers as of 7 November."


def run_status(analyser, message, quest_id="q1", placement=None):
    provider = StatusProvider(analyser)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), tools=_tools(), quest_placement=placement,
                        config=OrchestratorConfig(overseer=True))
    res = orch.run(message, mode=Mode.LIVE, sink=RecordingSink(), quest_id=quest_id, context_meta={"user_id": "u1"})
    return provider, res


def _tools():
    from quest_ai_runner.the_world.tools.tools import ToolResult, ToolRegistry, ToolSpec
    registry = Registry()

    def make(name):
        return ToolSpec(name=name, description="d", mutates=False, handler=lambda args, ctx: (lambda r: ToolResult(ok=r.ok, data=r.data, text=r.text))(registry.invoke(name, args, ctx)))
    return ToolRegistry([make(n) for n in ("read_spec_records", "read_metric_source", "list_quest_goals", "find_quest")])


def test_a_status_question_a_status_script_and_a_my_priority_question_each_get_the_snapshot_and_no_planner_loop():
    shapes = [("Say where the quest stands.", '{"asks_status": true}', "where are we with the big quest?"),
              ("A script to read out.", '{"asks_status": true, "read_aloud": true}', "what should i say on my call about where we are, keep it short"),
              ("What my priority is.", '{"asks_status": true, "actor": "me"}', "what is my priority right now")]
    for restated, flags, message in shapes:
        provider, res = run_status(restated + "\n" + flags, message)
        assert res.kind == "answer" and provider.planner_calls == 0
        assert any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
        assert provider.writer_prompts and "STATUS FROM A SNAPSHOT" in provider.writer_prompts[0]
        assert ("READ ALOUD" in provider.writer_prompts[0]) == ("read_aloud" in flags)
        assert ("Unrelated task" in provider.writer_prompts[0]) == ('"actor": "me"' in flags)


class Placement:
    def __init__(self, picked):
        self.picked, self.asked = picked, []

    def place(self, message, previous="", home_quest_id=None, max_quests=1):
        self.asked.append(message)
        return {"picked": self.picked, "ranked": self.picked}


def test_a_status_question_in_a_chat_with_no_quest_is_placed_on_a_quest_and_gets_its_snapshot_or_asks_one_short_question():
    placement = Placement(["q1"])
    provider, res = run_status('Where it stands.\n{"asks_status": true}', "where are we with the release?", quest_id=None, placement=placement)
    assert placement.asked and res.kind == "answer" and provider.planner_calls == 0
    assert any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
    abstain = Placement([])
    provider, res = run_status('Where it stands.\n{"asks_status": true}', "where are we with the release?", quest_id=None, placement=abstain)
    assert abstain.asked and provider.planner_calls == 0 and not any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
    assert res.kind == "answer" and "STATUS FROM A SNAPSHOT" not in "".join(provider.writer_prompts)


class JudgeProvider(StatusProvider):
    """The request analyser leaves the status report out; the reach judge (a second, stronger structured report) makes it."""

    def __init__(self, verdict):
        super().__init__("What is the top priority.")
        self.verdict = verdict

    def plan(self, prompt, *, model, tool_schema, tier=None):
        if "Decide ONE thing" in prompt:
            return dict(self.verdict)
        return super().plan(prompt, model=model, tool_schema=tool_schema)


def run_judged(verdict, message="whats our top priority right now?"):
    provider = JudgeProvider(verdict)
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider, registry=ModelRegistry(provider), tools=_tools(),
                        config=OrchestratorConfig(overseer=True, planner_reach_judge=True, read_reach_summary="the quest's own records"))
    res = orch.run(message, mode=Mode.LIVE, sink=RecordingSink(), quest_id="q1", context_meta={"user_id": "u1"})
    return provider, res


def test_a_status_question_the_analyser_did_not_flag_gets_the_snapshot_when_the_reach_judge_reports_the_status_shape():
    provider, res = run_judged({"reach": "inside", "asks_status": True, "asks_change": False})
    assert res.kind == "answer" and provider.planner_calls == 0
    assert any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
    assert provider.writer_prompts and "STATUS FROM A SNAPSHOT" in provider.writer_prompts[0]


def test_the_reach_judge_report_starts_no_snapshot_for_an_order_or_a_turn_it_did_not_flag():
    for verdict in ({"reach": "inside", "asks_status": True, "asks_change": True}, {"reach": "inside", "asks_status": False}, {"reach": "inside"}):
        provider, res = run_judged(verdict)
        assert not any("STATUS SNAPSHOT" in o.get("text", "") for o in res.gathered)
        assert provider.planner_calls >= 1


def test_a_quest_with_hundreds_of_goals_keeps_its_priority_steps_past_the_first_page():
    class Paged(Registry):
        def invoke(self, name, args, ctx):
            if name != "list_quest_goals":
                return super().invoke(name, args, ctx)
            filler = [{"id": f"f{i}", "title": f"Filler {i}", "text": "", "due": "2026-10-01", "spec_solution_id": None, "completed": False, "abandoned": False} for i in range(250)]
            keyed = [{"id": "k1", "title": "First milestone next step: week of 2026-11-02", "text": "", "due": "2026-12-30", "spec_solution_id": "s_a", "completed": False, "abandoned": False}]
            rows = filler + keyed
            if args.get("text_contains"):
                rows = [g for g in rows if args["text_contains"] in g["title"]]
            return SimpleNamespace(ok=True, data=rows[args.get("offset", 0):args.get("offset", 0) + args["limit"]], text="")
    snap = status_snapshot.build(Paged(), "q1", "u1")
    assert "First milestone next step" in snap["text"]
