"""Offline tests: a phrase a spoken turn quotes from the assistant's previous reply, mis-heard, is tied to the word of that reply."""
from quest_ai_runner.the_intelligence.mind.context_intake import quoted_phrase

PREVIOUS = "The release is not out yet. I do not have a record of that access being added since, so treat the blocker as open."
MESSAGE = "What do you mean by that axis? What is missing for the flow?"


def test_a_misheard_word_is_tied_to_the_word_of_the_previous_reply_and_the_note_says_to_state_what_it_is():
    found = quoted_phrase.resolve(PREVIOUS, MESSAGE, lambda prompt: '{"quoted": "axis", "means": "access"}')
    assert found["means"] == "access" and found["sentence"].startswith("I do not have a record of that access")
    note = quoted_phrase.note(found)
    assert '"axis"' in note and '"access"' in note and "say plainly what it is" in note
    assert quoted_phrase.note({}) == ""


def test_an_answer_that_fails_the_text_comparison_or_abstains_resolves_nothing():
    for answer in ('{"quoted": "orbit", "means": "access"}',            # the phrase is not in the message
                   '{"quoted": "axis", "means": "capital"}',            # the word is not in the previous reply
                   '{"quoted": "access", "means": "access"}',           # nothing to resolve
                   "{}", "not json"):
        assert quoted_phrase.resolve(PREVIOUS, MESSAGE, lambda prompt, a=answer: a) == {}
    assert quoted_phrase.resolve(PREVIOUS, "What is the blocker?", lambda prompt: '{"quoted": "blocker", "means": "blocker"}') == {}
    assert quoted_phrase.resolve("", MESSAGE, lambda prompt: '{"quoted": "axis", "means": "access"}') == {}


def test_the_previous_reply_comes_from_the_conversation_store_and_no_store_adds_nothing():
    class Store:
        def current_slice(self, conv_id, message, recent_turns=1):
            return type("S", (), {"text": "USER: first\nASSISTANT: " + PREVIOUS})()
    assert "access" in quoted_phrase.note_for(Store(), "c1", MESSAGE, lambda prompt: '{"quoted": "axis", "means": "access"}')
    assert quoted_phrase.note_for(None, "c1", MESSAGE, lambda prompt: "{}") == ""
