"""A task's metric result comes from the task's own quest specification, never from the AI system's own records.

Offline: the quest specification is a stub, no network and no model.
"""
from quest_ai_runner.the_intelligence.critical_thinking.metric_translation import task_metric_scope as scope
from quest_ai_runner.the_intelligence.critical_thinking.metric_translation import tools_metric_results as tmr
from quest_ai_runner.the_world.tools.tools import ToolContext, build_tool_registry

SYSTEM_METRIC = "metric-answer-a-turn"
SYSTEM_SOLUTION = "quest-self"
SPEC = {"solutions": {"esr_s1": {"name": "Weekly newsletter", "authoritative_metric_id": "esr_m1"}},
        "metrics": {"esr_m1": {"name": "Open rate", "solution_ids": ["esr_s1"]}}}


def test_the_system_ids_are_known():
    ids = scope.system_record_ids()
    assert SYSTEM_METRIC in ids and SYSTEM_SOLUTION in ids and "esr_m1" not in ids


def test_a_system_metric_is_refused_unless_the_quest_holds_it():
    assert scope.refusal("", SYSTEM_METRIC, SPEC)
    assert scope.refusal(SYSTEM_SOLUTION, "esr_m1", SPEC)
    assert scope.refusal("", SYSTEM_METRIC, None)            # an unreadable specification cannot show it belongs
    assert not scope.refusal("esr_s1", "esr_m1", SPEC)
    held = {"solutions": {SYSTEM_SOLUTION: {"name": "x"}}, "metrics": {SYSTEM_METRIC: {"name": "y"}}}
    assert not scope.refusal(SYSTEM_SOLUTION, SYSTEM_METRIC, held)    # work on the AI system, tracked in a quest that holds it


def test_the_payload_loses_the_system_entry_and_the_kept_one_names_its_solution():
    entries = [{"metric_id": SYSTEM_METRIC, "solution_id": SYSTEM_SOLUTION, "source": "recorded", "before": 0.9, "after": 0.9},
               {"metric_id": "esr_m1", "solution_id": "", "source": "recorded", "before": 0.2, "after": 0.3},
               {"metric_id": "", "solution_id": SYSTEM_SOLUTION, "source": "not_applicable", "reason": "a draft"}]
    kept, refused = scope.scope_entries(entries, "quest_1", SPEC)
    assert refused == 1 and len(kept) == 2
    assert kept[0]["solution_id"] == "esr_s1" and kept[0]["quest_id"] == "quest_1" and kept[0]["solution_name"] == "Weekly newsletter"
    assert kept[1]["solution_id"] == "" and kept[1]["reason"] == "a draft"


def test_the_brief_lists_the_quests_own_solutions():
    block = scope.quest_solutions_block(SPEC, "quest_1")
    assert "esr_s1" in block and "Open rate" in block and SYSTEM_METRIC not in block


def test_the_slice_of_a_system_solution_is_not_given():
    class Slice:
        solution_id, metric_id = SYSTEM_SOLUTION, SYSTEM_METRIC

    class Inner:
        def slice_for(self, request, working_dir=None):
            return Slice()
    assert scope.QuestScopedSource(Inner(), lambda: SPEC).slice_for("x") is None
    assert scope.QuestScopedSource(Inner(), lambda: {"solutions": {SYSTEM_SOLUTION: {}}, "metrics": {SYSTEM_METRIC: {}}}).slice_for("x") is not None


def test_the_tool_refuses_a_system_metric(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(scope, "read_quest_spec", lambda env, quest_id: SPEC)
    registry = build_tool_registry({"QAR_METRIC_LEDGER_DIR": str(tmp_path / "ledger")})
    bad = registry.invoke(tmr.TOOL_NAME, {"metric_id": SYSTEM_METRIC, "before": 0.9, "after": 0.9},
                          ToolContext(task_id="t1", quest_id="quest_1", meta={"run_id": "r1"}))
    assert not bad.ok and "AI system's own records" in bad.text
    good = registry.invoke(tmr.TOOL_NAME, {"metric_id": "esr_m1", "before": 0.2, "after": 0.3},
                           ToolContext(task_id="t1", quest_id="quest_1", meta={"run_id": "r1"}))
    assert good.ok and len(registry.metric_ledger.for_run("r1")) == 1
