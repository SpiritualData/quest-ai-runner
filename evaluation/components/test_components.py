"""Offline tests: no model call is made. Run: .venv/bin/python3 -m pytest evaluation/components/test_components.py"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def test_meter_counts_and_aborts():
    m = common.CallMeter(max_calls=2)
    m.record(); m.record()
    with pytest.raises(common.BudgetExceeded):
        m.record()


def test_wrap_keeps_signature_and_counts():
    import inspect

    class P:
        def plan(self, prompt, *, model, tool_schema, step=None): return {"ok": 1}
        def answer(self, messages, *, model, system=None): return "x"
    m = common.CallMeter()
    p = m.wrap(P())
    assert "step" in inspect.signature(p.plan).parameters
    p.plan("a", model="m", tool_schema={}); p.answer([], model="m")
    assert m.calls == 2


def test_orchestrator_has_no_deep_runner_and_no_reachable_quest():
    orch = common.build_offline_orchestrator(common.CallMeter())
    assert not getattr(orch, "deep_runner", None)
    assert common.OFFLINE_QUEST_URL.endswith(".invalid")


def test_classification_scorer_rules():
    from classification.score import score_case
    case = {"message": "m", "expect": ["tool"], "tools": ["create_quest_goal"]}
    assert score_case(lambda m, c: {"route": "tool", "tool": "create_quest_goal"}, case)["ok"]
    assert not score_case(lambda m, c: {"route": "tool", "tool": "create_quest_habit"}, case)["ok"]
    assert not score_case(lambda m, c: {"route": "deep", "tool": None}, case)["ok"]
    assert not score_case(lambda m, c: {"route": "nonsense"}, case)["ok"]


def test_quest_ops_scorer_catches_wrong_and_extra_writes():
    from quest_ops.fake_quest import WORLD
    from quest_ops.score import score_case
    from quest_ai_runner.core.tools import ToolContext

    def do(*calls):
        def fn(msg, world):
            for name, args in calls:
                assert world.registry.invoke(name, args, ToolContext(quest_id="q")).ok
            return {"reply": ""}
        return fn
    goal = {"id": "t", "message": "m", "expect": [{"tool": "create_quest_goal", "args_contain": {"title": "5K"}}]}
    assert score_case(do(("create_quest_goal", {"title": "Run 5K", "period": "2026_W42"})), goal)["ok"]
    assert not score_case(do(), goal)["ok"]
    r = score_case(do(("create_quest_goal", {"title": "Run 5K", "period": "2026_W42"}),
                      ("update_quest_current_state", {"value": "x"})), goal)
    assert not r["ok"] and "unrequested" in r["note"]
    assert not score_case(do(("create_quest_habit", {"name": "a"})),
                          {"id": "n", "message": "m", "expect": [{"no_writes": True}]})["ok"]
    WORLD.reset()
    assert WORLD.state["goals"] == []


def test_every_impl_builds_a_callable():
    import importlib
    for comp in ("classification", "quest_ops", "context_selection", "recipe_fast_path", "staleness", "prompt_size"):
        for p in sorted((Path(__file__).parent / comp / "impls").glob("[a-z]*.py")):
            assert hasattr(importlib.import_module(f"{comp}.impls.{p.stem}"), "build")


def _toy_corpus(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "poller.py").write_text('"""Discovers due tasks and claims them with bounded concurrency."""\n\ndef poll(): pass\n')
    (tmp_path / "pkg" / "mailer.py").write_text('"""Sends outbound email through the relay."""\n\ndef send(): pass\n')
    (tmp_path / "pkg" / "clock.py").write_text('"""Reads the wall clock."""\n\ndef now(): pass\n')
    from context_selection.corpus import build_corpus
    return build_corpus(tmp_path)


def test_context_selection_scorer_top1_top3_and_budget(tmp_path, monkeypatch):
    from context_selection.score import aggregate, score_case
    monkeypatch.setenv("QAR_EVAL_CORPUS_ROOT", str(tmp_path))
    _toy_corpus(tmp_path)
    case = {"task": "t", "target": "pkg/b.py"}
    first = score_case(lambda t, c: ["pkg/b.py", "pkg/a.py"], case)
    assert first["ok"] and first["top1"] and first["top3"]
    third = score_case(lambda t, c: ["x", "y", "pkg/b.py"], case)
    assert third["ok"] and not third["top1"] and third["top3"]
    fourth = lambda t, c: ["x", "y", "z", "pkg/b.py"]  # noqa: E731
    assert not score_case(fourth, case)["ok"]
    fourth.budget = 6                                  # a union solution declares a wider budget
    wide = score_case(fourth, case)
    assert wide["ok"] and not wide["top3"]
    assert not score_case(lambda t, c: [], case)["ok"]
    assert "top-1 1/2" in aggregate([first, score_case(lambda t, c: [], case)])


def test_context_selection_offline_impls_rank_a_toy_corpus(tmp_path):
    from context_selection.impls import blind_grep, idf_docstrings
    corpus = _toy_corpus(tmp_path)
    task = "which module discovers due tasks and claims them"
    assert blind_grep.build(common.CallMeter())(task, corpus)[0] == "pkg/poller.py"
    assert idf_docstrings.build(common.CallMeter())(task, corpus)[0] == "pkg/poller.py"


def test_context_selection_cases_point_at_real_files():
    import json
    cases = json.loads((Path(__file__).parent / "context_selection" / "cases.json").read_text())
    assert len(cases) == 15 and len({c["id"] for c in cases}) == 15
    assert all((common.REPO / c["target"]).is_file() for c in cases)


def test_staleness_scorer_separates_sha256_from_mtime_size():
    import json
    from staleness.impls import mtime_size, store_sha256
    from staleness.score import score_case
    cases = {c["id"]: c for c in json.loads((Path(__file__).parent / "staleness" / "cases.json").read_text())}
    sha, stat = store_sha256.build(common.CallMeter()), mtime_size.build(common.CallMeter())
    for cid in ("STL-001", "STL-002", "STL-003", "STL-004"):
        assert score_case(sha, cases[cid])["ok"], cid
    assert not score_case(stat, cases["STL-004"])["ok"]   # a touched file is not stale
    assert not score_case(stat, cases["STL-005"])["ok"]   # a same-size edit with the mtime restored is
    assert score_case(stat, cases["STL-006"])["ok"]


def test_recipe_fast_path_scorer_teaches_once_and_checks_the_world():
    from recipe_fast_path import score
    from recipe_fast_path.fake_world import TEACH_TOOLS
    from quest_ai_runner.core.tools import ToolContext
    sent = []

    def good(msg, world):
        sent.append(msg)
        if "yoga" in msg or msg == TEACH_TOOLS["complete_habit"]:
            habit = "yoga" if "yoga" in msg else "meditation"
            assert world.registry.invoke("complete_habit", {"habit": habit}, ToolContext(quest_id="q")).ok
        return {"reply": "", "exit_reason": "recipe"}
    ask = {"id": "r", "group": "repeat", "tool": "complete_habit", "ask": "mark my yoga habit as done",
           "expect_tool": "complete_habit", "expect_args": {"habit": "yoga"}}
    row = score.score_case(good, ask)
    assert row["ok"] and row["recipe"]
    score.score_case(good, dict(ask, id="r2"))
    assert sent.count(TEACH_TOOLS["complete_habit"]) == 1   # taught once per solution instance
    near = {"id": "n", "group": "near_miss", "tool": "complete_habit", "ask": "show my yoga habit",
            "expect_tool": "habit_streak", "expect_args": {"habit": "yoga"}, "forbid_tool": "complete_habit"}
    row = score.score_case(good, near)
    assert not row["ok"] and row["wrong_op"]                # the lookalike mutating tool ran
    assert score.verify({"expect_tool": None, "forbid_tool": "any"}, [])[0]


def test_recipe_fast_path_cases_cover_all_groups():
    import json
    cases = json.loads((Path(__file__).parent / "recipe_fast_path" / "cases.json").read_text())
    assert len(cases) == 30 and {c["group"] for c in cases} == {"repeat", "near_miss", "unrelated"}


def test_leaderboard_only_compares_rows_scored_on_the_same_cases():
    import leaderboard
    mk = lambda impl, passed, sha: dict(impl=impl, cases="10", passed=str(passed), cost_usd="0", cases_sha=sha)
    rows = {"cur": mk("cur", 5, "aaaa"), "other": mk("other", 9, "bbbb"), "same": mk("same", 8, "aaaa")}
    assert [r["impl"] for r in leaderboard.challengers(rows, "cur")] == ["same"]
    blank = {"cur": mk("cur", 5, ""), "old": mk("old", 9, "")}  # legacy rows compare as one group
    assert [r["impl"] for r in leaderboard.challengers(blank, "cur")] == ["old"]
