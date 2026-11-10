"""The hub loop binding hands the loop the lane's own operations as tools and gives its commands the lane's environment. Scripted completion, a fake registry."""
from __future__ import annotations

from quest_ai_runner.the_world.tools.delegated_agent_sessions.hub_loop_runner import HubLoopRunner, argument_shape, lane_tools
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec


class Script:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, system, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else "BLOCKED: script ended"


def registry(seen, fail=False):
    def decision(args, ctx: ToolContext):
        seen.append((dict(args), ctx.user_id, ctx.task_id))
        if fail:
            return ToolResult(ok=False, text="Quest refused the ask (422): Assigned to user id is required")
        return ToolResult(ok=True, data={"decision_id": "dec_1"}, text="Raised ask dec_1 for the person.")

    def note(args, ctx):
        return ToolResult(ok=True, text="Note added.")
    parameters = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}, "kind": {"type": "string"}}}
    return ToolRegistry([ToolSpec(name="create_decision", handler=decision, description="Raise something you need from a person.", parameters=parameters),
                         ToolSpec(name="add_quest_note", handler=note, description="Add a note to a goal.", parameters={"type": "object", "properties": {"text": {"type": "string"}}})])


def runner(tmp_path, script, seen, fail=False):
    return HubLoopRunner(root=str(tmp_path), complete=script, sandbox=False, review=False, session_dir=str(tmp_path / "s"), model_for=lambda p, t: "m", registry=registry(seen, fail))


def test_the_lane_operations_are_tools_with_their_description_and_one_line_argument_shape(tmp_path):
    tools = lane_tools(object(), {}, registry([]))
    assert set(tools) == {"create_decision", "add_quest_note"}
    assert tools["create_decision"].description == 'Raise something you need from a person. Input: one JSON object {"summary": string (required), "kind": string}'
    assert argument_shape({"properties": {}}) == "{}"


def test_a_decision_raised_through_the_tool_runs_with_the_requester_and_is_the_runs_decision_id(tmp_path):
    seen = []
    script = Script('USE: create_decision {"summary": "Need the sheet id"}\n', "REPORT: asked\n")
    deep = runner(tmp_path, script, seen).run_goal(goal="Do it", requester_user_id="user_7", run_id="run_1")
    assert seen == [({"summary": "Need the sheet id"}, "user_7", "run_1")]
    assert deep.decision_id == "dec_1" and not deep.met and "Raised ask dec_1" in script.prompts[1]


def test_a_tool_that_the_lane_refuses_fails_the_action_and_is_not_repeated_the_third_time(tmp_path):
    seen = []
    script = Script(*['USE: create_decision {"summary": "x"}\n'] * 4, "REPORT: could not ask\n")
    deep = runner(tmp_path, script, seen, fail=True).run_goal(goal="Do it", requester_user_id="user_7")
    assert len(seen) == 2 and not deep.decision_id and "Assigned to user id is required" in script.prompts[1]
    assert "tool call" in script.prompts[3]


def test_a_bad_input_says_what_shape_is_wanted(tmp_path):
    seen = []
    script = Script("USE: create_decision not json\n", "REPORT: ok\n")
    runner(tmp_path, script, seen).run_goal(goal="Do it")
    assert 'one JSON object like {"summary": string (required), "kind": string}' in script.prompts[1] and not seen


def test_commands_get_the_lane_environment_the_requester_and_the_run_and_no_nested_billing(tmp_path, monkeypatch):
    monkeypatch.setenv("QUEST_API_KEY", "key-from-the-lane")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    loop = runner(tmp_path, Script("ok"), [])
    env = loop.child_environment(loop.goal_settings(), "user_7", "run_9")
    assert env["QUEST_API_KEY"] == "key-from-the-lane" and env["QAR_REQUESTER_USER_ID"] == "user_7" and env["PYTHONUNBUFFERED"] == "1"
    assert "ANTHROPIC_API_KEY" not in env and "CLAUDECODE" not in env and "run_9" in env.values()


def test_a_follow_up_with_the_same_run_id_or_an_explicit_session_resumes_through_the_binding(tmp_path):
    first = Script("RUN: echo one\nSTATE: learned: the code is 4417\n", "RUN: echo two\n")
    runner(tmp_path, first, []).run_goal(goal="Work", run_id="task-1", max_turns=2)
    second = Script("REPORT: ok\n")
    deep = runner(tmp_path, second, []).run_goal(goal="Work, with what was missing", run_id="task-1", max_turns=2)
    assert deep.met and "the code is 4417" in second.prompts[0] and deep.end_reason == "met"
    third = Script("REPORT: ok\n")
    runner(tmp_path, third, []).run_goal(goal="Work", run_id="task-2", resume_session_id="task-1")
    assert "the code is 4417" in third.prompts[0]
