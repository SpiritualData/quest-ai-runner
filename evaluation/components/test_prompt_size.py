"""Offline tests for the prompt_size component (no model call). Run:
.venv/bin/python3 -m pytest evaluation/components/test_prompt_size.py"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401
from prompt_size import fixtures  # noqa: E402
from prompt_size.score import duplicate_blocks, score_case  # noqa: E402

CASES = json.loads((Path(__file__).parent / "prompt_size" / "cases.json").read_text())


def test_duplicate_counter_sees_repeated_context_update_blocks_and_paragraphs():
    block = fixtures.update_bundle(2, 300).as_prompt_block()
    assert duplicate_blocks(block) == (0, 0)
    assert duplicate_blocks("\n\n".join([block] * 16))[0] == 15          # the 2026-10-06 shape
    para = fixtures.prose(1, 400)
    assert duplicate_blocks(para + "\n\n" + para + "\n\nshort\n\nshort") == (0, 1)


def test_scorer_fails_a_prompt_with_duplicated_blocks_even_under_the_ceiling():
    case = dict(id="t", point="context_update_bundle", fixture="healthy", ceiling_chars=10 ** 6)
    block = fixtures.update_bundle(2, 300).as_prompt_block()
    r = score_case(lambda point, inputs: block + "\n\n" + block, case)
    assert not r["ok"] and r["dups"] >= 1 and "duplicated" in r["note"]
    assert score_case(lambda point, inputs: block, case)["ok"]


def test_scorer_fails_a_prompt_over_its_ceiling():
    case = dict(id="t", point="planner", fixture="healthy", ceiling_chars=100)
    assert not score_case(lambda point, inputs: "x" * 101, case)["ok"]
    assert score_case(lambda point, inputs: "x" * 100, case)["ok"]


def test_incident_fixtures_really_carry_the_duplicated_blocks_the_live_path_must_remove():
    from quest_ai_runner.core.prompt_budget import count_blocks
    deep = fixtures.build_inputs(dict(point="deep_prompt", fixture="incident"))
    assert count_blocks(deep["preamble"] + deep["brief"]) >= 16
    auto = fixtures.build_inputs(dict(point="autopilot_batch", fixture="incident"))
    assert len(auto["adopted_tasks"]) >= 6 and all(count_blocks(t["text"]) == 1 for t in auto["adopted_tasks"])


@pytest.mark.parametrize("case", [c for c in CASES if c["point"] != "planner"], ids=lambda c: c["id"])
def test_live_path_meets_ceiling_with_no_duplicates(case):
    from prompt_size.impls import live
    r = score_case(live.build(common.CallMeter()), case)
    assert r["ok"], r["note"]


def test_live_planner_healthy_fixture_passes():
    from prompt_size.impls import live
    meter = common.CallMeter()
    case = next(c for c in CASES if c["id"] == "PSZ-001")
    assert score_case(live.build(meter), case)["ok"] and meter.calls == 0
