"""The hub assembly's requirements report, the doctor command, the startup notice, the lock note and the .env.example block.

All offline: no model call, no network, and no variable's value is ever read into an assertion except to prove it is absent.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
from esoi.requirements import Entry, env

from quest_ai_runner.the_consciousness.terminal import cli_doctor
from quest_ai_runner.the_self.assembler import hub_lock_health, hub_requirements
from quest_ai_runner.the_self.assembler.hub_requirements import Report

ROOT = Path(__file__).resolve().parent.parent
SECRET = "not-a-real-key-value-123"


def unmet_entries(*, required: bool = True):
    return ([Entry(env("QAR_TEST_MISSING_KEY", secret=True, required=required), ("vendor-model-caller:test",))], {})


def test_report_lists_unmet_with_remedy_and_never_a_value(monkeypatch):
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: unmet_entries())
    monkeypatch.delenv("QAR_TEST_MISSING_KEY", raising=False)
    report = hub_requirements.requirements_report({"OTHER": SECRET})
    text = "\n".join(report.lines())
    assert [item.kind for item in report.unmet()] == ["env"]
    assert "remedy: set QAR_TEST_MISSING_KEY" in text
    assert SECRET not in text and SECRET not in json.dumps(report.as_json())
    assert report.exit_code == 1


def test_report_is_clean_when_the_value_is_present(monkeypatch):
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: unmet_entries())
    report = hub_requirements.requirements_report({"QAR_TEST_MISSING_KEY": SECRET})
    assert report.unmet() == [] and report.exit_code == 0
    assert SECRET not in "\n".join(report.lines())


def test_optional_unmet_does_not_fail(monkeypatch):
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: unmet_entries(required=False))
    report = hub_requirements.requirements_report({})
    assert report.unmet() == [] and report.exit_code == 0


def test_real_assembly_report_runs_offline_and_names_unresolved_seats():
    report = hub_requirements.requirements_report({})
    assert report.items, "the selected hub arms declare at least one requirement"
    assert all(seat in hub_requirements.hub.seats() for seat in report.unresolved)


def test_startup_notice_is_silent_unless_something_required_is_unmet(monkeypatch):
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: unmet_entries(required=False))
    assert hub_requirements.startup_notice({}) is None
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: unmet_entries())
    notice = hub_requirements.startup_notice({})
    assert notice and "quest-ai-runner doctor" in notice and "—" not in notice and "\n" not in notice
    assert hub_requirements.startup_notice({"QAR_TEST_MISSING_KEY": SECRET}) is None


def test_startup_notice_never_raises(monkeypatch):
    def boom(assembler=None):
        raise RuntimeError("broken hub")
    monkeypatch.setattr(hub_requirements, "selected_entries", boom)
    assert hub_requirements.startup_notice({}) is None


def test_session_hands_the_notice_to_the_startup_notifier(monkeypatch):
    from quest_ai_runner.the_consciousness.terminal import interactive_session
    monkeypatch.setattr(hub_requirements, "startup_notice", lambda *a, **k: "1 required setup item(s) unmet; run `quest-ai-runner doctor` for the fix.")
    assert "doctor" in interactive_session._hub_requirements_notice()
    shown = []
    notify = interactive_session._make_startup_notifier(None, [], shown.append)
    notify(interactive_session._hub_requirements_notice())
    assert len(shown) == 1


def doctor_args(**kw):
    return argparse.Namespace(json=kw.get("json", False), lock=kw.get("lock"), note_quest=kw.get("note_quest"), state=kw.get("state"))


def test_doctor_prints_remedy_and_exits_one_when_required_is_unmet(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: unmet_entries())
    code = cli_doctor._cmd_doctor(doctor_args(lock=str(tmp_path / "none.json")), logging.getLogger("t"))
    out = capsys.readouterr().out
    assert code == 1
    assert "[unmet]" in out and "remedy: set QAR_TEST_MISSING_KEY" in out
    assert "no lock file" in out


def test_doctor_json_and_exit_zero_when_all_met(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: ([], {}))
    code = cli_doctor._cmd_doctor(doctor_args(json=True, lock=str(tmp_path / "none.json")), logging.getLogger("t"))
    data = json.loads(capsys.readouterr().out)
    assert code == 0 and data["exit"] == 0 and data["lock"] is None


PLAN = {"schema": "esoi.lock.status/1", "state": "blocked", "exit": 4, "apply_safe": "esoi lock --update --safe",
        "updates": [{"solution": "store", "old_arm": "a", "new_arm": "b", "evidence": "b beat a (p=0.01)", "blocked": True,
                     "steps": [{"order": 1, "action": "set-environment", "command": "", "detail": "set SOME_KEY (signup link in the plan)", "human": True},
                               {"order": 2, "action": "apply", "command": "esoi lock --update --solution store", "detail": "", "human": False}]}]}


def test_note_has_ordered_steps_and_flags_human_steps():
    note = hub_lock_health.render_note(4, PLAN)
    assert "[BLOCKED]" in note and "1. set-environment" in note and "needs a person" in note
    assert "`esoi lock --update --solution store`" in note and hub_lock_health.HOW_TO in note
    assert "—" not in note


def test_same_plan_is_noted_once(tmp_path):
    state = str(tmp_path / "state.json")
    print_id = hub_lock_health.fingerprint(4, PLAN)
    assert hub_lock_health.noted_before(state, print_id) is False
    assert hub_lock_health.noted_before(state, print_id) is True
    changed = json.loads(json.dumps(PLAN))
    changed["updates"][0]["new_arm"] = "c"
    assert hub_lock_health.noted_before(state, hub_lock_health.fingerprint(4, changed)) is False


def test_no_lock_file_means_nothing_to_notice(tmp_path):
    assert hub_lock_health.lock_plan(str(tmp_path / "esoi.lock.json")) is None


def test_doctor_reports_a_blocked_lock_with_its_exit_code(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: ([], {}))
    lock = tmp_path / "esoi.lock.json"
    lock.write_text("{}")
    monkeypatch.setattr(hub_lock_health, "lock_plan", lambda path, assembler=None: (4, PLAN))
    code = cli_doctor._cmd_doctor(doctor_args(lock=str(lock)), logging.getLogger("t"))
    assert code == 4 and "[BLOCKED]" in capsys.readouterr().out


def test_env_example_block_is_the_hubs_and_no_hub_variable_is_repeated_outside_it():
    text = (ROOT / ".env.example").read_text()
    assert hub_requirements.block_of(text) == hub_requirements.render_block(), \
        "run: python -m quest_ai_runner requirements --write-env-example .env.example"
    outside = text.replace(hub_requirements.block_of(text), "")
    for name in hub_requirements.hub_env_names():
        assert not any(line.split("=", 1)[0].strip().lstrip("# ").strip() == name and "=" in line for line in outside.splitlines()), \
            f"{name} is declared by the hub; QAR's part of .env.example must not repeat it"


def test_env_descriptions_do_not_repeat_hub_variables():
    rows = (ROOT / "esoi" / "env_descriptions.csv").read_text().splitlines()[1:]
    own = {row.split(",", 1)[0] for row in rows}
    assert not own & set(hub_requirements.hub_env_names())


def test_selected_implementations_are_coherent_offline():
    result = subprocess.run([str(Path(sys.executable).parent / "esoi"), "requirements", "--package", "quest_ai_runner", "--coherence", "--offline"],
                            capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
