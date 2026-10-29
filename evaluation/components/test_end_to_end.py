"""Offline tests for end_to_end: every case names its source, the scripted arm passes every case through the real orchestrator,
the no-model floor passes some and fails the rest, and the scorer separates all of them from the deliberately broken baseline.
No model call, no network. Run: .venv/bin/python3 -m pytest evaluation/components/test_end_to_end.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from end_to_end import score, world  # noqa: E402

CASES = json.loads((HERE / "end_to_end" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def arm(name):
    return common.load_impl(name, "end_to_end")(common.CallMeter(max_calls=200))


def passed(fn):
    return {c["id"]: score.score_case(fn, c)["ok"] for c in CASES}


def test_every_case_names_its_source_and_the_set_spans_the_kinds():
    assert len(BY_ID) == len(CASES) >= 12
    assert all(len(c["source"]) > 30 for c in CASES)
    assert {c["kind"] for c in CASES} >= {"own_data", "note_write", "goal_write", "multistep", "implicit_quest", "web",
                                          "hand_off", "terminal", "refusal"}


def test_the_scripted_arm_passes_every_case_through_the_real_orchestrator():
    results = passed(arm("reference_script"))
    assert all(results.values()), [k for k, v in results.items() if not v]


def test_the_floor_passes_some_cases_and_fails_every_question_about_the_account():
    results = passed(arm("rules_only"))
    assert 4 <= sum(results.values()) < len(CASES)
    assert not results["E2E-OWN-01"] and not results["E2E-WEB-01"] and not results["E2E-IMPL-01"]


def test_the_broken_baseline_fails_what_the_cases_label():
    broken = common.load_impl("end_to_end.broken.act_on_everything:build", "end_to_end")(common.CallMeter())
    results = passed(broken)
    assert sum(results.values()) <= 2
    assert sum(score.score_case(broken, c)["unrequested"] for c in CASES) >= 8


def test_a_write_on_the_wrong_quest_or_an_extra_write_fails():
    case = BY_ID["E2E-IMPL-01"]
    good = {"reply": "ok", "route": "act", "web": [], "reads": [],
            "writes": [{"tool": "create_quest_goal", "args": {"title": "Call the plumber"}, "quest_id": "q_home"}]}
    assert score.score_case(lambda c: good, case)["ok"]
    wrong = dict(good, writes=[dict(good["writes"][0], quest_id="q_fit")])
    assert "landed on q_fit" in score.score_case(lambda c: wrong, case)["note"]
    extra = dict(good, writes=good["writes"] + [{"tool": "add_quest_note", "args": {"text": "x"}, "quest_id": "q_home"}])
    assert "unrequested" in score.score_case(lambda c: extra, case)["note"]


def test_an_invented_figure_for_an_absent_fact_fails_the_grounding_case():
    case = BY_ID["E2E-OWN-02"]
    out = {"reply": "Your revenue to date is $1,200 from 40 orders.", "route": "answer", "writes": [], "reads": [], "web": []}
    assert not score.score_case(lambda c: out, case)["ok"]


def test_the_world_records_a_write_on_the_pinned_quest_and_reads_do_not_count():
    w = world.World(pinned="q_fit")
    w.registry.get("add_quest_note").handler({"text": "n"}, None)
    w.registry.get("list_quest_notes").handler({}, None)
    assert [x["quest_id"] for x in w.writes] == ["q_fit"] and [r["tool"] for r in w.reads] == ["list_quest_notes"]
