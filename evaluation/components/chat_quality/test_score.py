"""Offline tests for the chat_quality component wiring: the scorer reads the record, the smoke cases are valid, the live arm
SKIPs without a dev Quest, and world.py still re-exports the data that world_spec.py now holds.
Run:  .venv/bin/python3 -m pytest evaluation/components/chat_quality/test_score.py -q"""
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import common  # noqa: E402
import runner  # noqa: E402
import world  # noqa: E402
import world_spec  # noqa: E402
from chat_quality import score  # noqa: E402


def test_scorer_reads_pass_flag_hard_failures_and_errors():
    assert score.score_case(lambda c: {"passed": True, "score": 0.9, "judged": {"summary": "fine"}}, {})["ok"]
    bad = score.score_case(lambda c: {"passed": False, "score": 0.0, "precheck": {"hard_failures": ["routing"]}}, {})
    assert not bad["ok"] and "routing" in bad["note"]
    err = score.score_case(lambda c: {"error": "boom", "passed": False, "score": None}, {})
    assert not err["ok"] and err["got"] == "error"


def test_smoke_cases_are_valid_dataset_cases():
    cases = common.load_cases("chat_quality")
    assert [c["id"] for c in cases] == ["EX-READ-1", "EX-WRITE-1"]
    assert runner.load_example_cases() == cases
    for case in cases:
        runner.validate_case(case, "cases.json")


def test_live_arm_skips_without_a_dev_quest(monkeypatch, capsys):
    monkeypatch.delenv("QAR_EVAL_DEV_ENV_FILE", raising=False)
    from chat_quality.impls import dev_quest_chat
    with pytest.raises(SystemExit) as stop:
        dev_quest_chat.build(None)
    assert stop.value.code == 0 and "SKIPPED" in capsys.readouterr().out


def test_world_reexports_the_spec_data():
    for name in ("PIVOTS", "QUESTS", "COLLECTIONS", "TEAM_CONTEXT", "AUTOPILOT_BY_QUEST", "MONTH_PERIOD", "ago", "ahead", "week_period", "habit_spec"):
        assert getattr(world, name) is getattr(world_spec, name)
    assert len(world.PIVOTS) == 17 and len(world.QUESTS) == 5
