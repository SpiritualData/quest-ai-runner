"""An acting tool's receipt carries its whole message, so the answer check can see what was sent."""
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.core.orchestrator_reads import ACTING_TOOL_ARGS_CHARS
from quest_ai_runner.core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from hubofhubs.act.execution_record import ExecutionRecord

from .conftest import StubProvider, StubRetrieval
from quest_ai_runner.core.model_registry import ModelRegistry


def receipt_for(mutates):
    spec = ToolSpec(name="send_it", description="d", mutates=mutates,
                    handler=lambda args, ctx: ToolResult(ok=True, text="sent"))
    orch = Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]),
                        registry=ModelRegistry(StubProvider([])), tools=ToolRegistry([spec]))
    gathered = []
    body = "line. " * 400 + "THE END"
    orch.run_tool_calls([{"name": "send_it", "args": {"body": body}}], gathered=gathered,
                        exec_record=ExecutionRecord(), tool_ok_signatures=set(),
                        emit=_Silent(), steps=1, ctx=ToolContext())
    return gathered[0]["text"]


class _Silent:
    def status(self, *a, **k): pass
    def emit(self, *a, **k): pass


def test_an_acting_tool_receipt_keeps_the_end_of_the_message():
    assert "THE END" in receipt_for(True) and ACTING_TOOL_ARGS_CHARS >= 3000


def test_a_read_receipt_stays_short():
    assert "THE END" not in receipt_for(False)
