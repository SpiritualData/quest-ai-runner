"""Offline unit tests for the LLM judge's rubric-count handling and other-account-data section.

Lives beside the harness, not under ``tests/`` (``pyproject.toml`` scopes ``testpaths`` to
``tests/``, which the public library's offline suite runs by default): importing ``judge`` pulls
in ``devclient``, which refuses to load unless pointed at this machine's dev lane .env, the same
constraint every other module in this DEV ONLY harness already carries. Run explicitly:

    .venv/bin/python3 -m pytest evaluation/qualitative/test_judge.py -q

No network calls and no real ``claude -p``: every provider here is a fake stand-in for
``ModelProvider.answer()``, and the account-data fetch is driven through a monkeypatched
``devclient``.
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import devclient  # noqa: E402
import judge as J  # noqa: E402

EVIDENCE = {"turns": [], "reply": "a plain reply", "kind": "answer", "actions": [],
            "tools": [], "delegated": False, "errors": [], "web_read": False}
PRE = {"checks": [], "hard_failures": []}


def make_case(n_rubric_items):
    return {"id": "unit-case", "dataset": "explicit", "area": "test",
            "quest_key": "sample_quest", "message": "a sample user message",
            "rubric": [f"rubric item {i}" for i in range(1, n_rubric_items + 1)]}


def verdict_json(n_items, model_pass=True):
    return json.dumps({
        "rubric": [{"id": i, "item": f"rubric item {i}", "pass": model_pass,
                    "evidence": "some quoted text"} for i in range(1, n_items + 1)],
        "score": 1.0 if model_pass else 0.0, "routing_ok": True, "side_effects_ok": True,
        "context_used": [], "code_review": None, "summary": "ok", "failure_class": None})


class ShortRubricProvider:
    """Always returns fewer rubric items than the case defines."""

    def __init__(self, returned_items):
        self.calls = 0
        self.returned_items = returned_items

    def answer(self, messages, *, model, system=None):
        self.calls += 1
        return verdict_json(self.returned_items)


class FlakyThenCorrectProvider:
    """Short rubric on the first call, a full-sized one on the second."""

    def __init__(self, full_items, short_items):
        self.calls = 0
        self.full_items = full_items
        self.short_items = short_items

    def answer(self, messages, *, model, system=None):
        self.calls += 1
        n = self.short_items if self.calls == 1 else self.full_items
        return verdict_json(n)


# ---------------------------------------------------------------------------------------------
# judge(): short rubric retries, then reports unjudged (or succeeds on the retry)
# ---------------------------------------------------------------------------------------------

def test_judge_retries_once_then_reports_unjudged_on_persistent_short_rubric():
    case = make_case(3)
    provider = ShortRubricProvider(returned_items=1)
    result = J.judge(case, EVIDENCE, [], PRE, "ground truth text", {}, world=None,
                     provider=provider)
    assert provider.calls == 2
    assert "verdict" not in result
    assert "error" in result


def test_judge_succeeds_on_retry_after_one_short_rubric():
    case = make_case(3)
    provider = FlakyThenCorrectProvider(full_items=3, short_items=1)
    result = J.judge(case, EVIDENCE, [], PRE, "ground truth text", {}, world=None,
                     provider=provider)
    assert provider.calls == 2
    assert "verdict" in result
    assert len(result["verdict"]["rubric"]) == 3


# ---------------------------------------------------------------------------------------------
# normalise_verdict(): the raise itself, and the cases that must NOT raise
# ---------------------------------------------------------------------------------------------

def test_normalise_verdict_raises_on_short_rubric():
    case = make_case(3)
    raw = json.loads(verdict_json(1))
    with pytest.raises(J.RubricCountMismatch):
        J.normalise_verdict(raw, case)


def test_normalise_verdict_ok_when_count_matches():
    case = make_case(2)
    raw = json.loads(verdict_json(2))
    v = J.normalise_verdict(raw, case)
    assert len(v["rubric"]) == 2
    assert all(r["pass"] for r in v["rubric"])


def test_normalise_verdict_truncates_when_model_returns_more_items():
    case = make_case(2)
    raw = json.loads(verdict_json(5))
    v = J.normalise_verdict(raw, case)
    assert len(v["rubric"]) == 2


def test_normalise_verdict_ok_when_case_has_no_rubric():
    case = {"id": "unit-case"}
    raw = json.loads(verdict_json(2))
    v = J.normalise_verdict(raw, case)
    assert len(v["rubric"]) == 2


# ---------------------------------------------------------------------------------------------
# other_account_data_section(): fetched via devclient, filtered against the world's own ids
# ---------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_other_account_data_cache(monkeypatch):
    """The section is cached once per process; each test needs its own fetch, and none of them
    may reach the real dev account over the network. Default to an empty account (no other
    quests/collections); a test of the fetch itself overrides these with its own monkeypatch."""
    J._OTHER_ACCOUNT_DATA["built"] = False
    J._OTHER_ACCOUNT_DATA["section"] = ""
    monkeypatch.setattr(devclient, "list_quests", lambda: [])
    monkeypatch.setattr(devclient, "list_collections", lambda: [])
    yield
    J._OTHER_ACCOUNT_DATA["built"] = False
    J._OTHER_ACCOUNT_DATA["section"] = ""


def test_other_account_data_excludes_world_ids_and_includes_the_rest(monkeypatch):
    world = {"quests": {"world_quest_key": "quest_in_world"},
             "collections": {"world_collection_key": "collection_in_world"}}

    monkeypatch.setattr(devclient, "list_quests", lambda: [
        {"quest_id": "quest_in_world"}, {"quest_id": "quest_elsewhere"}])
    monkeypatch.setattr(devclient, "quest_state", lambda qid: {"outcome": f"outcome of {qid}"})
    monkeypatch.setattr(devclient, "list_collections", lambda: [
        {"id": "collection_in_world", "name": "world-tagged collection"},
        {"id": "collection_elsewhere", "name": "unrelated collection"}])

    section = J.other_account_data_section(world)
    assert "outcome of quest_elsewhere" in section
    assert "outcome of quest_in_world" not in section
    assert "unrelated collection" in section
    assert "world-tagged collection" not in section


def test_other_account_data_fetched_once_and_cached(monkeypatch):
    calls = {"n": 0}

    def counting_list_quests():
        calls["n"] += 1
        return []

    monkeypatch.setattr(devclient, "list_quests", counting_list_quests)
    monkeypatch.setattr(devclient, "list_collections", lambda: [])

    J.other_account_data_section({})
    J.other_account_data_section({})
    assert calls["n"] == 1


def test_other_account_data_failure_is_handled_gracefully(monkeypatch):
    def boom():
        raise RuntimeError("network is down")

    monkeypatch.setattr(devclient, "list_quests", boom)
    section = J.other_account_data_section({})
    assert "network is down" in section


def test_judge_appends_other_account_data_to_the_ground_truth_sent_to_the_model(monkeypatch):
    monkeypatch.setattr(devclient, "list_quests", lambda: [{"quest_id": "quest_elsewhere"}])
    monkeypatch.setattr(devclient, "quest_state",
                        lambda qid: {"outcome": "a distinctive other-account outcome"})
    monkeypatch.setattr(devclient, "list_collections", lambda: [])

    case = make_case(1)
    provider = ShortRubricProvider(returned_items=1)  # matches case's single item, no retry
    captured = {}

    def answer(messages, *, model, system=None):
        captured["prompt"] = messages[0]["content"]
        return verdict_json(1)

    provider.answer = answer
    J.judge(case, EVIDENCE, [], PRE, "seeded ground truth", {}, world={}, provider=provider)
    assert "a distinctive other-account outcome" in captured["prompt"]
    assert "seeded ground truth" in captured["prompt"]


def test_score_is_computed_from_the_verdict_fields_not_the_judge_number():
    """Every item passed, pivot used and answer-changing, no failure class: 1.0, whatever number
    the judge wrote (it once wrote 0.6 for exactly this)."""
    import judge as J
    case = {"dataset": "implicit", "rubric": ["a", "b", "c"], "must_use_pivots": ["P"]}
    raw = {"rubric": [{"id": i, "pass": True, "evidence": "q"} for i in (1, 2, 3)],
           "score": 0.6, "routing_ok": True, "side_effects_ok": True,
           "context_used": [{"pivot": "P", "used": True, "changed_answer": True}]}
    v = J.normalise_verdict(raw, case)
    assert v["score"] == 1.0 and v["judge_score"] == 0.6


def test_formula_keeps_every_cap():
    import judge as J
    case = {"dataset": "implicit", "rubric": ["a", "b"], "must_use_pivots": ["P"]}
    base = {"rubric": [{"id": 1, "pass": True, "evidence": "q"}, {"id": 2, "pass": True, "evidence": "q"}],
            "score": 1.0, "routing_ok": True, "side_effects_ok": True}
    unused = J.normalise_verdict({**base, "context_used": [{"pivot": "P", "used": False}]}, case)
    assert unused["score"] == 0.6
    unchanged = J.normalise_verdict(
        {**base, "context_used": [{"pivot": "P", "used": True, "changed_answer": False}]}, case)
    assert unchanged["score"] == 0.4
    claimed = J.normalise_verdict(
        {**base, "score": 0.3, "failure_class": "claimed_unperformed_write",
         "context_used": [{"pivot": "P", "used": True, "changed_answer": True}]}, case)
    assert claimed["score"] == 0.3
    half = J.normalise_verdict(
        {**base, "rubric": [{"id": 1, "pass": True, "evidence": "q"}, {"id": 2, "pass": False}],
         "context_used": [{"pivot": "P", "used": True, "changed_answer": True}]}, case)
    assert half["score"] == 0.5
