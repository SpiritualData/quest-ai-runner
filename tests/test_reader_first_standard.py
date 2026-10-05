"""The reader-first standard is ONE text, said once on each surface a person reads."""
from quest_ai_runner.core.orchestrator import REPLY_VOICE_SYSTEM
from quest_ai_runner.core.reader_first import READER_FIRST_STANDARD
from quest_ai_runner.runner.executor import RESULT_IS_THE_WORK_CONTRACT


def test_chat_replies_and_task_results_carry_the_same_standard_once():
    assert REPLY_VOICE_SYSTEM.count(READER_FIRST_STANDARD) == 1
    assert RESULT_IS_THE_WORK_CONTRACT.count(READER_FIRST_STANDARD) == 1


def test_standard_asks_for_the_behaviours_that_cut_reader_cost():
    text = READER_FIRST_STANDARD
    assert "Answer first" in text
    assert "report only what changed" in text      # a recurring pass must not recap itself
    assert "table" in text and "diagram" in text   # format follows the content
    assert "say so and use the next best one" in text


def test_standard_follows_the_copy_conventions():
    assert "—" not in READER_FIRST_STANDARD
    assert "/home/" not in READER_FIRST_STANDARD
