"""The requirement check sees what the lane's config supplies, so a lane that sets ``corpus_root`` (or its Quest address, key and team) in its config file is not
told that ``QAR_CORPUS_ROOT`` and the Quest variables are unmet. The process environment still wins, and nothing is written to it."""
from __future__ import annotations

import os

import pytest

from quest_ai_runner.the_body.configuration import config_environment
from quest_ai_runner.the_body.configuration.config import RunnerConfig
from quest_ai_runner.the_self.assembler.hub_requirements import requirements_report

NAMES = ("QAR_CORPUS_ROOT", "QUEST_BASE_URL", "QUEST_API_KEY", "QUEST_TEAM_ID", "QAR_CONFIG_FILE")


@pytest.fixture
def bare(monkeypatch):
    for name in NAMES:
        monkeypatch.delenv(name, raising=False)
    config_environment.LANE_SUPPLIED.clear()
    yield
    config_environment.LANE_SUPPLIED.clear()


def corpus_item(report):
    return [i for i in report.items if "QAR_CORPUS_ROOT" in i.label]


def test_without_a_lane_config_the_corpus_root_is_unmet(bare):
    items = corpus_item(requirements_report())
    assert items and items[0].status != "met"


def test_a_corpus_root_the_lane_config_sets_meets_the_requirement_without_touching_the_environment(bare, tmp_path):
    config_environment.note_lane_config(RunnerConfig(corpus_root=str(tmp_path), quest_base_url="https://quest.example.org", quest_api_key="qsk_fake", team_id="team_fake"))
    report = requirements_report()
    assert corpus_item(report)[0].status == "met"
    assert not [i for i in report.items if ("QUEST_BASE_URL" in i.label or "QUEST_TEAM_ID" in i.label) and i.status != "met"]
    assert "QAR_CORPUS_ROOT" not in os.environ


def test_the_file_named_by_qar_config_file_is_read_when_no_config_was_loaded(bare, tmp_path, monkeypatch):
    path = tmp_path / "qar.toml"
    path.write_text(f'corpus_root = "{tmp_path}"\n')
    monkeypatch.setenv("QAR_CONFIG_FILE", str(path))
    assert corpus_item(requirements_report())[0].status == "met"


def test_an_unreadable_config_file_changes_nothing_and_the_process_environment_wins(bare, tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_CONFIG_FILE", str(tmp_path / "missing.toml"))
    assert corpus_item(requirements_report())[0].status != "met"
    config_environment.note_lane_config(RunnerConfig(corpus_root="/from/the/config"))
    assert config_environment.lane_supplied_environment()["QAR_CORPUS_ROOT"] == "/from/the/config"
    monkeypatch.setenv("QAR_CORPUS_ROOT", str(tmp_path))
    assert corpus_item(requirements_report())[0].status == "met"
