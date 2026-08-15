"""The deep-runner LADDER: opt-in wiring, and escalation through the loop that already existed.

Two claims are pinned here, and they are the two that matter for shipping this safely.

1. OPT-IN. A consumer that changed nothing sees a one-rung ladder holding exactly the runner it
   had before, no write capability anywhere, and byte-for-byte the behaviour of a single runner.
   Write access appears only when the consumer hands the library a ``FileWriter``.

2. NO NEW ESCALATION LOGIC. The research this was built from claims the orchestrator's goal loop
   ALREADY escalates: it runs an attempt, verifies it against the done-standard, and retries. So
   making the fast editor attempt 1 and the full deep runner attempt 2 should be a change to how
   the runner is RESOLVED (a list instead of one), not new control flow. These tests drive the
   real loop to prove that: a first rung that fails verification, and a first rung that produces
   nothing at all, both hand the work to the next rung.

Offline: no model, no subprocess, no ``claude`` binary is ever run.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import pytest

from quest_ai_runner.adapters.fast_edit_runner import FastEditRunner
from quest_ai_runner.adapters.files_writer import FilesWriter
from quest_ai_runner.config import (
    RunnerConfig,
    build_orchestrator,
    derive_capabilities,
    resolve_deep_runner_ladder,
)
from quest_ai_runner.core.adapters import DeepResult
from quest_ai_runner.core.goal_runner import SubprocessGoalRunner
from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator, PlanDecision

from .conftest import StubProvider, StubRetrieval


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """A file standing in for the ``claude`` binary on PATH. Never invoked: only found."""
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QAR_CLAUDE_PATH", "claude")
    return binary


class RecordingRunner:
    """A DeepRunner that records its calls and returns a scripted result."""

    def __init__(self, result: Optional[DeepResult] = None, name: str = "recording"):
        self.result = result or DeepResult(met=True, output="did the work")
        self.name = name
        self.calls: List[Dict[str, Any]] = []

    def run_goal(self, *, goal, brief, model=None, max_turns=None, **kwargs) -> DeepResult:
        self.calls.append({"goal": goal, "brief": brief, "model": model, **kwargs})
        return self.result


def _cfg(**kwargs) -> RunnerConfig:
    return RunnerConfig(retrieval=StubRetrieval({"README.md": "hi"}),
                        model_provider=StubProvider([]), **kwargs)


def _orch(provider, ladder, verdicts):
    """An orchestrator driving the REAL goal loop, with verification scripted.

    ``_verify_goal`` is the one thing stubbed. It is an LLM call, and what is under test here is
    which RUNNER each attempt uses given a verdict, not how the verdict is reached (that has its
    own tests in test_verify_tier.py).
    """
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider), deep_runner_ladder=ladder)
    scripted = list(verdicts)

    def fake_verify(goal, brief, output, **kwargs):
        return (scripted.pop(0) if scripted else {"met": True}), None

    orch._verify_goal = fake_verify
    return orch


# --- 1. opt-in: a consumer that changed nothing gets no write capability -------------------------

def test_default_consumer_ladder_is_just_the_deep_runner(fake_claude):
    ladder = resolve_deep_runner_ladder(_cfg())
    assert len(ladder) == 1
    assert isinstance(ladder[0], SubprocessGoalRunner)
    assert not any(isinstance(r, FastEditRunner) for r in ladder)


def test_default_consumer_orchestrator_has_a_one_rung_ladder(fake_claude):
    cfg = _cfg()
    orch = build_orchestrator(cfg)
    assert len(orch.deep_runner_ladder) == 1
    assert orch.deep_runner_ladder[0] is orch.deep_runner
    assert not any(isinstance(r, FastEditRunner) for r in orch.deep_runner_ladder)


def test_no_file_writer_means_no_write_capability_anywhere(fake_claude):
    """The library-level guarantee: with no writer wired there is no object in the wired brain
    that can modify a file."""
    orch = build_orchestrator(_cfg())
    for runner in orch.deep_runner_ladder:
        assert not hasattr(runner, "writer")


def test_wiring_a_file_writer_puts_the_fast_editor_in_front(fake_claude, tmp_path):
    writer = FilesWriter(str(tmp_path))
    ladder = resolve_deep_runner_ladder(_cfg(file_writer=writer))
    assert len(ladder) == 2
    assert isinstance(ladder[0], FastEditRunner)
    assert isinstance(ladder[1], SubprocessGoalRunner)
    assert ladder[0].writer is writer


def test_opting_in_does_not_change_what_cfg_deep_runner_means(fake_claude, tmp_path):
    """Consumers and both chat UIs read ``cfg.deep_runner`` to decide whether execution is
    available. The ladder is the orchestrator's business; that field must stay a single runner."""
    cfg = _cfg(file_writer=FilesWriter(str(tmp_path)))
    orch = build_orchestrator(cfg)
    assert isinstance(cfg.deep_runner, SubprocessGoalRunner)
    assert orch.deep_runner is cfg.deep_runner
    assert isinstance(orch.deep_runner_ladder[0], FastEditRunner)


