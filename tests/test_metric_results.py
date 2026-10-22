"""The metric record: the result type, the ledger two processes share, and the tool a worker records with.

ESOI asks every piece of work to say what the relevant metric was before and after the change. These tests pin the part of
that which is plain data and plain files: a number is a number and nothing a model typed is converted into one, a ledger
append from several threads and processes never tears a line, the rows of different sources are reconciled without one
overwriting another, and the tool refuses what is not a result. Offline: no model, no network.
"""
import math
import os
import threading
from concurrent.futures import ProcessPoolExecutor

import pytest

from quest_ai_runner.core import answer_explanation
from quest_ai_runner.core.metric_results import (
    MAX_METRICS, NOT_RECORDED_LINE, SOURCE_MEASURED, SOURCE_NOT_APPLICABLE, SOURCE_RECORDED, FileMetricLedger,
    MetricResult, metric_ledger_from_env, payload_of, reconcile, render_line)
from quest_ai_runner.core.tools import ToolContext, build_tool_registry

PAYLOAD_KEYS = {"solution_id", "metric_id", "name", "milestone", "before", "after", "delta", "source", "reason",
                "disagrees"}


def measured(**kw):
    return MetricResult(source=SOURCE_MEASURED, run_id="r1", metric_id="metric-x", solution_id="sol-x",
                        name="Pass rate", **kw)


def recorded(**kw):
    return MetricResult(source=SOURCE_RECORDED, run_id="r1", metric_id="metric-x", **kw)


# -- the result type ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0.8", "80%", "nan", True, False, [1], {"a": 1}, math.nan, math.inf, -math.inf])
def test_a_value_that_is_not_a_finite_number_is_rejected_not_converted(bad):
    with pytest.raises(ValueError):
        measured(before=bad)
    with pytest.raises(ValueError):
        measured(after=bad)


def test_ints_and_floats_are_kept_as_floats_and_the_delta_is_computed():
    result = measured(before=2, after=2.5)
    assert (result.before, result.after, result.delta) == (2.0, 2.5, 0.5)
    assert measured(after=1).delta is None   # a delta needs both ends


def test_a_result_needs_a_value_unless_it_is_not_applicable():
    with pytest.raises(ValueError):
        measured()
    with pytest.raises(ValueError):
        MetricResult(source="guessed", before=1)


def test_not_applicable_needs_a_reason_and_carries_no_values():
    with pytest.raises(ValueError, match="reason"):
        MetricResult(source=SOURCE_NOT_APPLICABLE, run_id="r1")
    with pytest.raises(ValueError, match="values"):
        MetricResult(source=SOURCE_NOT_APPLICABLE, run_id="r1", reason="a draft", before=1)
    assert MetricResult(source=SOURCE_NOT_APPLICABLE, run_id="r1", reason="  a draft  ").reason == "a draft"


def test_the_consumer_entry_has_exactly_the_documented_keys_and_the_row_adds_the_run():
    result = measured(before=0.5, after=0.75)
    assert set(result.to_dict()) == PAYLOAD_KEYS
    assert set(result.to_row()) == PAYLOAD_KEYS | {"run_id", "task_id", "ts"}
    assert MetricResult.from_row(result.to_row()) == result
    with pytest.raises(ValueError):
        MetricResult.from_row({"source": "measured", "before": "x"})


def test_free_text_is_cut_so_a_line_stays_small():
    result = MetricResult(source=SOURCE_RECORDED, run_id="r1", metric_id="m", before=1, name="n" * 1000,
                          milestone="m" * 1000)
    assert len(result.name) <= 160 and len(result.milestone) <= 300


def test_the_payload_cap_is_the_explanation_cap():
    assert MAX_METRICS == answer_explanation.MAX_ITEMS_PER_KIND


# -- the ledger --------------------------------------------------------------------------------

def test_rows_roundtrip_under_their_run_and_an_unknown_run_is_empty(tmp_path):
    ledger = FileMetricLedger(str(tmp_path))
    assert ledger.record(measured(before=0.5)) and ledger.record(recorded(after=0.9))
    assert [r.source for r in ledger.for_run("r1")] == [SOURCE_MEASURED, SOURCE_RECORDED]
    assert ledger.for_run("other") == [] and ledger.for_run("") == []
    assert not ledger.record(MetricResult(source=SOURCE_RECORDED, metric_id="m", before=1))   # no run id: nowhere to keep it


