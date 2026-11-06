"""`quest-ai-runner --check poll` validates and exits; it never builds a Poller or starts the loop. Offline."""
from __future__ import annotations

import logging

import pytest

from quest_ai_runner.the_consciousness.terminal import cli, cli_poll
from quest_ai_runner.the_consciousness.terminal.cli_parser import _build_parser as build_parser
from quest_ai_runner.the_self.assembler import hub_requirements


@pytest.mark.parametrize("argv", [["--check", "poll"], ["poll", "--check"]])
def test_check_flag_survives_either_position(argv):
    assert build_parser().parse_args(argv).check is True


def test_config_and_once_before_the_subcommand_survive():
    args = build_parser().parse_args(["--once", "--config", "x.toml", "poll"])
    assert args.once is True and args.config == "x.toml"


def test_check_never_builds_a_poller_or_runs_the_loop(monkeypatch):
    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("Poller must not be built under --check")

    monkeypatch.setattr(cli_poll, "Poller", Boom)
    monkeypatch.setattr(cli_poll, "_config_from_env", lambda path=None: type("C", (), {
        "lane_label": "t", "runner_label": None, "validate": lambda self: [], "state_path": None,
        "quest_base_url": "u", "quest_api_key": "k", "team_id": None})())
    monkeypatch.setattr(cli_poll, "check_lane", lambda cfg, lane, log, client=None: 7)
    args = build_parser().parse_args(["--check", "poll"])
    assert cli_poll._cmd_poll(args, logging.getLogger("t")) == 7


def test_check_lane_passes_and_fails_clearly(monkeypatch):
    monkeypatch.setattr("quest_ai_runner.the_consciousness.terminal.cli_chat._check_chat_prerequisites", lambda: [])
    monkeypatch.setattr(hub_requirements, "requirements_report", lambda *a, **k: hub_requirements.Report())

    class Client:
        def whoami(self):
            return {"user_id": "u"}

    log = logging.getLogger("t")
    assert cli_poll.check_lane(None, "t", log, client=Client()) == 0

    class Dead:
        def whoami(self):
            raise RuntimeError("down")

    assert cli_poll.check_lane(None, "t", log, client=Dead()) == 1


def test_openclaw_requirements_ignored_unless_a_channel_is_configured():
    report = hub_requirements.requirements_report({"QAR_CORPUS_ROOT": "/x"})
    assert not any("openclaw" in line.lower() for line in report.lines())
    assert not any("HUBOFHUBS_WRITE_ROOT" in i.label for i in report.items)
    with_channel = hub_requirements.requirements_report({"QAR_CHANNEL_PROVIDER": "openclaw"})
    assert any("openclaw" in line.lower() for line in with_channel.lines())
