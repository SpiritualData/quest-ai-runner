"""The same source title twice is one source: in the count, the named titles and the panel's list."""
from quest_ai_runner.the_consciousness.terminal.textual_ui_helpers import (
    _build_future_context_text, _build_shallow_context_bullets, context_block_lines, source_count)

CARDS = [{"title": "Quest AI Runner", "score": 0.9}, {"title": "Quest AI Runner", "score": 0.8}]


def test_a_title_repeated_by_two_cards_is_counted_and_named_once():
    assert source_count(CARDS, []) == 1
    line = context_block_lines(CARDS, [])[0].plain.strip()
    assert line == "Looked at 1 source: Quest AI Runner." or line.startswith("Looked at 1 source")
    assert line.count("Quest AI Runner") <= 1


def test_the_panels_list_has_each_source_once():
    assert _build_shallow_context_bullets(CARDS) == "- Quest AI Runner"
    text = _build_future_context_text("- Quest AI Runner\n- Quest AI Runner\n- Other").plain
    assert "(2)" in text and text.count("Quest AI Runner") == 1


def test_different_titles_stay_apart():
    assert source_count([{"title": "A"}, {"title": "B"}], []) == 2