def test_a_fast_editor_alone_counts_as_execution_capability(tmp_path, monkeypatch):
    """No Claude Code on the box, but a writer wired: the consumer really can execute (small
    edits), and both the capability probe and the brain's own gate must say so rather than
    reporting "nothing can run this"."""
    monkeypatch.setenv("QAR_CLAUDE_PATH", "qar-test-no-such-claude-binary")
    cfg = _cfg(file_writer=FilesWriter(str(tmp_path)))
    assert derive_capabilities(cfg)["code"] is True
    orch = build_orchestrator(cfg)
    assert len(orch.deep_runner_ladder) == 1
    assert isinstance(orch.deep_runner_ladder[0], FastEditRunner)
    assert orch._has_deep_execution_capability() is True


def test_nothing_wired_at_all_is_still_no_capability(monkeypatch):
    monkeypatch.setenv("QAR_CLAUDE_PATH", "qar-test-no-such-claude-binary")
    cfg = _cfg()
    assert resolve_deep_runner_ladder(cfg, warn=False) == []
    assert derive_capabilities(cfg)["code"] is False


def test_explicitly_disabling_execution_still_allows_a_wired_writer(tmp_path, monkeypatch):
    """``deep_runner=None`` means "no Claude Code", not "no execution of any kind" — a consumer
    that also wired a writer asked for exactly the fast path and nothing else."""
    cfg = _cfg(deep_runner=None, file_writer=FilesWriter(str(tmp_path)))
    ladder = resolve_deep_runner_ladder(cfg, warn=False)
    assert len(ladder) == 1 and isinstance(ladder[0], FastEditRunner)


# --- 2. escalation runs on the loop that already existed -----------------------------------------

def test_a_first_rung_that_fails_verification_hands_the_goal_to_the_next(tmp_path):
    """THE claim from the research report, driven through the real goal loop: the escalation is
    the loop's existing verify-and-retry, with the ladder indexed by attempt."""
    provider = StubProvider([])
    fast = RecordingRunner(DeepResult(met=True, output="I edited the file"), name="fast")
    deep = RecordingRunner(DeepResult(met=True, output="did the real work"), name="deep")
    orch = _orch(provider, [fast, deep],
                 verdicts=[{"met": False, "reason": "the edit did not cover the API change"},
                           {"met": True}])

    res = orch._run_deep(PlanDecision(action="deep", goal="update the docs and the client",
                                      deep_brief="do it"),
                         "update the docs and the client", "sonnet")

    assert len(fast.calls) == 1, "the cheap rung is tried exactly once"
    assert len(deep.calls) == 1, "the full runner picked the goal up on the next attempt"
    assert res.deep_results[0].met is True


def test_a_first_rung_that_produces_nothing_still_escalates(tmp_path):
    """A rung that DECLINES (no candidate file, missing binary, timeout) returns an error. The
    pre-ladder rule made that terminal, which was right when there was nothing to fall through
    to; on a ladder it must reach the next rung instead of stranding the work."""
    provider = StubProvider([])
    fast = RecordingRunner(DeepResult(met=False, output="", error="fast edit: no candidate file"))
    deep = RecordingRunner(DeepResult(met=True, output="did the real work"))
    orch = _orch(provider, [fast, deep], verdicts=[{"met": True}])

    res = orch._run_deep(PlanDecision(action="deep", goal="do the thing", deep_brief="do it"),
                         "do the thing", "sonnet")

    assert len(fast.calls) == 1
    assert len(deep.calls) == 1
    assert res.deep_results[0].met is True
    assert res.deep_results[0].output == "did the real work"


def test_a_first_rung_that_succeeds_never_reaches_the_second(tmp_path):
    provider = StubProvider([])
    fast = RecordingRunner(DeepResult(met=True, output="edited docs/notes.md"))
    deep = RecordingRunner(DeepResult(met=True, output="should not run"))
    orch = _orch(provider, [fast, deep], verdicts=[{"met": True}])

    res = orch._run_deep(PlanDecision(action="deep", goal="fix the typo", deep_brief="fix it"),
                         "fix the typo", "sonnet")

    assert len(fast.calls) == 1
    assert deep.calls == [], "a verified fast edit must not also spawn the full agent"
    assert res.deep_results[0].output == "edited docs/notes.md"


def test_a_one_rung_ladder_still_treats_a_silent_failure_as_terminal(tmp_path):
    """The regression guard for the change above: with nothing to fall through TO, an error with
    no output ends the goal after one attempt, exactly as before."""
    provider = StubProvider([])
    only = RecordingRunner(DeepResult(met=False, output="", error="binary not found"))
    orch = _orch(provider, [only], verdicts=[{"met": True}])

    orch._run_deep(PlanDecision(action="deep", goal="do the thing", deep_brief="do it"),
                   "do the thing", "sonnet")

    assert len(only.calls) == 1


