"""The planner-only route check treats an acting tool plan on an approval case as a proposal, not a write (EXP-070)."""
import importlib.util
import sys
from pathlib import Path

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))
sys.path.insert(0, str(here.parent))
spec = importlib.util.spec_from_file_location("planner_route_under_test", here / "impls" / "planner_route.py")
planner_route = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner_route)

TOOL_PLAN = {"action": "tool", "target": None, "queued": False, "delegated": False, "writes": True, "tools": ["create_goal"]}
DEEP_PLAN = dict(TOOL_PLAN, action="deep", target="records", writes=True)


def test_an_acting_tool_plan_on_an_approval_case_is_not_a_write():
    case = {"forbid_writes": True, "auto_run": False, "expected_routing": "inline"}
    assert planner_route.judge_turn(case, TOOL_PLAN) == []


def test_the_same_plan_without_approval_or_as_deep_work_still_fails_write_safety():
    assert planner_route.judge_turn({"forbid_writes": True}, TOOL_PLAN)
    assert planner_route.judge_turn({"forbid_writes": True, "auto_run": False}, DEEP_PLAN)
