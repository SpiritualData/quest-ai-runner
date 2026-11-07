"""The two places text leaves the AI (the final reply, a tool that writes the person's data) both apply the hub's em dash rule
(the rule itself is tested in the hub). Offline; no model."""
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

D = "—"


def test_a_tool_that_changes_data_receives_text_without_a_dash_and_a_read_is_left_alone():
    got = []
    properties = {"type": "object", "properties": {"text": {"type": "string"}}}
    write = ToolSpec(name="add_note", description="d", mutates=True, parameters=properties,
                     handler=lambda args, ctx: got.append(args["text"]) or ToolResult(ok=True, text="ok"))
    read = ToolSpec(name="search_notes", description="d", mutates=False, parameters=properties,
                    handler=lambda args, ctx: got.append(args["text"]) or ToolResult(ok=True, text="ok"))
    registry = ToolRegistry([write, read])
    registry.invoke("add_note", {"text": f"Cleared cycling {D} no running."}, ToolContext())
    registry.invoke("search_notes", {"text": f"cycling {D} running"}, ToolContext())
    assert got == ["Cleared cycling, no running.", f"cycling {D} running"]


def test_the_final_reply_of_a_turn_carries_no_em_dash():
    from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry
    from quest_ai_runner.the_intelligence.mind.orchestrator import Orchestrator, OrchestratorConfig

    from .conftest import StubProvider, StubRetrieval

    provider = StubProvider([{"action": "answer", "user_intent": "ask", "rationale": "r", "pending_read": []}],
                            answer_text=f"Quest can help {D} start with one goal {D} and grow from there.")
    config = OrchestratorConfig()
    config.overseer = False
    result = Orchestrator(retrieval=StubRetrieval(), provider=provider, registry=ModelRegistry(provider), config=config).run("Can Quest help?")
    assert result.kind == "answer" and D not in result.text
    assert result.text.startswith("Quest can help (start with one goal) and grow")
