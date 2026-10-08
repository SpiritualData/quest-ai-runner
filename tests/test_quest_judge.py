"""Offline tests for quest_judge: the pick AND the ranked candidate order, with a fake judge."""

from quest_ai_runner.core.quest_judge import select_and_rank_quests, select_quest

QUESTS = [
    {"quest_id": "q_a", "title": "Fitness", "state": "running 3x a week"},
    {"quest_id": "q_b", "title": "Grant", "state": "deadline in May"},
    {"quest_id": "q_c", "title": "Garden", "state": ""},
]


def judge_returning(verdict):
    seen = {}

    def call_judge(prompt, tool_schema):
        seen["prompt"] = prompt
        seen["tool"] = tool_schema["name"]
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    call_judge.seen = seen
    return call_judge


def test_pick_comes_first_then_rest_in_given_order():
    chosen, ranked = select_and_rank_quests(
        QUESTS, "how is my grant going?", judge_returning({"quest_id": "q_b"}))
    assert chosen == "q_b"
    assert ranked == ["q_b", "q_a", "q_c"]


def test_no_pick_puts_home_first_then_given_order():
    chosen, ranked = select_and_rank_quests(
        QUESTS, "random", judge_returning({"quest_id": ""}), home_quest_id="q_c")
    assert chosen is None
    assert ranked == ["q_c", "q_a", "q_b"]


def test_no_pick_without_home_keeps_given_order():
    chosen, ranked = select_and_rank_quests(QUESTS, "random", judge_returning({"quest_id": ""}))
    assert chosen is None
    assert ranked == ["q_a", "q_b", "q_c"]


def test_failure_falls_back_to_home_and_keeps_order():
    chosen, ranked = select_and_rank_quests(
        QUESTS, "x", judge_returning(RuntimeError("boom")), home_quest_id="q_b")
    assert chosen == "q_b"
    assert ranked == ["q_b", "q_a", "q_c"]


def test_failure_without_home_is_none_with_order():
    chosen, ranked = select_and_rank_quests(QUESTS, "x", judge_returning(RuntimeError("boom")))
    assert chosen is None
    assert ranked == ["q_a", "q_b", "q_c"]


def test_unknown_id_falls_back_to_home():
    chosen, ranked = select_and_rank_quests(
        QUESTS, "x", judge_returning({"quest_id": "nope"}), home_quest_id="q_a")
    assert chosen == "q_a"
    assert ranked == ["q_a", "q_b", "q_c"]


def test_json_string_verdict_is_parsed():
    chosen, ranked = select_and_rank_quests(
        QUESTS, "x", judge_returning('{"quest_id": "q_c", "reason": "yes"}'))
    assert chosen == "q_c"
    assert ranked[0] == "q_c"


def test_empty_quests_never_calls_the_judge():
    calls = []
    chosen, ranked = select_and_rank_quests([], "x", lambda p, t: calls.append(p))
    assert (chosen, ranked) == (None, [])
    assert calls == []


def test_prompt_marks_home_and_carries_state():
    judge = judge_returning({"quest_id": ""})
    select_and_rank_quests(QUESTS, "hello", judge, home_quest_id="q_a")
    assert "q_a [HOME]" in judge.seen["prompt"]
    assert "deadline in May" in judge.seen["prompt"]
    assert judge.seen["tool"] == "quest_selection"


def test_select_quest_returns_only_the_pick():
    assert select_quest(QUESTS, "x", judge_returning({"quest_id": "q_c"})) == "q_c"
    assert select_quest(QUESTS, "x", judge_returning({"quest_id": ""})) is None


def many_quests(n):
    return [{"quest_id": f"q{i}", "title": f"Quest {i}", "state": ""} for i in range(n)]


def test_ranked_covers_every_candidate_but_judge_sees_only_the_window():
    quests = many_quests(40)
    judge = judge_returning({"quest_id": ""})
    chosen, ranked = select_and_rank_quests(quests, "x", judge)
    assert chosen is None
    assert ranked == [f"q{i}" for i in range(40)]
    assert "q24" in judge.seen["prompt"] and "q25" not in judge.seen["prompt"]


def test_home_outside_the_window_is_still_shown_to_the_judge():
    quests = many_quests(40)
    judge = judge_returning({"quest_id": ""})
    chosen, ranked = select_and_rank_quests(quests, "x", judge, home_quest_id="q35")
    assert "q35 [HOME]" in judge.seen["prompt"]
    assert ranked[0] == "q35" and len(ranked) == 40


def test_pick_outside_the_window_is_rejected_as_unknown():
    quests = many_quests(40)
    chosen, ranked = select_and_rank_quests(
        quests, "x", judge_returning({"quest_id": "q30"}), home_quest_id="q1", judge_limit=10)
    assert chosen == "q1"
    assert len(ranked) == 40