def test_a_malformed_line_is_skipped_not_fatal(tmp_path):
    ledger = FileMetricLedger(str(tmp_path))
    ledger.record(measured(before=0.5))
    with open(ledger.files.path_for("r1"), "a", encoding="utf-8") as fh:
        fh.write("not json\n{\"source\": \"measured\", \"before\": \"text\"}\n[1]\n")
    ledger.record(recorded(after=0.9))
    assert [r.source for r in ledger.for_run("r1")] == [SOURCE_MEASURED, SOURCE_RECORDED]


def test_threads_appending_together_never_tear_a_line(tmp_path):
    ledger = FileMetricLedger(str(tmp_path))
    barrier = threading.Barrier(8)

    def writer(n):
        barrier.wait()
        for i in range(25):
            assert ledger.record(MetricResult(source=SOURCE_RECORDED, run_id="r1", metric_id=f"m{n}-{i}",
                                              before=float(i), reason="x" * 200))

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(ledger.for_run("r1")) == 8 * 25


def write_rows(directory, n):
    ledger = FileMetricLedger(directory)
    for i in range(30):
        ledger.record(MetricResult(source=SOURCE_RECORDED, run_id="shared", metric_id=f"p{n}-{i}", after=float(i)))


def test_processes_appending_together_share_one_run(tmp_path):
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(write_rows, [str(tmp_path)] * 4, range(4)))
    assert len(FileMetricLedger(str(tmp_path)).for_run("shared")) == 4 * 30


def test_a_new_ledger_file_has_the_permissions_an_ordinary_write_gets(tmp_path):
    old = os.umask(0o027)
    try:
        ledger = FileMetricLedger(str(tmp_path / "fresh"))
        ledger.record(measured(before=1))
    finally:
        os.umask(old)
    assert os.stat(ledger.files.path_for("r1")).st_mode & 0o777 == 0o640   # 0666 minus the umask, never mkstemp's 0600


def test_the_directory_comes_from_the_environment(tmp_path):
    assert metric_ledger_from_env({"QAR_METRIC_LEDGER_DIR": str(tmp_path)}).directory == str(tmp_path)
    assert metric_ledger_from_env({}).directory.endswith("qar-metric-ledger")


# -- reconciling rows --------------------------------------------------------------------------

def test_a_measured_before_and_the_later_after_become_one_row():
    rows = [measured(before=0.5, ts=1.0), measured(after=0.75, ts=2.0)]
    (only,) = reconcile(rows)
    assert (only.before, only.after, only.delta, only.disagrees) == (0.5, 0.75, 0.25, False)


def test_measured_and_recorded_stay_two_rows_and_disagreement_flags_both():
    out = reconcile([measured(before=0.5, after=0.75), recorded(before=0.5, after=0.9)])
    assert [r.source for r in out] == [SOURCE_MEASURED, SOURCE_RECORDED]
    assert all(r.disagrees for r in out)
    assert (out[0].after, out[1].after) == (0.75, 0.9)   # neither overwrote the other


def test_agreement_flags_nothing_and_an_unshared_end_is_not_a_disagreement():
    out = reconcile([measured(before=0.5, after=0.75), recorded(before=0.5)])
    assert not any(r.disagrees for r in out)


def test_not_applicable_rows_are_kept_apart_from_values_and_deduplicated_by_reason():
    na = lambda reason: MetricResult(source=SOURCE_NOT_APPLICABLE, run_id="r1", reason=reason)  # noqa: E731
    out = reconcile([na("a draft"), recorded(after=1), na("a draft"), na("an edit")])
    assert [r.source for r in out] == [SOURCE_RECORDED, SOURCE_NOT_APPLICABLE, SOURCE_NOT_APPLICABLE]


def test_the_payload_is_capped():
    rows = [MetricResult(source=SOURCE_RECORDED, run_id="r1", metric_id=f"m{i}", after=1) for i in range(MAX_METRICS + 5)]
    assert len(reconcile(rows)) == MAX_METRICS


def test_the_text_line_is_written_from_the_payload():
    payload = payload_of(reconcile([measured(before=0.5, after=0.75)]))
    assert render_line(payload) == "Metric: Pass rate 0.5 -> 0.75 (measured)"
    assert render_line([]) == "" and render_line([], missing=True) == NOT_RECORDED_LINE
    na = payload_of([MetricResult(source=SOURCE_NOT_APPLICABLE, run_id="r1", reason="a draft")])
    assert render_line(na) == "Metric: not applicable (a draft)"
    flagged = payload_of(reconcile([measured(after=0.75), recorded(after=0.9)]))
    assert "differs" in render_line(flagged)
    for text in (render_line(payload), render_line(na), NOT_RECORDED_LINE):
        assert "—" not in text


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
    from quest_ai_runner.tools.__main__ import main
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
