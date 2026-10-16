"""Standing instructions reach every deep prompt, by default and whoever the run acts as."""
from quest_ai_runner.core import standing_instructions as si
import importlib.util
from pathlib import Path

import pytest

from quest_ai_runner.core.goal_runner import compose_goal_prompt

SYNC_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "sync_standing_instructions.py"


def default_text() -> str:
    return si.DEFAULT_INSTRUCTIONS_FILE.read_text(encoding="utf-8").strip()


def test_default_ships_and_is_applied_when_unset(monkeypatch):
    monkeypatch.delenv(si.DEEP_INSTRUCTIONS_ENV, raising=False)
    assert si.DEFAULT_INSTRUCTIONS_FILE.is_file()
    assert "Evolvable Structure that Optimizes Intent" in default_text()
    prompt = compose_goal_prompt("done", "do the task")
    assert default_text() in prompt
    assert prompt.index(default_text()) < prompt.index("TASK:")


def test_a_persona_preamble_does_not_replace_them(monkeypatch):
    monkeypatch.delenv(si.DEEP_INSTRUCTIONS_ENV, raising=False)
    prompt = compose_goal_prompt("done", "do the task",
                                 preamble="=== ACT AS THIS PERSON (their persona) ===\nA rep.")
    assert "ACT AS THIS PERSON" in prompt
    assert default_text() in prompt


def test_empty_setting_turns_the_layer_off(monkeypatch):
    monkeypatch.setenv(si.DEEP_INSTRUCTIONS_ENV, "")
    assert si.load_standing_instructions() == ""
    assert default_text() not in compose_goal_prompt("done", "do the task")


def test_path_list_with_default_token_in_order(monkeypatch, tmp_path):
    extra = tmp_path / "org_rules.md"
    extra.write_text("Org rule: cite sources.\n", encoding="utf-8")
    monkeypatch.setenv(si.DEEP_INSTRUCTIONS_ENV, f"default:{extra}".replace(":", __import__("os").pathsep))
    text = si.load_standing_instructions()
    assert text.index("Evolvable Structure that Optimizes Intent") < text.index("Org rule: cite sources.")


def test_edits_apply_without_restart_and_unreadable_files_are_skipped(monkeypatch, tmp_path):
    own = tmp_path / "rules.md"
    own.write_text("first", encoding="utf-8")
    missing = tmp_path / "missing.md"
    monkeypatch.setenv(si.DEEP_INSTRUCTIONS_ENV, f"{missing}{__import__('os').pathsep}{own}")
    assert si.load_standing_instructions() == "first"
    own.write_text("second", encoding="utf-8")
    assert si.load_standing_instructions() == "second"


def test_never_cut_by_the_budget(monkeypatch):
    monkeypatch.delenv(si.DEEP_INSTRUCTIONS_ENV, raising=False)
    huge_preamble = "context block\n\n" + ("filler " * 40_000)
    prompt = compose_goal_prompt("done", "do the task", preamble=huge_preamble, budget_tokens=8_000)
    assert default_text() in prompt


def _sync_module():
    spec = importlib.util.spec_from_file_location("sync_standing_instructions", SYNC_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_shipped_default_matches_the_skill_it_is_generated_from():
    """The shipped copy is derived from SKILL.md; where the skill exists, they must not drift."""
    sync = _sync_module()
    skill = sync.skill_path()
    if not skill.is_file():
        pytest.skip(f"skill not on this machine: {skill}")
    expected = sync.build(skill.read_text(encoding="utf-8"))
    assert si.DEFAULT_INSTRUCTIONS_FILE.read_text(encoding="utf-8") == expected, (
        "prompts/evolvable_structure.md drifted from the skill: run scripts/sync_standing_instructions.py")
