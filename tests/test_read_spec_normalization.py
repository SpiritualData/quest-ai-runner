"""A planner read that is a structured lookup without its "query" wrapper still runs.

Before 2026-10-07 ``normalize_decision`` kept a read only when it carried a known surface key, so a
named read operation written top-level (``{"operation": ..., "args": ...}``, the shape a
consumer's own discovery text can show) was dropped: the "read" step ran nothing and the turn
answered from no data.
"""
from quest_ai_runner.core.orchestrator import OrchestratorConfig, normalize_decision


def plan(reads):
    return {"action": "read", "reads": reads, "rationale": "r", "user_intent": "ask"}


def test_top_level_structured_lookup_is_handed_to_the_query_reader_whole():
    spec = {"operation": "list_items", "args": {"container_id": "c1"}}
    decision = normalize_decision(plan([spec]), OrchestratorConfig())
    assert decision.action == "read"
    assert decision.reads == [{"query": spec}]


def test_known_shapes_are_unchanged():
    reads = [{"grep": "x"}, {"query": {"text": "y"}}, {"cards": "z"}]
    decision = normalize_decision(plan(reads), OrchestratorConfig())
    assert decision.reads == reads


def test_a_surface_this_turn_lacks_is_still_dropped_not_rerouted():
    decision = normalize_decision(plan([{"web": "news"}, {"tools": "send"}]), OrchestratorConfig(),
                                  web_enabled=False, tools_enabled=False)
    assert decision.reads == []


def test_empty_and_non_dict_reads_are_dropped():
    decision = normalize_decision(plan([{}, "text", None]), OrchestratorConfig())
    assert decision.reads == []
