"""The consumer's side of the hub's native tool loop (``native_loop``) when a lane runs it for a long task.

Pinned here, offline (fakes for the registry, the runner and the model; no process, no network):

* the lane's ``[deep_runner_options]`` reach the runner's own constructor (ceilings, permission, sandbox), generically and by the runner's own parameter names, and a
  name the runner does not take is refused instead of dropped;
* the frame the binding adds tells the session exactly how to add a note to a goal and raise a decision from Bash, and the ``QAR-ESCALATED:`` last-line rule;
* a run a spend or time ceiling shaped (wrapped up at the soft limit, stopped at the hard limit) is never reported as done and never as a person's cancel: it ends
  incomplete, with what was done and the session to continue, and the goal loop neither verifies nor retries it.
"""
from __future__ import annotations

import pytest

from hubofhubs.api import DeepResult
from quest_ai_runner.the_body.configuration import config_resolvers
from quest_ai_runner.the_body.configuration.config import RunnerConfig, resolve_deep_runner
from quest_ai_runner.the_consciousness.request_intake.executor import TaskExecutor
from quest_ai_runner.the_intelligence.mind.orchestrator import OrchestratorResult, PlanDecision
from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_text import mark_ceiling_stop
from quest_ai_runner.the_world.tools.delegated_agent_sessions import native_loop_runner
from tests.conftest import StubProvider, StubRetrieval
from tests.test_fast_edit_ladder import RecordingRunner, _orch


