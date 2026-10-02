"""The quest-data ladder guard: a change to the person's own Quest data must never land on a rung
that can only edit files, and prefers the governed-operations rung over the full agent.
"""
from quest_ai_runner.core.adapters import (
    WRITE_SURFACE_AGENT,
    WRITE_SURFACE_FILES,
    WRITE_SURFACE_OPERATIONS,
)
from quest_ai_runner.core.orchestrator import (
    QUEST_DATA_WRITE_SURFACE_INSTRUCTION,
    OrchestratorConfig,
    apply_quest_data_ladder_guard,
    ladder_for_quest_data,
    normalize_decision,
)


class Rung:
    def __init__(self, name, surface=None):
        self.name = name
        if surface is not None:
            self.write_surface = surface

    def __repr__(self):
        return self.name


files = Rung("files", WRITE_SURFACE_FILES)
ops = Rung("ops", WRITE_SURFACE_OPERATIONS)
agent = Rung("agent", WRITE_SURFACE_AGENT)
undeclared = Rung("undeclared")


def test_file_editing_rung_is_dropped():
    assert ladder_for_quest_data([files, agent]) == [agent]


def test_operations_rung_preferred_over_agent():
    assert ladder_for_quest_data([files, ops, agent]) == [ops]


def test_undeclared_rung_and_none_count_as_agent():
    assert ladder_for_quest_data([files, undeclared]) == [undeclared]
    assert ladder_for_quest_data([None]) == [None]


def test_only_file_rungs_leaves_an_empty_ladder():
    assert ladder_for_quest_data([files]) == []


def test_guard_narrows_default_ladder_without_instruction_when_ops_survives():
    ladder, brief = apply_quest_data_ladder_guard([files, ops, agent], "BRIEF", deliberate_choice=False)
    assert ladder == [ops]
    assert brief == "BRIEF"


def test_guard_adds_instruction_when_no_ops_rung_survives():
    ladder, brief = apply_quest_data_ladder_guard([files, agent], "BRIEF", deliberate_choice=False)
    assert ladder == [agent]
    assert brief == "BRIEF" + QUEST_DATA_WRITE_SURFACE_INSTRUCTION


def test_deliberate_file_rung_is_not_rerouted_but_is_told_the_tool():
    ladder, brief = apply_quest_data_ladder_guard([files], "BRIEF", deliberate_choice=True)
    assert ladder == [files]
    assert brief == "BRIEF" + QUEST_DATA_WRITE_SURFACE_INSTRUCTION


def test_deliberate_agent_rung_is_kept_and_told_the_tool():
    ladder, brief = apply_quest_data_ladder_guard([agent], "BRIEF", deliberate_choice=True)
    assert ladder == [agent]
    assert brief.endswith(QUEST_DATA_WRITE_SURFACE_INSTRUCTION)


def test_deliberate_ops_rung_needs_no_instruction():
    ladder, brief = apply_quest_data_ladder_guard([ops], "BRIEF", deliberate_choice=True)
    assert ladder == [ops]
    assert brief == "BRIEF"


def test_a_ladder_of_only_file_rungs_becomes_the_nothing_wired_placeholder():
    # Dropping the only rung must not leave an empty ladder: the call site reads ladder[-1], and
    # ``[None]`` is the existing "nothing can execute this" placeholder that reports honestly.
    ladder, brief = apply_quest_data_ladder_guard([files], "BRIEF", deliberate_choice=False)
    assert ladder == [None]
    assert brief.endswith(QUEST_DATA_WRITE_SURFACE_INSTRUCTION)


# ---------------------------------------------------------------------------
# The planner field the guard reads: strict values, fail-safe on anything else.
# ---------------------------------------------------------------------------

def test_deep_target_parses_known_values():
    for value in ("quest_data", "code_or_files", "other", " Quest_Data "):
        d = normalize_decision(
            {"action": "deep", "rationale": "r", "goal": "g", "deep_target": value},
            OrchestratorConfig())
        assert d.deep_target == value.strip().lower()


def test_deep_target_absent_or_garbage_is_none():
    for raw in ({}, {"deep_target": None}, {"deep_target": "quest"}, {"deep_target": 7},
                {"deep_target": ""}):
        d = normalize_decision({"action": "deep", "rationale": "r", "goal": "g", **raw},
                               OrchestratorConfig())
        assert d.deep_target is None
