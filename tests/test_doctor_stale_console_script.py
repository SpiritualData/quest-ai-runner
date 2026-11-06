"""`quest-ai-runner doctor` flags an installed console script that imports a module which no longer exists.

Run: python3 -m pytest tests/test_doctor_stale_console_script.py -q -p no:cacheprovider
"""
from quest_ai_runner.the_consciousness.terminal import cli_doctor


def write_script(directory, module, name):
    script = directory / "quest-ai-runner"
    script.write_text(f"#!/usr/bin/env python\nimport sys\nfrom {module} import {name}\nsys.exit({name}())\n")
    return script


def test_healthy_script_reports_no_problem(tmp_path):
    write_script(tmp_path, "quest_ai_runner.the_consciousness.terminal.cli", "main")
    assert cli_doctor.stale_console_script_problems(tmp_path) == []


def test_script_importing_a_removed_module_prints_the_fix(tmp_path):
    write_script(tmp_path, "quest_ai_runner.cli_that_moved", "main")
    problems = cli_doctor.stale_console_script_problems(tmp_path)
    assert len(problems) == 1
    assert "quest_ai_runner.cli_that_moved.main" in problems[0]
    assert "pip install -e" in problems[0] and "--no-deps" in problems[0]


def test_script_importing_a_removed_name_is_stale(tmp_path):
    write_script(tmp_path, "quest_ai_runner.the_consciousness.terminal.cli", "main_that_was_renamed")
    assert cli_doctor.stale_console_script_problems(tmp_path)


def test_doctor_exits_one_and_names_the_script_when_stale(monkeypatch, capsys, tmp_path):
    import argparse, logging
    from quest_ai_runner.the_self.assembler import hub_requirements
    monkeypatch.setattr(hub_requirements, "selected_entries", lambda assembler=None: ([], {}))
    monkeypatch.setattr(cli_doctor, "stale_console_script_problems", lambda script_dir=None: ["installed script x is stale; fix: pip install -e . --no-deps"])
    args = argparse.Namespace(json=False, lock=str(tmp_path / "none.json"), note_quest=None, state=None)
    assert cli_doctor._cmd_doctor(args, logging.getLogger("t")) == 1
    assert "Console script: STALE" in capsys.readouterr().out