def test_the_terminal_rung_repeats_for_further_attempts(tmp_path):
    """The ladder is indexed ``min(attempt - 1, len - 1)``, the same shape as the model ladder, so
    a 3rd attempt stays on the full runner rather than falling off the end."""
    provider = StubProvider([])
    fast = RecordingRunner(DeepResult(met=True, output="a fast edit"))
    deep = RecordingRunner(DeepResult(met=True, output="the real work"))
    orch = _orch(provider, [fast, deep],
                 verdicts=[{"met": False, "reason": "no"}, {"met": False, "reason": "still no"},
                           {"met": True}])

    orch._run_deep(PlanDecision(action="deep", goal="do the thing", deep_brief="do it"),
                   "do the thing", "sonnet")

    assert len(fast.calls) == 1
    assert len(deep.calls) == 2


def test_a_pinned_runner_override_is_never_prefixed_with_the_fast_editor(tmp_path):
    """A pinned runner (the queued deferred hand-off) is a deliberate routing decision. Quietly
    running something else first would override a choice the caller already made."""
    provider = StubProvider([])
    fast = RecordingRunner(DeepResult(met=True, output="a fast edit"))
    pinned = RecordingRunner(DeepResult(met=True, output="queued"))
    orch = _orch(provider, [fast, RecordingRunner()], verdicts=[{"met": True}])

    orch._run_deep(PlanDecision(action="deep", goal="do the thing", deep_brief="do it"),
                   "do the thing", "sonnet", runner_override=pinned)

    assert fast.calls == []
    assert len(pinned.calls) == 1


def test_a_classifier_selected_runner_is_never_prefixed_either(tmp_path):
    provider = StubProvider([])
    fast = RecordingRunner(DeepResult(met=True, output="a fast edit"))
    named = RecordingRunner(DeepResult(met=True, output="named runner ran"))
    orch = Orchestrator(retrieval=StubRetrieval(), provider=provider,
                        registry=ModelRegistry(provider),
                        deep_runner_ladder=[fast, RecordingRunner()],
                        deep_runners={"data": named},
                        deep_runner_classifier=lambda message, goal, brief: "data")
    orch._verify_goal = lambda *a, **k: ({"met": True}, None)

    orch._run_deep(PlanDecision(action="deep", goal="do the thing", deep_brief="do it"),
                   "do the thing", "sonnet")

    assert fast.calls == []
    assert len(named.calls) == 1


def test_each_rung_is_probed_for_its_own_optional_kwargs(tmp_path):
    """The rungs are different objects with different signatures, so the "does this runner accept
    ``emit``/``context_preamble``?" probe has to be per runner. Computing it once against the
    first rung would silently drop streaming and context for the second."""
    provider = StubProvider([])

    class MinimalRunner:
        def __init__(self):
            self.calls = []

        def run_goal(self, *, goal, brief, model=None, max_turns=None) -> DeepResult:
            self.calls.append(goal)
            return DeepResult(met=False, output="tried and fell short")

    class RichRunner(RecordingRunner):
        def run_goal(self, *, goal, brief, model=None, max_turns=None, emit=None,
                     context_preamble=None, run_id=None) -> DeepResult:
            self.calls.append({"goal": goal, "context_preamble": context_preamble,
                               "run_id": run_id})
            return DeepResult(met=True, output="the real work")

    minimal, rich = MinimalRunner(), RichRunner()
    orch = _orch(provider, [minimal, rich],
                 verdicts=[{"met": False, "reason": "not enough"}, {"met": True}])

    orch._run_deep(PlanDecision(action="deep", goal="do the thing", deep_brief="do it"),
                   "do the thing", "sonnet", rep_preamble="PERSONA")

    assert len(minimal.calls) == 1, "the narrow signature was called without the extra kwargs"
    assert len(rich.calls) == 1
    assert rich.calls[0]["context_preamble"] == "PERSONA"
    assert rich.calls[0]["run_id"]


# --- end to end: an opted-in consumer really edits a file, in process ----------------------------

def test_an_opted_in_consumer_edits_a_real_file_through_the_ladder(tmp_path):
    """The whole path: ladder rung 1 is the fast editor, it calls the provider once, and the file
    on disk changes. No subprocess, no agent."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "notes.md").write_text("Status: paused\n")

    class OneShotProvider(StubProvider):
        def answer(self, messages, *, model, system=None, layers=None) -> str:
            return "notes.md\n```\nStatus: active\n```\n"

    provider = OneShotProvider([])
    writer = FilesWriter(str(corpus), backup_dir=str(tmp_path / "backups"))
    fast = FastEditRunner(provider=provider, writer=writer, registry=ModelRegistry(provider))
    deep = RecordingRunner(DeepResult(met=True, output="should not be needed"))
    orch = _orch(provider, [fast, deep], verdicts=[{"met": True}])

    res = orch._run_deep(PlanDecision(action="deep", goal="set the status in notes.md to active",
                                      deep_brief="edit it"),
                         "set the status in notes.md to active", "sonnet")

    assert (corpus / "notes.md").read_text() == "Status: active\n"
    assert deep.calls == []
    assert res.deep_results[0].met is True
