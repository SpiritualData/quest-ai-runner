"""A milestone turn's messages and reads: message 1 says the work is made, never a step recorded for someone; the planner's own source read is taken out so the
source is read once by code; no planner fact text reaches message 1; record text is never cut inside a word; and no blocked ask is filed once the turn's work is
kept. Synthetic records and a fake tool registry stand in for Quest."""
import datetime
import json
import re
from types import SimpleNamespace

import pytest

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application import milestone_cue as cue
from quest_ai_runner.the_intelligence.mind.response_writer import act_status
from quest_ai_runner.the_intelligence.mind.response_writer import orchestrator_turn_messages as messages
from quest_ai_runner.the_world.tools.tools import ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.the_world.tools.tools_status import ToolStatus

@pytest.fixture(autouse=True)
def cheap_model(monkeypatch):
    """The turn's one cheap model call, answered offline: the milestone judgment picks the first candidate listed, any other question is answered NONE."""
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator

    def fake(self, prompt):
        found = re.search(r"^(esr_\w+):", prompt, re.M) if "These are milestones of the quest" in prompt else None
        return found.group(1) if found else "NONE"
    monkeypatch.setattr(Orchestrator, "cheap_decision", fake)


TABLES = {
    "solutions": {"esr_c": {"name": "P3 Partner users", "milestones": "100 new users come through a partner.", "owner_user_ids": ["u_ops"],
                            "purpose": "Travis runs partner outreach.", "outputs": "A partner pitch."}},
    "metrics": {"esr_mc": {"name": "P3 users via partner", "record": "0 of 100", "solution_ids": ["esr_c"]}},
}
NOW = datetime.datetime(2026, 11, 4, tzinfo=datetime.timezone.utc)


def tools(calls, goals=(), member="SpiritOps"):
    def write(args, ctx):
        calls.append(dict(args))
        goal = {"id": "g1", "name": args["title"], "assigned_to_user_id": "u_ops", "assignee_name": member, "spec_solution_id": "esr_c", "spec_metric_id": "esr_mc",
                "period": args["period"], "scheduled_date": args["due_date"]}
        return ToolResult(ok=True, status=ToolStatus.APPLIED, data={"state": "created", "goal": goal}, text="Created.")
    return ToolRegistry([
        ToolSpec(name="read_spec_records", description="d", mutates=False, handler=lambda a, c: ToolResult(ok=True, data={"records": TABLES, "time_zone": "UTC"}, text="r")),
        ToolSpec(name="list_quest_goals", description="d", mutates=False, handler=lambda a, c: ToolResult(ok=True, data=list(goals), text="l")),
        ToolSpec(name="create_or_update_quest_goal", description="d", mutates=True, handler=write)])


def resolved(registry):
    return cue.resolve(registry, "q1", "get the partner users moving", NOW, decide=None)


def test_the_first_message_says_the_work_is_made_not_a_step_recorded():
    doing = resolved(tools([])).doing()
    assert doing.startswith("Making the next piece of work") and "Readiness" not in doing and "of 5" not in doing


def test_a_blocked_ask_is_not_filed_once_the_step_is_recorded_in_the_goal():
    res = SimpleNamespace(output="the reply")
    verdict = {"blocker": "needs_person"}
    assert messages.file_blocked_ask(SimpleNamespace(), None, {"milestone_step_recorded": True}, res, verdict, question="Which source?") is False
    assert res.output == "the reply"


SOURCED = {"solutions": TABLES["solutions"],
           "metrics": {"esr_mc": dict(TABLES["metrics"]["esr_mc"], record="Provisional 83.3 percent", source="cov_source", measured_through="2026-10-31")}}


def sourced_tools(calls, reads, goals=(), value="73.8 percent", label="the coverage report"):
    base = tools(calls, goals)
    def read(args, ctx):
        reads.append(dict(args))
        return ToolResult(ok=True, data={"value": value, "as_of": "2026-11-07", "source": label, "detail": ""}, text="read")
    registry = ToolRegistry([base.get("read_spec_records"), base.get("list_quest_goals"), base.get("create_or_update_quest_goal"),
                             ToolSpec(name="read_metric_source", description="d", mutates=False, handler=read)])
    registry.get("read_spec_records").handler = lambda a, c: ToolResult(ok=True, data={"records": SOURCED, "time_zone": "UTC"}, text="r")
    return registry


def answer_for(prompt):
    return json.dumps({"draft": "Pitch.", "human_step": "Send it."})


def test_the_planners_source_read_is_taken_out_so_the_source_is_read_once():
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    calls, reads = [], []
    provider = StubProvider([{"action": "tool", "user_intent": "act", "rationale": "r", "tool_calls": [{"name": "read_metric_source", "args": {}}]}])
    config = OrchestratorConfig()
    config.overseer = False
    Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config, tools=sourced_tools(calls, reads)).run("get the partner users moving", quest_id="q1")
    assert reads == [{"name": "cov_source"}]      # read once, by the progress step's code


def test_the_first_message_never_carries_the_planners_fact_text_even_when_the_plan_still_reads():
    from hubofhubs.api import EVENT_INTERIM
    from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from .conftest import StubProvider, StubRetrieval
    leak = "CUE FACTS: milestone 'P3', solution esr_c, metric source cov_source. Code reads it via read_metric_source"
    provider = StubProvider([{"action": "tool", "user_intent": "act", "rationale": "r", "facts_read": [leak],
                              "tool_calls": [{"name": "list_quest_goals", "args": {"quest_id": "q1"}}]}])
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), tools=sourced_tools([], []),
                        config=OrchestratorConfig(interim_replies=True, overseer=False, min_seconds_between_interims=0.0))
    events = []
    orch.run("get the partner users moving", sink=StreamSink(events.append), quest_id="q1")
    first = next(e for e in events if e["type"] == EVENT_INTERIM)["text"]
    assert "CUE FACTS" not in first and "esr_" not in first and "read_metric_source" not in first and "Current value" in first


def test_record_text_is_never_cut_inside_a_word_or_an_identifier():
    text = "none 344, botswana 14 (existing pool, not the new source), mentee-launch 0, and more words after that"
    for limit in range(20, len(text)):
        cut = cue.clip(text, limit)
        assert len(cut) <= limit and all(token in text.replace(",", " ").replace("(", " ").replace(")", " ").split() or token.rstrip(",)") in text for token in cut.split())
        assert not text.startswith(cut) or text[len(cut):len(cut) + 1] in ("", " ", ",", ";", ".", ":", "(")
    assert cue.first_sentence(text, 60) == "none 344, botswana 14 (existing pool"
    assert cue.first_sentence("Short value. More.") == "Short value"
    assert cue.clip("fits", 10) == "fits"

