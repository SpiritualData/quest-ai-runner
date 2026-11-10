"""The terminal speaks the hub's session contract: helpers under their parent, the solution a run serves, what was blocked and why, and what each runner can take
while it works. Scripted events, no model."""
from __future__ import annotations

from hubofhubs.api import ProgressEvent
from quest_ai_runner.the_consciousness.terminal.session_contract import RunSupport, TurnSession, support_of, unsupported_line

from .test_terminal_calm_output import RecordingLog, run_app


def session(**data) -> ProgressEvent:
    return ProgressEvent(type="session", data=data)


SCRIPT = [
    session(type="run-started", goal="Ship it", harness="loop", solution="release-notes", milestone="notes are right"),
    session(type="agent-started", agent_id="helper-1", parent_id="main", task="write the tests", solution=""),
    session(type="agent-started", agent_id="helper-2", parent_id="helper-1", task="build fixtures", solution="fixtures"),
    session(type="agent-ended", agent_id="helper-2", state="met", calls=1),
    session(type="step", step_id="s1", kind="tool", label="deploy prod", state="blocked", detail="you refused it"),
    session(type="agent-ended", agent_id="helper-1", state="unmet", calls=2),
]


def test_the_events_of_a_run_become_lines_a_person_can_follow():
    turn = TurnSession()
    lines = [line for event in SCRIPT for line in turn.take(event)]
    assert lines == ["Serves release-notes: notes are right", "↳ helper: write the tests", "  ↳ helper: build fixtures (fixtures)", "  ✓ helper met: build fixtures",
                     "⊘ blocked: deploy prod (you refused it)", "~ helper unmet: write the tests"]


def test_the_end_of_the_turn_draws_the_helper_tree_and_what_was_blocked():
    turn = TurnSession()
    for event in SCRIPT:
        turn.take(event)
    closing = turn.closing_lines()
    assert closing[0] == "Helpers:" and any("write the tests" in line for line in closing) and any("build fixtures" in line for line in closing)
    assert closing[-2:] == ["Blocked:", "  deploy prod: you refused it"]
    assert TurnSession().closing_lines() == []


def test_a_run_with_no_session_events_adds_nothing():
    turn = TurnSession()
    assert turn.take(ProgressEvent(type="exec", text="Read: a.txt", data={})) == [] and turn.closing_lines() == []


class StreamLike:
    def run_goal(self, *, goal, brief, control=None): ...


class OneShot:
    def run_goal(self, *, goal, brief, cancel_check=None): ...


class HubLike:
    emits_session = True

    def steer(self, message, *, run_id=None): ...

    def run_goal(self, *, goal, brief="", control=None, **ignored): ...


def test_support_is_read_from_how_the_runner_is_built_and_a_missing_control_gets_one_plain_line():
    assert support_of(StreamLike()) == RunSupport(stop=True, steer=True, pause=True, helpers=False)
    assert support_of(HubLike()) == RunSupport(stop=True, steer=True, pause=True, helpers=True)
    one_shot = support_of(OneShot())
    assert one_shot == RunSupport(stop=True, steer=False, pause=False, helpers=False)
    assert "when its current step ends" in unsupported_line("steer", one_shot) and "Esc stops" in unsupported_line("pause", one_shot)
    assert unsupported_line("steer", support_of(HubLike())) is None


def test_the_real_hub_loop_binding_reports_the_contract_and_takes_a_control():
    from quest_ai_runner.the_world.tools.delegated_agent_sessions.hub_loop_runner import HubLoopRunner
    assert support_of(HubLoopRunner(root=".", complete=lambda s, p: "ok")) == RunSupport(stop=True, steer=True, pause=True, helpers=True)


def test_the_app_writes_the_session_lines_as_they_arrive_and_the_tree_at_the_end():
    async def build(app, pilot):
        rec = RecordingLog()
        app._tlog = rec
        for event in SCRIPT:
            app._handle_event(event)
        await pilot.pause()
        return rec.lines, app._turn_session.closing_lines()

    lines, closing = run_app(build)
    shown = [line.strip() for line in lines]
    assert "Serves release-notes: notes are right" in shown and "↳ helper: write the tests" in shown and "⊘ blocked: deploy prod (you refused it)" in shown
    assert closing[0] == "Helpers:"
