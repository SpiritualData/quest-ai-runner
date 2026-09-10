"""A lane is a config file, not a program.

Three lanes on this pattern each kept a Python consumer alive for the same handful of chores: read
my .env, rename three of its variables onto the library's names, set two QAR_* defaults, read a
JSON map, build one client. None of that is business logic; it is config that had no file to live
in, and every lane wrote it again slightly differently. These tests pin the file-only path.
"""
from __future__ import annotations

import json
import os

import pytest

from quest_ai_runner.config import (RunnerConfig, apply_config_environment,
                                    resolve_config_objects)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("LANE_URL", "LANE_TEAM", "QUEST_BASE_URL", "QUEST_TEAM_ID",
              "QAR_MAX_PARALLEL", "QAR_CLAUDE_PATH"):
        monkeypatch.delenv(k, raising=False)


def test_env_files_are_loaded_in_order_with_earlier_files_winning(tmp_path, monkeypatch):
    first = tmp_path / "lane.env"
    first.write_text("LANE_URL=https://lane.example.org\n")
    fallback = tmp_path / "shared.env"
    fallback.write_text("LANE_URL=https://shared.example.org\nLANE_TEAM=team_abc\n")

    cfg = RunnerConfig(env_files=[str(first), str(fallback)])
    apply_config_environment(cfg)

    assert os.environ["LANE_URL"] == "https://lane.example.org"   # earlier file wins
    assert os.environ["LANE_TEAM"] == "team_abc"                  # later file fills the gap


def test_aliases_bridge_a_deployments_own_variable_names(monkeypatch):
    monkeypatch.setenv("LANE_URL", "https://lane.example.org")
    monkeypatch.setenv("LANE_TEAM", "team_abc")

    apply_config_environment(RunnerConfig(
        env_aliases={"QUEST_BASE_URL": "LANE_URL", "QUEST_TEAM_ID": "LANE_TEAM"}))

    assert os.environ["QUEST_BASE_URL"] == "https://lane.example.org"
    assert os.environ["QUEST_TEAM_ID"] == "team_abc"


def test_a_blank_placeholder_does_not_block_the_aliased_value(monkeypatch):
    """The bug that made one lane hand-write its own bridge helper.

    An .env carrying a blank line under the library's OWN name (`QUEST_TEAM_ID=`) counts as
    "already present" to setdefault, so the real value the alias resolved never applied and the
    lane polled the wrong scope with no error anywhere.
    """
    monkeypatch.setenv("QUEST_TEAM_ID", "")          # blank placeholder, as an .env leaves it
    monkeypatch.setenv("LANE_TEAM", "team_abc")

    apply_config_environment(RunnerConfig(env_aliases={"QUEST_TEAM_ID": "LANE_TEAM"}))

    assert os.environ["QUEST_TEAM_ID"] == "team_abc"


def test_a_real_environment_value_still_wins_over_the_file(monkeypatch):
    monkeypatch.setenv("QUEST_TEAM_ID", "team_from_the_unit")
    monkeypatch.setenv("LANE_TEAM", "team_abc")

    apply_config_environment(RunnerConfig(env_aliases={"QUEST_TEAM_ID": "LANE_TEAM"},
                                          env={"QUEST_TEAM_ID": "team_from_the_file"}))

    assert os.environ["QUEST_TEAM_ID"] == "team_from_the_unit"


def test_env_table_reaches_the_knobs_no_toml_field_can_set(monkeypatch):
    """OrchestratorConfig is nested, so no top-level TOML field reaches it. Every lane pinned
    these through os.environ in Python; the [env] table is how a file does it."""
    apply_config_environment(RunnerConfig(env={"QAR_MAX_PARALLEL": "3",
                                               "QAR_CLAUDE_PATH": "/opt/bin/claude"}))
    assert os.environ["QAR_MAX_PARALLEL"] == "3"
    assert os.environ["QAR_CLAUDE_PATH"] == "/opt/bin/claude"


def test_a_json_map_file_becomes_the_quest_folder_map(tmp_path):
    f = tmp_path / "folders.json"
    f.write_text(json.dumps({"quest_1": "/work/one", "quest_2": "/work/two"}))

    cfg = resolve_config_objects(RunnerConfig(quest_folder_map_file=str(f)))

    assert cfg.quest_folder_map == {"quest_1": "/work/one", "quest_2": "/work/two"}


