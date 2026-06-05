"""The shipped examples must import cleanly and actually wire up a working brain — offline.

These tests guard against the examples rotting: they import every example module, exercise
``custom_consumer.build_config`` from the environment, and drive the example's own StubProvider
through the REAL Orchestrator to prove both the answer and the confirm/escalate branches work.
No network and no API key — same offline approach as the runner tests.
"""
import importlib

from quest_ai_runner.config import RunnerConfig, build_orchestrator
from quest_ai_runner.core.adapters import Escalation

from .conftest import StubEscalation, StubRetrieval


def test_all_example_modules_import():
    # Importing proves the modules are syntactically valid and their imports resolve.
    for name in ("examples", "examples.custom_consumer", "examples.run_lane", "examples.e2e_demo"):
        assert importlib.import_module(name) is not None


def test_custom_consumer_build_config_from_env(monkeypatch, tmp_path):
    from examples import custom_consumer

    (tmp_path / "README.md").write_text("fact: the sky is blue")
    monkeypatch.setenv("QUEST_BASE_URL", "https://api.example.org")
    monkeypatch.setenv("QUEST_API_KEY", "qsk_example")
    monkeypatch.setenv("QUEST_TEAM_ID", "team_example")
    monkeypatch.setenv("QAR_CORPUS_ROOT", str(tmp_path))

    cfg = custom_consumer.build_config(with_model_provider=False)
    assert isinstance(cfg, RunnerConfig)
    assert cfg.quest_base_url == "https://api.example.org"
    assert cfg.quest_api_key == "qsk_example"
    assert cfg.team_id == "team_example"
    assert cfg.corpus_root == str(tmp_path)
    assert cfg.retrieval is not None      # FilesAdapter over the corpus
    assert cfg.deep_runner is not None    # SubprocessGoalRunner wired when a corpus is set

    # Only the model provider is missing (we asked to omit it); everything else validates.
    assert custom_consumer.build_config.__doc__  # has docs
    problems = cfg.validate()
    assert problems == ["a model_provider is required"]


def test_custom_consumer_incomplete_when_env_missing(monkeypatch):
    from examples import custom_consumer

    for var in ("QUEST_BASE_URL", "QUEST_API_KEY", "QUEST_TEAM_ID", "QAR_CORPUS_ROOT"):
        monkeypatch.delenv(var, raising=False)
    cfg = custom_consumer.build_config(with_model_provider=False)
    problems = cfg.validate()
    assert "quest_base_url is required" in problems
    assert "quest_api_key (qsk_...) is required" in problems


def test_example_stub_provider_drives_real_brain_answer():
    """The e2e_demo StubProvider + the real Orchestrator: an ordinary task -> grounded answer."""
    from examples.e2e_demo import StubProvider

    cfg = RunnerConfig(
        quest_base_url="https://x", quest_api_key="qsk_x", team_id="t",
        retrieval=StubRetrieval({"README.md": "fact: yes"}),
        model_provider=StubProvider(),
    )
    orch = build_orchestrator(cfg)
    result = orch.run("What is the capital of France?")
    assert result.kind == "answer"
    assert "STUB-ANSWER" in result.text


def test_example_stub_provider_drives_real_brain_confirm():
    """A human-only task (carries the marker) -> the brain raises a confirm via the EscalationSink."""
    from examples.e2e_demo import HUMAN_ONLY_MARKER, StubProvider

    esc = StubEscalation(decision_id="dec_demo")
    cfg = RunnerConfig(
        quest_base_url="https://x", quest_api_key="qsk_x", team_id="t",
        retrieval=StubRetrieval({"README.md": "fact: yes"}),
        model_provider=StubProvider(),
        escalation=esc,
    )
    orch = build_orchestrator(cfg)
    result = orch.run(f"{HUMAN_ONLY_MARKER} Approve and send the partner agreement.")
    assert result.kind == "confirm"
    assert len(esc.raised) == 1
    assert isinstance(esc.raised[0], Escalation)
