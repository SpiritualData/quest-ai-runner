"""The em dash pass: text with a dash comes out with the punctuation its context needs, never a hyphen glued to words, and the
two places text leaves the AI (the final reply, a tool that writes the person's data) both use it. Offline; no model."""
from quest_ai_runner.the_intelligence.critical_thinking.response_quality.em_dash_pass import clean_value, remove_em_dashes
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec

D = "—"


def test_a_single_dash_between_clauses_becomes_a_comma():
    assert remove_em_dashes(f"Saved the note {D} easy cycling from Monday.") == "Saved the note, easy cycling from Monday."
    assert remove_em_dashes(f"It is done{D}really.") == "It is done, really."


def test_a_pair_of_dashes_wraps_an_aside_in_parentheses():
    assert remove_em_dashes(f"The plan {D} which you wrote {D} works.") == "The plan (which you wrote) works."


def test_a_bullet_label_gets_a_colon_and_a_number_range_reads_as_a_range():
    assert remove_em_dashes(f"- Goal {D} run 5k") == "- Goal: run 5k"
    assert remove_em_dashes(f"Budget is 10{D}20 dollars") == "Budget is 10 to 20 dollars"


def test_a_dash_at_the_edge_of_a_line_is_dropped_and_no_hyphen_is_ever_produced():
    assert remove_em_dashes(f"Wait {D}") == "Wait"
    assert remove_em_dashes(f"{D} first") == "first"
    for text in (f"Yes {D} Quest can help {D} start with a goal.", f"A {D} b {D} c {D} d", f"Done. {D} Next."):
        out = remove_em_dashes(text)
        assert D not in out and " - " not in out and "-" not in out


def test_text_without_a_dash_is_returned_untouched():
    for text in ("Plain sentence, with a comma.", "A well-known range 10-20.", "", "Line one\n\nLine two"):
        assert remove_em_dashes(text) == text
    assert remove_em_dashes(None) is None


def test_values_are_cleaned_through_lists_and_dicts_but_ids_are_left_alone():
    cleaned = clean_value({"text": f"a {D} b", "tags": [f"x {D} y"], "quest_id": f"q{D}1", "n": 3})
    assert cleaned == {"text": "a, b", "tags": ["x, y"], "quest_id": f"q{D}1", "n": 3}


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
