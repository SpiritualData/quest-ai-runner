"""A deep run's live status lines must say what really happened.

Reported 2026-10-05 from a chat: within a few seconds the status said a deep run started on sonnet,
then "Goal not met", then that it was retrying with opus. Nothing in that sequence was a deep run.
Two separate causes, both pinned here:

* **A runner that does not run the ladder's model** (a consumer's in-process answerer or data-op
  runner, or a queue hand-off whose lane picks its own model) was announced and escalated exactly
  like a Claude Code worker: "Starting on sonnet", "Deep run used sonnet", "retrying with opus",
  while every attempt was the same in-process call. Such a runner declares
  ``uses_deep_model = False`` and the goal loop then makes no model claim for it.
* **A worker that never started** (Claude Code refusing an unrecognized ``--model``, bad
  credentials, a missing binary) returned the CLI's error text as output. The goal loop verified
  that text, reported "Goal not met" and escalated the model. It is now ``launch_failed``: the
  loop reports it as the error it is, never verifies it and never escalates on it.

Offline: no real ``claude`` binary, no network.
"""
from __future__ import annotations

import json
import stat
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.core import usage_limit
from quest_ai_runner.core.adapters import DeepResult, runner_uses_deep_model
from quest_ai_runner.core.goal_runner import (
    SubprocessConfig,
    SubprocessGoalRunner,
    envelope_reports_no_work,
)
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, OrchestratorConfig

from .conftest import StubRetrieval
from .test_per_goal_context_iteration import ScriptedProvider

NOT_MET = {"met": False, "reason": "it is incomplete", "what_fell_short": "incomplete",
           "next_steps": "finish it"}

# The envelope Claude Code really prints for ``claude -p --model claude-bogus-9`` (exit 1),
# captured 2026-10-05 and trimmed to the fields that matter.
UNRECOGNIZED_MODEL_ENVELOPE = {
    "type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
    "api_error_status": 404, "terminal_reason": "api_error", "total_cost_usd": 0,
    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
              "cache_read_input_tokens": 0},
    "result": ("There's an issue with the selected model (claude-bogus-9). It may not exist or "
               "you may not have access to it. Run --model to pick a different model."),
}


@pytest.fixture(autouse=True)
def fresh_usage_note():
    usage_limit.reset_for_tests()
    yield
    usage_limit.reset_for_tests()


def deep_plan(difficulty: Optional[str] = "normal") -> Dict[str, Any]:
    plan = {"action": "deep", "goal": "Do the work",
            "deep_subtasks": [{"goal": "Do the work", "brief": "do it"}], "rationale": "deep"}
    if difficulty:
        plan["deep_difficulty"] = difficulty
        plan["deep_difficulty_reason"] = "because"
    return plan


class ScriptedRunner:
    """A deep runner returning scripted results and recording the model each attempt was given."""

    def __init__(self, results: List[DeepResult], *, uses_model: bool = True):
        self.results = list(results)
        self.models: List[Optional[str]] = []
        self.uses_deep_model = uses_model

    def run_goal(self, *, goal: str, brief: str, model: Optional[str] = None,
                 max_turns: Optional[int] = None) -> DeepResult:
        self.models.append(model)
        if self.results:
            return self.results.pop(0)
        return DeepResult(met=True, output="done")


class RecordingSink:
    def __init__(self):
        self.events = []

    def update(self, event, mode) -> None:
        self.events.append(event)

    def texts(self) -> List[str]:
        return [e.text or "" for e in self.events]


def run_turn(runner, verdicts, **cfg):
    provider = ScriptedProvider(plans=[deep_plan()], verdicts=verdicts)
    sink = RecordingSink()
    orch = Orchestrator(retrieval=StubRetrieval({}), provider=provider,
                        registry=ModelRegistry(provider), deep_runner=runner,
                        config=OrchestratorConfig(deep_model_ladder=["haiku", "sonnet", "opus"],
                                                  **cfg))
    result = orch.run("please do the thing", sink=sink)
    return result, sink, provider


# --- a runner that does not run the ladder's model ---------------------------------------------

def test_runner_uses_deep_model_defaults_true_and_reads_the_declaration():
    class Plain:
        def run_goal(self, **kw):
            return DeepResult(met=True)

    assert runner_uses_deep_model(Plain()) is True
    assert runner_uses_deep_model(ScriptedRunner([], uses_model=False)) is False
    assert runner_uses_deep_model(None) is False


