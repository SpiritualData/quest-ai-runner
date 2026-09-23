"""The planner is told that reads only reach the listed sources, and to hand off the rest."""
from quest_ai_runner.core.orchestrator import render_planner_prompt


def test_planner_prompt_carries_reach_of_a_read():
    prompt = render_planner_prompt(user_message="check the job on the server")
    assert "REACH OF A READ" in prompt
    assert "deferred_deep" in prompt
    # Discovery and the sufficiency checklist are both scoped to what reads can reach.
    assert "Discovery maps ONLY the listed sources" in prompt
    assert "This checklist covers what your reads CAN reach" in prompt
