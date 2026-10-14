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
    for comp in ("classification", "quest_ops"):
        for p in sorted((Path(__file__).parent / comp / "impls").glob("[a-z]*.py")):
            assert hasattr(importlib.import_module(f"{comp}.impls.{p.stem}"), "build")
