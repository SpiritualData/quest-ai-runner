"""A tool name the planner made up is refused with the next step: read the records through the capability menu, never answer that nothing is recorded."""
from hubofhubs.api import ExecutionRecord

from quest_ai_runner.intelligence.mind.orchestrator import Orchestrator
from quest_ai_runner.intelligence.mind.context_intake.orchestrator_reads import NOT_A_TOOL_NEXT
from quest_ai_runner.world.tools.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry

from .conftest import StubProvider, StubRetrieval


class Silent:
    def status(self, *a, **k): pass
    def emit(self, *a, **k): pass


def test_a_made_up_entries_tool_points_the_replan_to_the_operations():
    spec = ToolSpec(name="get_spec", description="d", mutates=False, handler=lambda args, ctx: ToolResult(ok=True, text="spec"))
    orch = Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]), registry=ModelRegistry(StubProvider([])), tools=ToolRegistry([spec]))
    gathered = []
    orch.run_tool_calls([{"name": "list_collection_entries", "args": {"collection_id": "c1"}}], gathered=gathered,
                        exec_record=ExecutionRecord(), tool_ok_signatures={}, emit=Silent(), steps=1, ctx=ToolContext())
    note = gathered[0]
    assert note["planner_only"] and "NOT RUN" in note["text"] and NOT_A_TOOL_NEXT in note["text"]
    assert "list_operations" in NOT_A_TOOL_NEXT and "nothing is recorded" in NOT_A_TOOL_NEXT
