"""The metric record as QAR wires it: the ledger folder, the cap pinned to the explanation, and the tool a worker records with.

The row, the ledger file and the reconciling are the esoi library's (``esoi.optimization.keep_metric_record.metric_results``) and are tested there. What is
QAR's own is where the ledger lives (``QAR_METRIC_LEDGER_DIR``), that the payload cap equals the answer explanation's, and the
tool that refuses what is not a result. Offline: no model, no network.
"""
import os

import pytest
from esoi import MAX_METRICS, SOURCE_NOT_APPLICABLE, SOURCE_RECORDED, FileMetricLedger

from quest_ai_runner.the_intelligence.mind.response_writer import answer_explanation
from quest_ai_runner.the_world.tools.tools import ToolContext, build_tool_registry
from quest_ai_runner.the_intelligence.critical_thinking.metric_translation.tools_metric_results import metric_ledger_from_env

PAYLOAD_KEYS = {"solution_id", "metric_id", "name", "milestone", "before", "after", "delta", "source", "reason",
                "disagrees"}


def test_the_payload_cap_is_the_explanation_cap():
    assert MAX_METRICS == answer_explanation.MAX_ITEMS_PER_KIND


def test_the_directory_comes_from_the_environment(tmp_path):
    assert metric_ledger_from_env({"QAR_METRIC_LEDGER_DIR": str(tmp_path)}).directory == str(tmp_path)
    assert os.path.basename(metric_ledger_from_env({}).directory).startswith("qar-metric-ledger")


# -- the tool a worker calls -------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)   # a bare folder: no specification records around
    return build_tool_registry({"QAR_METRIC_LEDGER_DIR": str(tmp_path / "ledger")})


def call(registry, args, **ctx):
    return registry.invoke("record_metric_result", args, ToolContext(**ctx))


def test_the_tool_keeps_a_recorded_result_under_the_run_id(registry):
    out = call(registry, {"metric_id": "metric-x", "before": 0.5, "after": 0.75, "milestone": "first"},
               task_id="task_1", meta={"run_id": "run_a"})
    assert out.ok and "Metric: metric-x 0.5 -> 0.75 (recorded)" in out.text
    (row,) = registry.metric_ledger.for_run("run_a")
    assert (row.source, row.run_id, row.task_id, row.metric_id, row.milestone) == (SOURCE_RECORDED, "run_a", "task_1",
                                                                                 "metric-x", "first")
    assert registry.metric_ledger.for_run("run_b") == []


def test_with_only_the_task_the_result_is_kept_under_the_task(registry):
    assert call(registry, {"metric_id": "m", "after": 1}, task_id="task_1").ok
    assert len(registry.metric_ledger.for_run("task_1")) == 1


def test_without_any_id_the_tool_says_what_to_add(registry):
    out = call(registry, {"metric_id": "m", "after": 1})
    assert not out.ok and "--run" in out.text


def test_not_applicable_needs_a_reason(registry):
    meta = {"run_id": "run_a"}
    assert not call(registry, {"not_applicable": True}, meta=meta).ok
    assert not call(registry, {"not_applicable": True, "reason": "   "}, meta=meta).ok
    assert call(registry, {"not_applicable": True, "reason": "Edited a draft; no metric measures it."}, meta=meta).ok
    (row,) = registry.metric_ledger.for_run("run_a")
    assert row.source == SOURCE_NOT_APPLICABLE and row.before is None


@pytest.mark.parametrize("args", [
    {"metric_id": "m", "before": "0.5"},
    {"metric_id": "m", "after": True},
    {"metric_id": "m", "after": float("nan")},
    {"metric_id": "m"},
    {"before": 1, "after": 2},
    {"not_applicable": True, "reason": "a draft", "after": 1},
])
def test_the_tool_refuses_what_is_not_a_result(registry, args):
    out = call(registry, args, meta={"run_id": "run_a"})
    assert not out.ok and out.text.startswith("Not recorded")
    assert registry.metric_ledger.for_run("run_a") == []