def test_an_inline_map_wins_over_the_file_it_is_merged_with(tmp_path):
    f = tmp_path / "folders.json"
    f.write_text(json.dumps({"quest_1": "/from/file", "quest_2": "/only/in/file"}))

    cfg = resolve_config_objects(RunnerConfig(quest_folder_map_file=str(f),
                                              quest_folder_map={"quest_1": "/inline"}))

    assert cfg.quest_folder_map == {"quest_1": "/inline", "quest_2": "/only/in/file"}


def test_a_missing_map_file_is_not_fatal(tmp_path):
    cfg = resolve_config_objects(RunnerConfig(quest_folder_map_file=str(tmp_path / "nope.json")))
    assert cfg.quest_folder_map is None


def test_context_sources_come_from_a_json_file_too(tmp_path):
    f = tmp_path / "sources.json"
    f.write_text(json.dumps({"quest_1": [{"source": "drive_comments", "folder_id": "F"}]}))

    cfg = resolve_config_objects(RunnerConfig(context_sources_file=str(f)))

    assert cfg.context_sources_map["quest_1"][0]["folder_id"] == "F"


def test_drive_comments_is_built_from_a_credential_description(tmp_path):
    sa = tmp_path / "sa.json"
    sa.write_text("{}")

    cfg = resolve_config_objects(RunnerConfig(
        drive_comments_auth={"service_account_file": str(sa)}))

    assert cfg.drive_comments is not None
    assert type(cfg.drive_comments).__name__ == "DriveComments"


def test_a_live_client_supplied_in_python_is_never_overwritten(tmp_path):
    sa = tmp_path / "sa.json"
    sa.write_text("{}")
    sentinel = object()

    cfg = resolve_config_objects(RunnerConfig(
        drive_comments=sentinel, drive_comments_auth={"service_account_file": str(sa)}))

    assert cfg.drive_comments is sentinel


def test_a_missing_credential_turns_the_channel_off_rather_than_raising(tmp_path):
    cfg = resolve_config_objects(RunnerConfig(
        drive_comments_auth={"service_account_file": str(tmp_path / "absent.json")}))
    assert cfg.drive_comments is None


def test_every_new_field_is_expressible_in_a_toml_file(tmp_path):
    """The point of the exercise: if one of these is not file-expressible, a lane needs Python."""
    f = tmp_path / "qar.toml"
    f.write_text(
        'lane_label = "cantr"\n'
        'state_path = "/var/lib/cantr/state.json"\n'
        'env_files = ["/etc/cantr.env"]\n'
        'quest_folder_map_file = "/etc/folders.json"\n'
        'context_sources_file = "/etc/sources.json"\n'
        '[env_aliases]\nQUEST_BASE_URL = "LANE_URL"\n'
        '[env]\nQAR_MAX_PARALLEL = "3"\n'
        '[drive_comments_auth]\nservice_account_file = "/etc/sa.json"\n'
    )

    cfg = RunnerConfig.from_file(str(f))

    assert cfg.lane_label == "cantr"
    assert cfg.state_path == "/var/lib/cantr/state.json"
    assert cfg.env_files == ["/etc/cantr.env"]
    assert cfg.env_aliases == {"QUEST_BASE_URL": "LANE_URL"}
    assert cfg.env == {"QAR_MAX_PARALLEL": "3"}
    assert cfg.drive_comments_auth == {"service_account_file": "/etc/sa.json"}


def test_a_blank_line_in_the_first_env_file_falls_through_to_the_next(tmp_path, monkeypatch):
    """A lane's own .env may document optional credentials as blank lines.

    Cantr's .env does exactly this: QUEST_API_URL/_KEY/JOSHUA_USER_ID are blank, meaning "use the
    shared personal file". A presence check reads those blanks as already-set, the fallback never
    applies, and the lane starts unconfigured with nothing in the log saying why. That is why the
    consumer carried a hand-written bridge helper.
    """
    own = tmp_path / "lane.env"
    own.write_text("LANE_URL=\nLANE_TEAM=team_cantr\n")
    shared = tmp_path / "shared.env"
    shared.write_text("LANE_URL=https://shared.example.org\nLANE_TEAM=team_personal\n")

    apply_config_environment(RunnerConfig(env_files=[str(own), str(shared)]))

    assert os.environ["LANE_URL"] == "https://shared.example.org"   # blank fell through
    assert os.environ["LANE_TEAM"] == "team_cantr"                  # real value still won
