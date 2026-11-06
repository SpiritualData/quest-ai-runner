"""The past-activity gate: a message naming someone is searched for what they asked the AI before the question back. Offline."""
from datetime import date

from quest_ai_runner.the_world.tools import tool_guards

FRIDAY = date(2026, 11, 6)
OP = "find_ai_activity"


def test_names_are_the_capitalised_words_after_the_first():
    assert tool_guards.names_in("Mary had a chat on Thursday that went badly") == []
    assert tool_guards.names_in("I think Mary's chat with Quest went badly, Mary asked about Bob") == ["Mary", "Bob"]
    assert tool_guards.names_in("what did Mary ask?") == ["Mary"]


def test_a_question_back_about_someone_is_searched_first():
    reads = tool_guards.past_activity_reads_owed("what did Mary ask the AI?", "clarify", OP, FRIDAY)
    assert reads == [{"operation": OP, "args": {"person": "Mary", "limit": 10, "days": 30}}]


def test_a_named_time_becomes_the_window():
    reads = tool_guards.past_activity_reads_owed("that chat Mary had last week went badly", "answer", OP, FRIDAY)
    assert reads[0]["args"]["date"] == "2026-10-26..2026-11-01" and reads[0]["args"]["person"] == "Mary"


def test_nothing_is_owed_without_the_operation_a_name_or_a_reason():
    assert tool_guards.past_activity_reads_owed("what did Mary ask?", "clarify", None, FRIDAY) == []
    assert tool_guards.past_activity_reads_owed("what did you do yesterday?", "clarify", OP, FRIDAY) == []
    assert tool_guards.past_activity_reads_owed("what should Mary do next?", "answer", OP, FRIDAY) == []
    assert tool_guards.past_activity_reads_owed("what did Mary ask?", "read", OP, FRIDAY) == []