def test_an_in_process_runner_is_never_described_as_running_a_model():
    runner = ScriptedRunner([DeepResult(met=True, output="first try"),
                             DeepResult(met=True, output="second try")], uses_model=False)
    _, sink, _ = run_turn(runner, [NOT_MET, {"met": True}])
    texts = sink.texts()
    assert not [t for t in texts if t.startswith("Starting on")]
    assert not [e for e in sink.events if "deep_model_selection" in (e.data or {})]
    assert not [t for t in texts if t.startswith("Deep run used")]
    assert not [e for e in sink.events if "deep_run_model" in (e.data or {})]
    retries = [t for t in texts if t.startswith("Goal not met yet, retrying")]
    assert retries and all(" with " not in t for t in retries)
    # It ran twice (a genuine verified retry with feedback), but no tier was climbed for it.
    assert len(runner.models) == 2 and runner.models[0] == runner.models[1]


def test_a_model_running_worker_still_announces_and_escalates():
    runner = ScriptedRunner([DeepResult(met=True, output="first"),
                             DeepResult(met=True, output="second")])
    _, sink, _ = run_turn(runner, [NOT_MET, {"met": True}])
    texts = sink.texts()
    assert [t for t in texts if t.startswith("Starting on sonnet")]
    assert runner.models == ["sonnet", "opus"]
    assert "Goal not met yet, retrying with opus…" in texts


# --- a worker that never started ----------------------------------------------------------------

def test_a_launch_failure_is_reported_as_an_error_not_verified_and_not_escalated():
    failed = DeepResult(met=False, launch_failed=True,
                        error="The deep worker could not start: There's an issue with the "
                              "selected model (claude-bogus-9).")
    runner = ScriptedRunner([failed, DeepResult(met=True, output="should never run")])
    result, sink, provider = run_turn(runner, [NOT_MET, {"met": True}])
    texts = sink.texts()
    assert runner.models == ["sonnet"], "a launch failure must not be retried on a stronger model"
    assert provider.verify_calls == 0, "there is no work product to verify"
    assert not [t for t in texts if t.startswith("Goal not met")]
    assert not [t for t in texts if t.startswith("Deep run used")]
    assert [t for t in texts if "could not start" in t]
    assert result.deep_results and result.deep_results[0].launch_failed
    assert not result.deep_results[0].met


def fake_claude(tmp_path, envelope: dict, code: int) -> str:
    out = tmp_path / "envelope.json"
    out.write_text(json.dumps(envelope))
    script = tmp_path / "claude"
    script.write_text(f"#!/bin/sh\ncat > /dev/null\ncat '{out}'\nexit {code}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_an_unrecognized_model_is_a_launch_failure_with_the_cli_reason(tmp_path):
    binary = fake_claude(tmp_path, UNRECOGNIZED_MODEL_ENVELOPE, 1)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=binary))
    res = runner.run_goal(goal="do the thing", brief="do the thing", model="claude-bogus-9")
    assert res.launch_failed and not res.met
    assert res.output == "", "the CLI's error text is not work product"
    assert res.error.startswith("The deep worker could not start:")
    assert "issue with the selected model" in res.error


def test_a_missing_binary_is_a_launch_failure(tmp_path):
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path),
                                                   claude_path=str(tmp_path / "no-such-claude")))
    res = runner.run_goal(goal="do the thing", brief="do the thing")
    assert res.launch_failed and not res.met and "could not start" in res.error


def test_a_failure_after_real_work_is_not_a_launch_failure(tmp_path):
    envelope = dict(UNRECOGNIZED_MODEL_ENVELOPE,
                    usage={"input_tokens": 1200, "output_tokens": 340},
                    result="Edited two files, then the API errored.")
    binary = fake_claude(tmp_path, envelope, 1)
    runner = SubprocessGoalRunner(SubprocessConfig(working_dir=str(tmp_path), claude_path=binary))
    res = runner.run_goal(goal="do the thing", brief="do the thing")
    assert not res.launch_failed and not res.met
    assert "Edited two files" in res.output


def test_envelope_reports_no_work_only_for_a_parsed_zero_usage_error():
    assert envelope_reports_no_work(json.dumps(UNRECOGNIZED_MODEL_ENVELOPE))
    # Cache traffic is work.
    cached = dict(UNRECOGNIZED_MODEL_ENVELOPE, usage={"input_tokens": 0, "output_tokens": 0,
                                                      "cache_read_input_tokens": 900})
    assert not envelope_reports_no_work(json.dumps(cached))
    # Not an error, no usage block, plain text, garbage: none of them say "nothing ran".
    assert not envelope_reports_no_work(json.dumps(dict(UNRECOGNIZED_MODEL_ENVELOPE,
                                                        is_error=False)))
    no_usage = {k: v for k, v in UNRECOGNIZED_MODEL_ENVELOPE.items() if k != "usage"}
    assert not envelope_reports_no_work(json.dumps(no_usage))
    assert not envelope_reports_no_work("plain text output")
    assert not envelope_reports_no_work("")
