"""Offline tests for quest_judge: one ranker for single and batch calls, with a fake judge."""

from quest_ai_runner.core.quest_judge import rank_quests, select_and_rank_quests, select_quest

QUESTS = [
    {"quest_id": "q_a", "title": "Fitness", "state": "running 3x a week"},
    {"quest_id": "q_b", "title": "Grant", "state": "deadline in May"},
    {"quest_id": "q_c", "title": "Garden", "state": ""},
]


def judge_returning(results):
    """A fake judge answering with the given verdict rows, recording the prompt it was sent."""
    seen = {}

    def call_judge(prompt, tool_schema):
        seen["prompt"] = prompt
        seen["tool"] = tool_schema["name"]
        if isinstance(results, Exception):
            raise results
        return {"results": results}

    call_judge.seen = seen
    return call_judge


def row(item, quest_id, ranking):
    return {"item": item, "quest_id": quest_id, "ranking": ranking}


def test_single_message_returns_pick_and_semantic_ranking():
    judge = judge_returning([row(1, "q_b", ["q_b", "q_c", "q_a"])])
    chosen, ranked = select_and_rank_quests(QUESTS, "how is my grant?", judge)
    assert chosen == "q_b"
    assert ranked == ["q_b", "q_c", "q_a"]
    assert judge.seen["tool"] == "quest_ranking"


def test_ranking_is_the_judges_order_not_the_given_order():
    judge = judge_returning([row(1, "", ["q_c", "q_a", "q_b"])])
    chosen, ranked = select_and_rank_quests(QUESTS, "gardening tips", judge)
    assert chosen is None
    assert ranked == ["q_c", "q_a", "q_b"]


def test_batch_returns_one_result_per_message_in_order():
    judge = judge_returning([
        row(2, "q_a", ["q_a", "q_c", "q_b"]),
        row(1, "q_c", ["q_c", "q_b", "q_a"]),
    ])
    out = rank_quests([{"text": "one"}, {"text": "two"}], QUESTS, judge)
    assert out[0] == ("q_c", ["q_c", "q_b", "q_a"])
    assert out[1] == ("q_a", ["q_a", "q_c", "q_b"])
    assert judge.seen["prompt"].count("MESSAGE:") == 2


def test_missing_message_row_uses_home_then_fallback_order():
    judge = judge_returning([row(1, "q_c", ["q_c", "q_a", "q_b"])])
    out = rank_quests([{"text": "one"}, {"text": "two"}], QUESTS, judge,
                      home_quest_id="q_b", fallback_order=["q_a", "q_c", "q_b"])
    assert out[0] == ("q_c", ["q_c", "q_a", "q_b"])
    assert out[1] == ("q_b", ["q_b", "q_a", "q_c"])


def test_failure_uses_home_pick_and_priority_fallback_order():
    out = rank_quests([{"text": "x"}], QUESTS, judge_returning(RuntimeError("boom")),
                      home_quest_id="q_b", fallback_order=["q_c", "q_a", "q_b"])
    assert out[0] == ("q_b", ["q_b", "q_c", "q_a"])


def test_failure_without_home_is_none_with_fallback_order():
    chosen, ranked = select_and_rank_quests(
        QUESTS, "x", judge_returning(RuntimeError("boom")), fallback_order=["q_c", "q_b", "q_a"])
    assert chosen is None
    assert ranked == ["q_c", "q_b", "q_a"]


def test_unknown_pick_falls_back_to_home():
    chosen, ranked = select_and_rank_quests(
        QUESTS, "x", judge_returning([row(1, "nope", ["q_a", "q_b", "q_c"])]), home_quest_id="q_a")
    assert chosen == "q_a"
    assert ranked[0] == "q_a" and len(ranked) == 3


def test_ranking_with_unknown_and_duplicate_ids_is_cleaned():
    judge = judge_returning([row(1, "q_a", ["zzz", "q_a", "q_a", "q_c"])])
    chosen, ranked = select_and_rank_quests(QUESTS, "x", judge)
    assert chosen == "q_a"
    assert ranked == ["q_a", "q_c", "q_b"]


def test_empty_quests_never_calls_the_judge():
    calls = []
    assert select_and_rank_quests([], "x", lambda p, t: calls.append(p)) == (None, [])
    assert calls == []


def test_prompt_marks_home_and_carries_state_and_previous():
    judge = judge_returning([row(1, "", ["q_a", "q_b", "q_c"])])
    select_and_rank_quests(QUESTS, "hello", judge, home_quest_id="q_a", previous_message="earlier")
    assert "q_a [HOME]" in judge.seen["prompt"]
    assert "deadline in May" in judge.seen["prompt"]
    assert "PREVIOUS: earlier" in judge.seen["prompt"]


def test_judge_window_limits_the_prompt_but_ranking_covers_all():
    quests = [{"quest_id": f"q{i}", "title": f"Quest {i}", "state": ""} for i in range(40)]
    judge = judge_returning([row(1, "", [f"q{i}" for i in range(25)])])
    chosen, ranked = select_and_rank_quests(quests, "x", judge)
    assert "q24" in judge.seen["prompt"] and "q25" not in judge.seen["prompt"]
    assert chosen is None
    assert len(ranked) == 40 and ranked[:25] == [f"q{i}" for i in range(25)]


def test_home_outside_the_window_is_still_shown():
    quests = [{"quest_id": f"q{i}", "title": f"Quest {i}", "state": ""} for i in range(40)]
    judge = judge_returning([row(1, "", ["q35"])])
    chosen, ranked = select_and_rank_quests(quests, "x", judge, home_quest_id="q35")
    assert "q35 [HOME]" in judge.seen["prompt"]
    assert ranked[0] == "q35" and len(ranked) == 40


def test_select_quest_returns_only_the_pick():
    assert select_quest(QUESTS, "x", judge_returning([row(1, "q_c", ["q_c"])])) == "q_c"
    assert select_quest(QUESTS, "x", judge_returning([row(1, "", ["q_a"])])) is None


def test_long_state_keeps_its_newest_end_not_its_start():
    from quest_ai_runner.core.quest_judge import STATE_LIMIT, _quest_lines
    old = "OLDSTART " + "filler " * 200
    state = old + "NEWEST: the tiler confirmed Thursday."
    line = _quest_lines([{"quest_id": "q1", "title": "Bathroom", "state": state}], None)
    assert "NEWEST: the tiler confirmed Thursday." in line
    assert "OLDSTART" not in line
    assert line.count("filler") <= STATE_LIMIT // len("filler ")


def test_short_state_is_untouched():
    from quest_ai_runner.core.quest_judge import _quest_lines
    line = _quest_lines([{"quest_id": "q1", "title": "Run", "state": "Ran 5k today"}], None)
    assert line.endswith("| state: Ran 5k today")
