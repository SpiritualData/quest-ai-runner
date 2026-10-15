"""The planner prompt caps its volatile sections: newest kept, explicit marker, latest message intact."""
from quest_ai_runner.core.planner_context import cap_section


def test_within_limit_is_unchanged():
    assert cap_section("abc", 10) == "abc"
    assert cap_section("abc" * 100, 0) == "abc" * 100


def test_tail_keeps_newest_and_marks():
    out = cap_section("old " * 100 + "NEWEST", 50, keep="tail")
    assert out.startswith("[truncated ") and out.endswith("NEWEST")


def test_head_keeps_start_and_marks():
    out = cap_section("FIRST " + "x" * 500, 50, keep="head")
    assert out.startswith("FIRST") and out.endswith("chars]") and "[truncated 4" in out
