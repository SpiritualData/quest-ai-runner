"""Offline tests for the quest_ops dev arm: the 50 migrated chat cases are all present and well formed, the
route classifier and verifiers behave on synthetic data, and the arm is skipped without a dev Quest.
Run: .venv/bin/python3 -m pytest evaluation/components/test_quest_ops_dev.py"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "quest_ops"))
sys.path.insert(0, str(HERE))

import dev_arm  # noqa: E402
import dev_run  # noqa: E402
import dev_verify  # noqa: E402
import dev_world  # noqa: E402

CHAT_IDS = ("R1 R2 R3 R4 FR1 FR2 R5 L1 L2 L3 LA1 LA2 LA3 LA4 LA5 LA6 SC1 C1 C2 C3 C4 D1 D2 D3 H1 H2 H3 H4 "
            "T1 T2 E1 E2 E3 G1 G2 G3 G4 G5 G6 G7 G8 F1 F2 F3 F4 X1 X2 X3 X4 X5").split()
RECIPE_IDS = "RP1 RN1 RP2 RP3 RN2 RP4 RP5 RP6".split()


def test_every_migrated_case_is_present_once_with_a_stratum():
    cases = dev_run.load_cases()
    ids = [c["id"] for c in cases]
    assert len(CHAT_IDS) == 50 and ids == CHAT_IDS + RECIPE_IDS
    assert len(set(ids)) == len(ids)
    for case in cases:
        assert case["stratum"] in {"read", "list", "write", "inform", "contrast", "recipe"}
        assert case["expect_route"] in {"inline", "deep"} and case["message"].strip()
        assert case["verify"]["kind"] in dev_verify.CHECKS
    assert {c["id"] for c in dev_run.load_cases() if c["expect_route"] == "deep"} == {"X1", "X2", "X3", "X4", "X5"}


def test_write_cases_carry_a_positive_control_and_known_placeholders():
    fx = {"quest": "q", "goals": {"A": "a", "B": "b", "C": "c"},
          **{k: k for k in dev_world.COLLECTION_KEYS}}
    for case in dev_run.load_cases():
        if case["stratum"] == "write" and case["id"] not in ("D2",):
            assert case["control"], case["id"]
            for step in case["control"]:
                text = json.dumps(dev_world.resolve_refs(step, fx))
                assert "{" not in text.replace('{"', "").replace("{}", ""), (case["id"], text)


def test_recipe_subsets_name_known_cases():
    ids = {c["id"] for c in dev_run.load_cases()}
    for spec in json.loads((HERE / "quest_ops" / "dev_recipe_subsets.json").read_text()).values():
        assert set(spec["teach"]) | set(spec["score"]) <= ids


def test_sample_is_stratified_and_seeded():
    cases = dev_run.load_cases()
    a, b = dev_run.draw_sample(cases, 12, 7), dev_run.draw_sample(cases, 12, 7)
    assert [c["id"] for c in a] == [c["id"] for c in b] and len(a) == 12
    assert {c["stratum"] for c in a} == {c["stratum"] for c in cases}


def test_route_classification():
    def turn(*events):
        return dev_arm.TurnResult(list(events))
    assert turn({"event": "delegated", "task_id": "t1"}).route == "delegated"
    short = turn({"event": "done", "content": "ok", "pending_undo": {"kind": "task", "task_id": "t2"}})
    assert short.task_ids == ["t2"] and short.text == "ok" and short.route == "answer"
    assert turn({"event": "exec", "data": {"phase": "code", "mutating": True}}).route == "inline_write"
    assert turn({"event": "exec", "data": {"phase": "code"}}).route == "inline_code"
    assert turn({"event": "plan", "data": {"action": "deep"}}, {"event": "done", "content": "x"}).planner_deep
    assert turn({"event": "plan", "data": {"action": "read", "step": 15}}).hit_step_cap


def test_verifiers_on_synthetic_state(monkeypatch):
    class Res:
        text, delegated, route = "Your outcome is a sub-50 10K", False, "answer"
    fx = {"quest": "q", "goals": {"A": "ga"}, "journal": "j"}
    assert dev_verify.verify({"kind": "reply_has", "needles": ["sub-50", "10k"]}, Res, fx)[0]
    assert not dev_verify.verify({"kind": "reply_has", "needles": ["nope"]}, Res, fx)[0]
    assert dev_verify.standalone_number("3 times", "3") and not dev_verify.standalone_number("13 runs", "3")

    class Client:
        def entries_of(self, _coll):
            return [{"fieldValues": {"distance_km": 10.4}}]

        def goals_of(self, _quest):
            return [{"id": "ga", "name": "old"}, {"id": "new", "name": "Track day"}]
    monkeypatch.setattr(dev_world, "client", lambda: Client())
    assert dev_verify.verify({"kind": "journal_distance", "accept": ["10.4"]}, Res, fx)[0]
    assert dev_verify.verify({"kind": "new_goal", "keyword": "track"}, Res, fx)[0]
    assert not dev_verify.verify({"kind": "new_goal", "keyword": "old"}, Res, fx)[0]  # original goals excluded


def test_field_write_honesty(monkeypatch):
    class Client:
        def quest_state(self, _quest):
            return {"current_state": "unchanged"}
    monkeypatch.setattr(dev_world, "client", lambda: Client())
    monkeypatch.setattr(dev_world, "open_field_decisions", lambda quest, marker: [{"id": "d"}])

    class Said:
        def __init__(self, text):
            self.text = text
    spec = {"kind": "field_write", "field": "current_state", "marker": "55"}
    assert dev_verify.verify(spec, Said("I have asked for your approval to change it."), {"quest": "q"})[0]
    assert not dev_verify.verify(spec, Said("Done! I've updated your current state."), {"quest": "q"})[0]


def test_dev_arm_is_skipped_without_a_dev_quest(monkeypatch, capsys):
    monkeypatch.delenv("QAR_EVAL_DEV_ENV_FILE", raising=False)
    assert dev_run.main([]) == 0 and "SKIPPED" in capsys.readouterr().out
    with pytest.raises(dev_world.DevUnavailable):
        dev_world.client()
