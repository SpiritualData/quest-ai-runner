"""``runner.lane.run_lane`` — the shared --check/--once/loop driver every executor lane's ``main()``
should be nothing more than a call to (see docs/tutorial-your-first-lane.md).

Three separate consumers of this library hand-rolled this same glue before it moved into the
library (see CHANGELOG.md, Unreleased, and ``lane.py``'s own module docstring); these tests cover
the behavior any of them relies on: the --check/--once/loop dispatch, degrading instead of
crashing on an incomplete config, the .env loader, the state-path default, and that the
local-time-due-gate function is now an inert, logged no-op.
"""
from __future__ import annotations

import logging
import os

import pytest

from quest_ai_runner.config import RunnerConfig
from quest_ai_runner.runner.lane import install_local_time_due_gate, load_env_file, run_lane
from tests.conftest import StubProvider, StubRetrieval


def _complete_cfg(**kwargs) -> RunnerConfig:
    return RunnerConfig(
        quest_base_url="https://quest.example",
        quest_api_key="qsk_test",
        team_id="team_1",
        retrieval=StubRetrieval({"README.md": "hi"}),
        model_provider=StubProvider([]),
        **kwargs,
    )


# --- config-incomplete degradation -----------------------------------------

def test_incomplete_config_degrades_to_exit_0(caplog):
    with caplog.at_level(logging.INFO, logger="t-lane"):
        rc = run_lane([], prog="t", description="d", lane_label="t", log_name="t-lane",
                      build_config=RunnerConfig)  # no key/url/retrieval/provider at all
    assert rc == 0
    assert any("not fully configured" in r.getMessage() for r in caplog.records)


def test_not_configured_keywords_controls_which_problems_degrade(monkeypatch):
    """A problem whose text matches none of ``not_configured_keywords`` does NOT trigger the
    degrade-to-0 path — it is treated as a real (if incomplete) config and the lane proceeds to
    build a Poller, instead of stopping early at ``cfg.validate()``."""
    reached = {"poller_built": False, "ran_once": False}

    def build():
        # missing retrieval + model_provider only (no "key"/"url" substring in those problems)
        return RunnerConfig(quest_base_url="https://quest.example", quest_api_key="qsk_test")

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        reached["poller_built"] = True
        self.cfg = cfg

    def fake_run_once(self):
        reached["ran_once"] = True
        return []

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.run_once", fake_run_once)
    rc = run_lane(["--once"], prog="t", description="d", lane_label="t", log_name="t-lane2",
                  build_config=build, not_configured_keywords=("key", "url"))
    assert rc == 0
    assert reached["poller_built"] and reached["ran_once"], (
        "no keyword matched, so run_lane must not have taken the early degrade-to-0 exit")


# --- --check / --once / loop dispatch --------------------------------------

def test_check_reports_key_status(monkeypatch):
    calls = {}

    class FakeClient:
        def whoami(self):
            calls["called"] = True
            return {"user": "test"}

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        self.cfg = cfg
        self.client = FakeClient()

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    rc = run_lane(["--check"], prog="t", description="d", lane_label="t", log_name="t-lane3",
                  build_config=_complete_cfg)
    assert rc == 0
    assert calls.get("called")


def test_check_reports_failure_as_exit_1(monkeypatch):
    class FakeClient:
        def whoami(self):
            raise RuntimeError("boom")

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        self.cfg = cfg
        self.client = FakeClient()

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    rc = run_lane(["--check"], prog="t", description="d", lane_label="t", log_name="t-lane4",
                  build_config=_complete_cfg)
    assert rc == 1


def test_once_calls_run_once(monkeypatch):
    handled = ["task_1"]

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        self.cfg = cfg

    def fake_run_once(self):
        return handled

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.run_once", fake_run_once)
    rc = run_lane(["--once"], prog="t", description="d", lane_label="t", log_name="t-lane5",
                  build_config=_complete_cfg)
    assert rc == 0


