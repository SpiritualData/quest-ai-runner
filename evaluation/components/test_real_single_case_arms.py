"""Offline tests for the single-case arms that run a REAL edge instead of a stand-in: a real child process for the deep runner, a real MCP
server process for write-through-mcp and the OpenClaw channel, the installed console script for the terminal, and the live-model arm of
act-in-the-world (never run here). Each real arm must pass its component's whole case set, and a broken variant must fail where the
real edge is the only thing that can tell. No model call, no network. Run:
.venv/bin/python3 -m pytest evaluation/components/test_real_single_case_arms.py -q -p no:cacheprovider"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from esoi import harness  # noqa: E402


def arm(component, name):
    return common.load_impl(name, component)(common.CallMeter(max_calls=0))


def cases(component):
    return {c["id"]: c for c in json.loads((HERE / component / "cases.json").read_text())}


def run(fn, case):
    return fn(case["kind"], case["args"])


def test_real_process_runs_a_real_child_and_is_not_a_patched_popen():
    from deep_runner.impls import real_process
    fn = arm("deep_runner", "real_process")
    popen_before = subprocess.Popen
    clean = run(fn, cases("deep_runner")["DPR-001"])
    assert subprocess.Popen is popen_before
    assert clean == {"met": True, "has_want": True, "decision_id": None, "error_kind": "none", "raised": False}
    assert real_process.WORKER.startswith("#!")


@pytest.mark.parametrize("case_id", ["DPR-006", "DPR-008", "DPR-010", "DPR-011"])
def test_real_process_reports_escalation_partial_missing_and_killed_children(case_id):
    case = cases("deep_runner")[case_id]
    got = run(arm("deep_runner", "real_process"), case)
    for key, want in case["expect"]["eq"].items():
        assert got[key] == want, (case_id, key, got)


def test_real_process_broken_worker_that_ignores_its_exit_code_is_caught(monkeypatch):
    from deep_runner.impls import real_process
    monkeypatch.setattr(real_process, "WORKER", real_process.WORKER.replace("sys.exit(scenario.get(\"exit\", 0))", "sys.exit(0)"))
    got = run(arm("deep_runner", "real_process"), cases("deep_runner")["DPR-009"])
    want = cases("deep_runner")["DPR-009"]["expect"]["eq"]
    assert any(got[key] != value for key, value in want.items())   # a worker that always exits 0 no longer matches the stopped run


def test_real_mcp_server_counts_calls_the_server_received():
    fn = arm("write_through_mcp", "real_stdio_server")
    all_cases = cases("write_through_mcp")
    for case_id in ("MCP-001", "MCP-004", "MCP-010", "MCP-012", "MCP-014"):
        case = all_cases[case_id]
        got = run(fn, case)
        for key, want in case["expect"]["eq"].items():
            assert got[key] == want, (case_id, key, got)


def test_real_mcp_server_broken_allowlist_is_caught_by_the_servers_own_call_log():
    case = cases("write_through_mcp")["MCP-004"]
    args = json.loads(json.dumps(case["args"]))
    args["writable"] = ["create", "delete"]   # the broken variant: the allowlist lets the refused tool through
    args["tools"] = ["create", "delete"]
    got = arm("write_through_mcp", "real_stdio_server")(case["kind"], args)
    assert got["n_calls"] == 1 and got["ok"] is True   # the server really received the write the allowlist should have refused


def test_real_gateway_clamps_the_wait_and_reports_a_dead_gateway_unhealthy():
    fn = arm("talk_over_a_channel", "real_gateway")
    all_cases = cases("talk_over_a_channel")
    for case_id in ("CHN-001", "CHN-007", "CHN-008", "CHN-010", "CHN-015", "CHN-020"):
        case = all_cases[case_id]
        got = run(fn, case)
        for key, want in case["expect"]["eq"].items():
            assert got[key] == want, (case_id, key, got)


def test_real_gateway_broken_baseline_a_live_gateway_is_healthy_where_a_dead_one_is_not():
    fn = arm("talk_over_a_channel", "real_gateway")
    case = cases("talk_over_a_channel")["CHN-008"]
    alive = dict(case["args"], raises=False, content={"events": []})
    assert run(fn, dict(case, args=alive))["healthy"] is True
    assert run(fn, case)["healthy"] is False


def test_real_console_script_exits_with_a_real_code_and_prints_to_a_real_stream():
    fn = arm("operate_from_a_terminal", "real_console_script")
    help_case = cases("operate_from_a_terminal")["CLI-001"]
    got = run(fn, help_case)
    assert got["exit"] == 0 and "usage: quest-ai-runner" in got["text"]
    bad = run(fn, dict(help_case, args=dict(help_case["args"], argv=["no-such-command"])))
    assert bad["exit"] != 0   # broken baseline: a command that does not exist must not exit zero


def test_live_model_arm_of_act_in_the_world_is_paid_and_scored_by_the_components_rule():
    path = HERE / "act_in_the_world" / "impls" / "live_model.py"
    assert harness.declares_paid(path)
    assert harness.declares_paid(HERE / "end_to_end" / "impls" / "qar_live.py")
    from act_in_the_world import score
    assert score.score_case.__module__ == "act_in_the_world.score"