@pytest.fixture
def programs(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("QAR_CLAUDE_PATH", "claude")
    for name in config_resolvers.SELECT_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("QAR_SKILLS_DIR", str(tmp_path / "none"))


def lane(**kwargs):
    return RunnerConfig(retrieval=StubRetrieval({"README.md": "hi"}), model_provider=StubProvider([]), corpus_root="/tmp", **kwargs)


# --- the lane's runner options -----------------------------------------------------------------------------------------------

def test_a_lanes_ceiling_permission_and_sandbox_settings_reach_the_native_runner(programs):
    options = {"ceiling_usd": 5, "ceiling_seconds": 7200, "soft_fraction": 0.9, "ceiling_turns": 300, "permission": "acceptEdits", "sandbox": True, "sandbox_network": False}
    runner = resolve_deep_runner(lane(selections={"delegated-agent-sessions": "native_loop"}, deep_runner_options=options))
    assert type(runner) is native_loop_runner.NativeLoopRunner
    assert (runner.ceiling_usd, runner.ceiling_seconds, runner.soft_fraction, runner.ceiling_turns) == (5.0, 7200.0, 0.9, 300)
    assert (runner.permission, runner.sandbox, runner.sandbox_network) == ("acceptEdits", True, False)


def test_no_options_keep_the_runners_own_defaults_and_unlimited_stays_explicit(programs):
    plain = resolve_deep_runner(lane(selections={"delegated-agent-sessions": "native_loop"}))
    assert plain.ceiling_usd and plain.ceiling_seconds and plain.permission == "skip" and plain.sandbox is False     # the hub's defaults are on, never unbounded
    free = resolve_deep_runner(lane(selections={"delegated-agent-sessions": "native_loop"}, deep_runner_options={"ceiling_usd": "unlimited"}))
    assert free.ceiling_usd is None


def test_the_options_work_for_any_runner_the_lane_builds_and_a_name_it_does_not_take_is_refused(programs):
    stream = resolve_deep_runner(lane(selections={"delegated-agent-sessions": "stream_json"}, deep_runner_options={"ceiling_usd": 2}))
    assert stream.ceiling_usd == 2.0
    typo = lane(selections={"delegated-agent-sessions": "native_loop"}, deep_runner_options={"ceilng_usd": 2})
    with pytest.raises(ValueError, match="ceilng_usd"):
        config_resolvers.deep_runner_options(typo, native_loop_runner.NativeLoopRunner)
    assert resolve_deep_runner(typo, warn=False) is None            # the lane does not run with the ceiling silently off
    with pytest.raises(ValueError, match="sets for"):
        config_resolvers.deep_runner_options(lane(deep_runner_options={"notes": "x"}), native_loop_runner.NativeLoopRunner, taken=("notes",))


def test_the_options_are_a_config_file_table(tmp_path):
    path = tmp_path / "qar.toml"
    path.write_text('corpus_root = "/tmp"\n[deep_runner_options]\nceiling_usd = 5\npermission = "acceptEdits"\n')
    assert RunnerConfig.from_file(path).deep_runner_options == {"ceiling_usd": 5, "permission": "acceptEdits"}


# --- the frame's account of the lane's operations ----------------------------------------------------------------------------

class Spec:
    def __init__(self, parameters):
        self.parameters = parameters


class Registry:
    def __init__(self, **specs):
        self.specs = specs

    def get(self, name):
        return self.specs.get(name)


FAKE = Registry(add_quest_note=Spec({"properties": {"quest_id": {"type": "string"}, "text": {"type": "string"}}, "required": ["text"]}),
                create_decision=Spec({"properties": {"summary": {"type": "string"}, "assignee": {"type": "string"}}, "required": ["summary"]}))


def test_the_frame_says_exactly_how_to_add_a_note_and_raise_a_decision_from_bash():
    note = native_loop_runner.lane_note({}, registry=FAKE, python="/venv/bin/python")
    assert "/venv/bin/python -m quest_ai_runner tools call add_quest_note" in note and '"text": string (required)' in note
    assert "/venv/bin/python -m quest_ai_runner tools call create_decision" in note and '"summary": string (required)' in note
    assert "`--quest <id>`" in note and "`--task <id>`" in note
    assert "LAST line of your final answer must be exactly `QAR-ESCALATED: <decision id>`" in note
    assert "—" not in note


def test_a_lane_without_those_operations_is_told_nothing_about_them():
    assert native_loop_runner.lane_note({}, registry=Registry()) == ""
    only_note = native_loop_runner.lane_note({}, registry=Registry(add_quest_note=FAKE.specs["add_quest_note"]))
    assert "add_quest_note" in only_note and "create_decision" not in only_note and "QAR-ESCALATED" not in only_note


def test_the_runner_hands_the_frame_both_the_skills_and_the_lane_operations(programs, monkeypatch, tmp_path):
    skills = tmp_path / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("description: a demo\n")
    monkeypatch.setenv("QAR_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(native_loop_runner, "lane_note", lambda env, registry=None, python=None: "LANE OPERATIONS TEXT")
    runner = resolve_deep_runner(lane(selections={"delegated-agent-sessions": "native_loop"}))
    assert str(tmp_path / "skills") in runner.notes and runner.notes.endswith("LANE OPERATIONS TEXT")


# --- what a ceiling-shaped run is reported as ----------------------------------------------------------------------------------

WRAPPED = DeepResult(met=True, output="Done: steps 1 to 9. Left: 10 to 20. To resume: continue.", session_id="s-1", end_reason="wrapped_up_at_soft_limit")
HARD = DeepResult(met=False, cancelled=True, output="partial work", session_id="s-2", end_reason="stopped_at_hard_limit:time",
                  error="The run was stopped at its hard limit (time). Work already finished is kept; resume session s-2 to continue.")


def run_loop(result, verdicts=()):
    runner = RecordingRunner(result)
    verified = []
    orch = _orch(StubProvider([]), [runner], [])
    orch._verify_goal = lambda *a, **k: (verified.append(1) or {"met": True}, None)
    res = orch._run_deep(PlanDecision(action="deep", goal="g", deep_brief="b"), "g", "sonnet", cancel_check=None)
    return runner, verified, res


def reported(res):
    class Client:
        def report_incomplete(self, task_id, result, session_id=None):
            self.session = session_id
    executor = TaskExecutor(Client(), None)
    executor._post_conv = executor._report_progress = lambda *a, **k: None
    executor._with_context_receipt = lambda text, *a, **k: text
    return executor._report("t1", OrchestratorResult(kind="deep", deep_results=res.deep_results, goals=["g"]))


def test_a_run_wrapped_up_at_the_soft_limit_is_not_done_and_is_not_verified_or_retried():
    runner, verified, res = run_loop(WRAPPED)
    done = res.deep_results[0]
    assert len(runner.calls) == 1 and not verified                    # not verified as done, not run again past its ceiling
    assert done.met is False and done.ceiling_stop == "wrapped_up_at_soft_limit" and "wrapped up" in done.error
    outcome = reported(res)
    assert outcome.status == "incomplete" and "Done: steps 1 to 9" in outcome.result and "a reply continues the same session" in outcome.result


def test_a_run_stopped_at_the_hard_limit_is_incomplete_with_its_resume_handle_not_a_persons_cancel():
    runner, verified, res = run_loop(HARD)
    assert res.kind == "deep" and len(runner.calls) == 1 and not verified          # "cancelled" would be read as the person's stop and end silently
    done = res.deep_results[0]
    assert done.cancelled is False and done.met is False and done.session_id == "s-2"
    outcome = reported(res)
    assert outcome.status == "incomplete" and "partial work" in outcome.result and "resume session s-2" in outcome.result


def test_the_hard_stop_with_no_output_is_still_incomplete_not_failed():
    bare = DeepResult(met=False, cancelled=True, session_id="s-3", end_reason="stopped_at_hard_limit:spend", error="The run was stopped at its hard limit (spend).")
    _, _, res = run_loop(bare)
    assert reported(res).status == "incomplete"


def test_a_raised_decision_and_a_persons_pause_are_left_alone_and_other_runs_are_untouched():
    asked = DeepResult(met=False, decision_id="dec_1", end_reason="wrapped_up_at_soft_limit")
    assert mark_ceiling_stop(asked).decision_id == "dec_1" and not getattr(asked, "ceiling_stop", "")
    paused = DeepResult(met=False, cancelled=True, paused=True, end_reason="stopped_at_hard_limit:time")
    assert mark_ceiling_stop(paused).cancelled is True and not getattr(paused, "ceiling_stop", "")
    plain = DeepResult(met=True, output="ok", end_reason="resume_stopped:plateau")
    assert mark_ceiling_stop(plain).met is True and not getattr(plain, "ceiling_stop", "")
