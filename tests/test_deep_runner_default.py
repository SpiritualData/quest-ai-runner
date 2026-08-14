"""``RunnerConfig.deep_runner`` is ON BY DEFAULT, with the same tri-state as the assembler.

Before this, the field defaulted to ``None``, which conflated two very different things: "the
consumer never wired a worker" and "the consumer deliberately wants no execution". A consumer that
simply did not know it had to construct a ``SubprocessGoalRunner`` got the second behaviour: every
request for real work was planned and then silently dropped (which is how the false "Executing:
<goal>" completion report in ``test_no_deep_executor_honesty.py`` reached a live user).

The tri-state, mirroring ``resolve_context_assembler``:

  * UNSET (the ``_AUTO_DEEP_RUNNER`` sentinel) -> build the default ``SubprocessGoalRunner``
    pointed at ``claude`` on PATH, or degrade to ``None`` with a loud warning if it isn't there.
  * an INSTANCE -> used exactly as given.
  * ``None`` -> execution explicitly disabled, silently (an intentional consumer choice).

Fully offline: no ``claude`` subprocess is ever spawned. The tests only exercise the RESOLUTION
(a PATH lookup and a constructor call); the autouse ``deep_runner_default_is_inert_in_tests``
fixture in conftest points QAR_CLAUDE_PATH at a nonexistent binary for the rest of the suite, and
each test here overrides it explicitly for the branch it covers.
"""

from __future__ import annotations

import logging
import os

import pytest

from quest_ai_runner.config import (
    _AUTO_DEEP_RUNNER,
    RunnerConfig,
    build_orchestrator,
    derive_capabilities,
    resolve_deep_runner,
)
from quest_ai_runner.core.goal_runner import SubprocessGoalRunner
from tests.conftest import StubDeepRunner, StubProvider, StubRetrieval


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """An executable file that stands in for the ``claude`` binary on PATH. Never invoked: the
    resolution only has to FIND it."""
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("QAR_CLAUDE_PATH", "claude")
    return binary


def _cfg(**kwargs) -> RunnerConfig:
    return RunnerConfig(
        retrieval=StubRetrieval({"README.md": "hi"}),
        model_provider=StubProvider([]),
        **kwargs,
    )


# --- the field's own default ----------------------------------------------

def test_field_default_is_the_sentinel_not_none():
    """``None`` has to stay available as an EXPLICIT signal, so it cannot also be the default."""
    assert RunnerConfig.deep_runner is _AUTO_DEEP_RUNNER
    assert _cfg().deep_runner is _AUTO_DEEP_RUNNER


# --- unset -> auto-build ---------------------------------------------------

def test_unset_builds_the_default_subprocess_runner(fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("QAR_DEEP_WORKING_DIR", str(tmp_path))
    runner = resolve_deep_runner(_cfg())
    assert isinstance(runner, SubprocessGoalRunner)
    assert runner.cfg.working_dir == str(tmp_path)
    assert runner.cfg.claude_path == "claude"


def test_working_dir_falls_back_to_corpus_root_then_cwd(fake_claude, monkeypatch, tmp_path):
    """The same precedence the CLI has always used, so there is one set of knobs, not two."""
    monkeypatch.delenv("QAR_DEEP_WORKING_DIR", raising=False)
    runner = resolve_deep_runner(_cfg(corpus_root=str(tmp_path)))
    assert runner.cfg.working_dir == str(tmp_path)

    runner_no_corpus = resolve_deep_runner(_cfg())
    assert runner_no_corpus.cfg.working_dir == os.getcwd()


def test_qar_claude_path_selects_the_binary(fake_claude, monkeypatch, tmp_path):
    other = tmp_path / "my-claude"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    monkeypatch.setenv("QAR_CLAUDE_PATH", str(other))
    runner = resolve_deep_runner(_cfg())
    assert runner.cfg.claude_path == str(other)


# --- unset, but no worker on PATH -> graceful None + a loud warning --------

def test_missing_binary_degrades_to_none_with_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("QAR_CLAUDE_PATH", "definitely-not-a-real-binary-qar")
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.context"):
        runner = resolve_deep_runner(_cfg())
    assert runner is None, "never hand back a runner that would fail on every spawn"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("definitely-not-a-real-binary-qar" in m for m in warnings), warnings
    # The warning has to say what is lost and how to fix it, not just that something is missing.
    assert any("UNAVAILABLE" in m and "QAR_CLAUDE_PATH" in m for m in warnings), warnings


def test_an_explicit_path_that_is_not_executable_degrades_too(monkeypatch, tmp_path, caplog):
    not_executable = tmp_path / "claude"
    not_executable.write_text("not a program")
    not_executable.chmod(0o644)
    monkeypatch.setenv("QAR_CLAUDE_PATH", str(not_executable))
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.context"):
        assert resolve_deep_runner(_cfg()) is None


def test_resolution_never_raises(monkeypatch):
    """Degrade gracefully, always: a broken default must not stop the runner from starting."""
    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise RuntimeError("PATH lookup exploded")

    monkeypatch.setattr("shutil.which", _boom)
    assert resolve_deep_runner(_cfg()) is None


# --- explicit None -> stays None, silently ---------------------------------

def test_explicit_none_disables_execution_without_a_warning(monkeypatch, caplog):
    monkeypatch.setenv("QAR_CLAUDE_PATH", "definitely-not-a-real-binary-qar")
    with caplog.at_level(logging.WARNING, logger="quest-ai-runner.context"):
        runner = resolve_deep_runner(_cfg(deep_runner=None))
    assert runner is None
    assert not [r for r in caplog.records if "deep runner" in r.getMessage()], (
        "disabling execution on purpose is not a problem to warn about")


def test_explicit_none_is_not_overridden_even_when_claude_is_available(fake_claude):
    assert resolve_deep_runner(_cfg(deep_runner=None)) is None


# --- explicit instance -> used as-is ---------------------------------------

def test_explicit_instance_is_returned_unchanged(fake_claude):
    mine = StubDeepRunner()
    assert resolve_deep_runner(_cfg(deep_runner=mine)) is mine


# --- integration: the sentinel never escapes build_orchestrator ------------

def test_build_orchestrator_resolves_and_writes_back(fake_claude, tmp_path, monkeypatch):
    """Consumers and both chat UIs read ``cfg.deep_runner`` directly to decide whether execution
    is available, so the sentinel must not survive the build."""
    monkeypatch.setenv("QAR_DEEP_WORKING_DIR", str(tmp_path))
    cfg = _cfg()
    orch = build_orchestrator(cfg)
    assert isinstance(cfg.deep_runner, SubprocessGoalRunner)
    assert orch.deep_runner is cfg.deep_runner
    assert orch._has_deep_execution_capability()


def test_build_orchestrator_leaves_an_explicit_none_alone(fake_claude):
    cfg = _cfg(deep_runner=None)
    orch = build_orchestrator(cfg)
    assert cfg.deep_runner is None
    assert orch.deep_runner is None
    assert not orch._has_deep_execution_capability()


def test_capabilities_report_the_resolved_default(fake_claude, tmp_path, monkeypatch):
    """The heartbeat must report what the runner can ACTUALLY do. An unset field used to report
    code=False while the auto-built default would have executed the work perfectly well."""
    monkeypatch.setenv("QAR_DEEP_WORKING_DIR", str(tmp_path))
    assert derive_capabilities(_cfg())["code"] is True
    assert derive_capabilities(_cfg(deep_runner=None))["code"] is False
