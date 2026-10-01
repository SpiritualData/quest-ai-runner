"""The planner must treat local files/links as hand-off work, never as something it cannot access."""
from quest_ai_runner.core.orchestrator import render_planner_prompt


def test_planner_never_tells_user_to_paste_what_deep_can_reach():
    prompt = render_planner_prompt(user_message="x")
    assert "NOT LIMITED TO WHAT YOUR READS CAN SEE" in prompt
    assert "NEVER tell the user you cannot" in prompt
    assert "paste, upload, or copy" in prompt
