"""The quest field standard is ONE: the backend's YAML, vendored, and everything is built from it."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from quest_ai_runner.core import quest_field_instructions as qfi
from quest_ai_runner.core.quest_field_instructions import (
    GUIDANCE_PATH, ITEM_INSTRUCTIONS, QUEST_FIELD_INSTRUCTIONS, UNIVERSAL_RULES, field_instructions_text,
    render_compact_block, render_markdown, what_a_quest_is)
from quest_ai_runner.core.tools_quest_standard import (
    QUEST_FIELD_TOOLS, add_measurable_outcome_spec, add_quest_strategy_spec, create_quest_goal_spec,
    create_quest_habit_spec, quest_field_tool_spec)

ROOT = Path(__file__).resolve().parent.parent
BACKEND_YAML = ROOT.parent / "quest-backend" / "app" / "prompts" / "quest_field_guidance.yaml"
DOC = yaml.safe_load(GUIDANCE_PATH.read_text(encoding="utf-8"))


def _entries():
    for section in ("fields", "other_fields"):
        for name, entry in DOC[section].items():
            if not entry.get("alias_of"):
                yield name, entry


@pytest.mark.parametrize("name,entry", list(_entries()))
def test_every_yaml_field_has_an_instruction_with_its_esoi_and_every_rule(name, entry):
    spec = QUEST_FIELD_INSTRUCTIONS[name]
    text = spec.render()
    assert entry["esoi"].strip() in text
    assert entry["summary"].strip() in text
    for rule in entry["rules"]:
        assert rule in text
    for item in [*entry.get("good", []), *entry.get("bad", []), *entry.get("does_not_belong", [])]:
        assert item in text
    assert spec.weak_example == entry["bad"][0] and spec.strong_example == entry["good"][0]
    assert qfi.instruction_for(name) is spec


def test_alias_resolves_and_universal_rules_are_the_yamls():
    assert qfi.instruction_for("preferences_text") is QUEST_FIELD_INSTRUCTIONS["preferences"]
    assert UNIVERSAL_RULES == tuple(DOC["universal_rules"])


def test_strategy_item_is_the_yaml_entry_plus_only_the_runner_suffix():
    strategies = DOC["fields"]["strategies"]
    item = ITEM_INSTRUCTIONS["strategy"]
    assert item.what_it_is == strategies["esoi"].strip() + " " + qfi.STRATEGY_RUNNER_SUFFIX
    assert item.rules == tuple(strategies["rules"])
    assert "Candidate Solutions" in item.what_it_is and "approach the person commits" not in item.what_it_is


def test_goals_and_habits_are_implementations_in_motion_and_never_a_source_of_fields():
    goal = ITEM_INSTRUCTIONS["goal"].what_it_is
    assert "Implementation in motion" in goal and "never the source for an acceptance criterion" in goal
    assert "Implementation in motion" in ITEM_INSTRUCTIONS["habit"].what_it_is


def test_vendored_yaml_equals_the_backends():
    if not BACKEND_YAML.is_file():
        pytest.skip("quest-backend checkout not next to this repo")
    assert GUIDANCE_PATH.read_bytes() == BACKEND_YAML.read_bytes(), (
        "drift: run scripts/sync_quest_field_guidance.py")


def test_no_other_module_defines_field_instructions():
    marker = re.compile(r"what_it_is=|good_looks_like=")
    offenders = sorted(
        str(p.relative_to(ROOT)) for p in (ROOT / "quest_ai_runner").rglob("*.py")
        if p.name != "quest_field_instructions.py" and marker.search(p.read_text(encoding="utf-8")))
    assert offenders == []


def _factory():
    return lambda: object()


def test_overview_is_in_compact_block_markdown_and_every_tool_description():
    short, full = what_a_quest_is("short"), what_a_quest_is("full")
    assert short and full and "not a to-do list" in short
    assert short in render_compact_block()
    doc = render_markdown()
    assert full in doc
    for entry in [*DOC["fields"].values(), *DOC["other_fields"].values()]:
        if not entry.get("alias_of"):
            for item in entry.get("does_not_belong", []):
                assert item in doc
    descriptions = [quest_field_tool_spec(f, {}, client_factory=_factory()).description
                    for f in QUEST_FIELD_TOOLS]
    descriptions += [s({}, client_factory=_factory()).description for s in
                     (add_measurable_outcome_spec, add_quest_strategy_spec, create_quest_goal_spec,
                      create_quest_habit_spec)]
    for description in descriptions:
        assert description.count(short) == 1


def test_no_em_dash_anywhere_in_rendered_output():
    texts = [render_compact_block(), render_markdown(), what_a_quest_is("full"),
             *(field_instructions_text(k) for k in [*QUEST_FIELD_INSTRUCTIONS, *ITEM_INSTRUCTIONS])]
    assert not any("—" in t for t in texts)


def test_strategy_tool_offers_problem_links_and_an_approach():
    props = add_quest_strategy_spec({}).parameters["properties"]
    assert props["problem_ids"]["type"] == "array"
    assert "approach" in props