def test_the_tool_is_a_standing_deep_operation_and_never_a_chat_one(registry):
    spec = registry.get("record_metric_result")
    assert spec.standing_deep and spec.on_request_only and not spec.mutates and spec.workflow_step is False
    assert "- record_metric_result (" in registry.render_deep_block("anything at all")
    assert "record_metric_result" not in registry.render_planner_block("anything at all")


def test_a_worker_calls_it_from_the_shell_with_a_run_id(tmp_path, monkeypatch, capsys):
    from quest_ai_runner.the_consciousness.terminal.__main__ import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QAR_STANDARD_TOOLS", "1")   # the test suite turns the standard tools off unless asked
    monkeypatch.setenv("QAR_METRIC_LEDGER_DIR", str(tmp_path / "ledger"))
    code = main(["call", "record_metric_result", "--run", "run_s", "--kw", "metric_id=metric-x",
                 "--kw", "before=0.25", "--kw", "after=0.5"])
    assert code == 0 and "(recorded)" in capsys.readouterr().out
    assert main(["call", "record_metric_result", "--run", "run_s", "--kw", "metric_id=m", "--kw", "after=high"]) == 1
    assert [r.after for r in FileMetricLedger(str(tmp_path / "ledger")).for_run("run_s")] == [0.5]
    monkeypatch.setenv("QAR_TOOL_RUN_ID", "run_env")
    assert main(["call", "record_metric_result", "--kw", "not_applicable=true", "--kw", "reason=a note"]) == 0
    assert len(FileMetricLedger(str(tmp_path / "ledger")).for_run("run_env")) == 1


# -- naming a quest metric, and which metric a run records ---------------------------------------

def test_a_quest_metric_is_named_from_the_quest_records_when_no_repository_holds_it(tmp_path, monkeypatch):
    """A result recorded against a quest's own metric (an ``esr_`` record) carries its name, so a screen never shows the id."""
    from quest_ai_runner.the_intelligence.critical_thinking.metric_translation import tools_metric_results as tmr
    monkeypatch.chdir(tmp_path)
    seen = []

    def lookup(metric_id):
        seen.append(metric_id)
        return {"name": "Paid subscribers", "solution_id": "esr_sol"}

    row = tmr.build_result({"metric_id": "esr_m1", "before": 0, "after": 0, "reason": "read live"}, "run1", "", lookup)
    assert (row.name, row.solution_id, seen) == ("Paid subscribers", "esr_sol", ["esr_m1"])
    given = tmr.build_result({"metric_id": "esr_m1", "solution_id": "esr_given", "after": 1}, "run1", "", lookup)
    assert given.solution_id == "esr_given"   # what the call said wins over the lookup


def test_a_failed_quest_lookup_still_keeps_the_result(tmp_path, monkeypatch):
    from quest_ai_runner.the_intelligence.critical_thinking.metric_translation import tools_metric_results as tmr
    monkeypatch.chdir(tmp_path)
    assert tmr.quest_metric_names({}, "quest_1", "esr_m1") == {}            # no Quest credentials: nothing is asked
    assert tmr.quest_metric_names({"QUEST_BASE_URL": "x", "QUEST_API_KEY": "y"}, None, "esr_m1") == {}
    row = tmr.build_result({"metric_id": "esr_m1", "after": 2}, "run1", "", lambda _id: {})
    assert (row.metric_id, row.name, row.after) == ("esr_m1", "", 2)


def test_every_place_that_asks_for_a_metric_says_which_one(registry):
    """The rule that the metric is the changed solution's, not a downstream outcome, reaches the worker three ways."""
    from quest_ai_runner.the_intelligence.critical_thinking.metric_translation.goal_metrics import CLOSING, NUDGE_BRIEF
    from quest_ai_runner.the_intelligence.critical_thinking.metric_translation.tools_metric_results import WHICH_METRIC
    assert "solution your change implements" in WHICH_METRIC and "defining one" in WHICH_METRIC
    assert WHICH_METRIC in CLOSING and WHICH_METRIC in NUDGE_BRIEF
    assert WHICH_METRIC in registry.get("record_metric_result").when_to_use
    assert "—" not in WHICH_METRIC
