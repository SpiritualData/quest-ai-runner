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


def test_chat_replies_are_proactive_only_when_relevant_and_stay_in_scope():
    text = REPLY_VOICE_SYSTEM
    assert "be proactive where their context makes it worth their time" in text
    assert "any suggestion they already declined" in text   # past guidance stands
    assert "Never pad a reply with research or ideas they have no use for" in text
    assert "answer each one\n  separately or ask which they mean" in text