def test_no_flags_calls_run_forever(monkeypatch):
    calls = {"forever": False}

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        self.cfg = cfg

    def fake_run_forever(self):
        calls["forever"] = True

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.run_forever", fake_run_forever)
    rc = run_lane([], prog="t", description="d", lane_label="t", log_name="t-lane6",
                  build_config=_complete_cfg)
    assert rc == 0
    assert calls["forever"] is True


# --- state_path resolution --------------------------------------------------

def test_state_path_defaults_to_qar_state_path_env(monkeypatch, tmp_path):
    seen = {}

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        seen["state_path"] = state_path
        self.cfg = cfg

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.run_once", lambda self: [])
    wanted = str(tmp_path / "custom_state.json")
    monkeypatch.setenv("QAR_STATE_PATH", wanted)
    run_lane(["--once"], prog="t", description="d", lane_label="t", log_name="t-lane7",
             build_config=_complete_cfg)
    assert seen["state_path"] == wanted


def test_explicit_state_path_wins_over_env(monkeypatch, tmp_path):
    seen = {}

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        seen["state_path"] = state_path
        self.cfg = cfg

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.run_once", lambda self: [])
    monkeypatch.setenv("QAR_STATE_PATH", str(tmp_path / "env_state.json"))
    explicit = tmp_path / "explicit" / "state.json"
    run_lane(["--once"], prog="t", description="d", lane_label="t", log_name="t-lane8",
             build_config=_complete_cfg, state_path=explicit)
    assert seen["state_path"] == str(explicit)
    assert explicit.parent.is_dir()  # parent dirs are created


# --- load_env_file -----------------------------------------------------------

def test_load_env_file_sets_unset_vars(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO_LANE_TEST=from_file\n# a comment\nBAR_LANE_TEST=\"quoted\"\n")
    monkeypatch.delenv("FOO_LANE_TEST", raising=False)
    monkeypatch.delenv("BAR_LANE_TEST", raising=False)
    load_env_file(env_file)
    assert os.environ["FOO_LANE_TEST"] == "from_file"
    assert os.environ["BAR_LANE_TEST"] == "quoted"


def test_load_env_file_does_not_override_already_set_vars(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("FOO_LANE_TEST2=from_file\n")
    monkeypatch.setenv("FOO_LANE_TEST2", "from_process")
    load_env_file(env_file)
    assert os.environ["FOO_LANE_TEST2"] == "from_process"


def test_load_env_file_missing_file_is_a_silent_noop(tmp_path):
    load_env_file(tmp_path / "does_not_exist.env")  # must not raise


def test_run_lane_loads_env_file_before_build_config(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("QAR_LANE_TEST_MARKER=hello\n")
    monkeypatch.delenv("QAR_LANE_TEST_MARKER", raising=False)
    seen = {}

    def build():
        seen["marker"] = os.environ.get("QAR_LANE_TEST_MARKER")
        return _complete_cfg()

    def fake_poller_init(self, cfg, *, state_path=None, **kw):
        self.cfg = cfg

    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.__init__", fake_poller_init)
    monkeypatch.setattr("quest_ai_runner.runner.poller.Poller.run_once", lambda self: [])
    # --once: build_config must still run, but the loop-forever path (which would hang the test
    # suite) is never reached.
    run_lane(["--once"], prog="t", description="d", lane_label="t", log_name="t-lane9",
             build_config=build, env_file=env_file)
    assert seen["marker"] == "hello"


# --- install_local_time_due_gate is now an inert, logged no-op -------------

def test_install_local_time_due_gate_is_a_noop_and_never_patches_quest_client(caplog):
    from quest_ai_runner.runner.quest_client import QuestClient

    original = QuestClient.discover_due
    log = logging.getLogger("t-lane-gate")
    with caplog.at_level(logging.DEBUG, logger="t-lane-gate"):
        install_local_time_due_gate(log)
    assert QuestClient.discover_due is original, (
        "the old monkey-patch fallback must not be installed from inside this library — the "
        "library's own runner.poller._due_now_locally is always present here by construction")
    assert any("no lane-level fallback is needed" in r.getMessage() for r in caplog.records)
