"""The bar a chat message must clear before an answer turn becomes a task.

Every case here is a real cockpit message that opened a task nobody asked for (Zee's lane, august
2026): status asks, statements of context, and instructions about the assistant's own behavior. The
commands alongside them must keep escalating, which is what makes this a threshold and not a mute.
"""
from quest_ai_runner.core.orchestrator import (
    _change_verb_used_as_verb,
    _message_requests_change,
    message_change_signal_ambiguous,
    message_holds_off_work,
)

# Messages that must NEVER open a task on their own.
NOT_DIRECTIVES = [
    # Told us, in so many words, not to.
    "dont create task just drop asnewr here in chat",
    "don't create a task for this",
    "no new tasks please",
    "kill and delete those tasks.. i havent given you an instruction yet",
    "hold off on this for now",
    "just answer here in the chat",
    # Asking where something stands. "update" and "move" are nouns/idioms here, not orders.
    "give me an update on the product subscription work",
    "give me a status on the campaign",
    "where are we with the Quest facebook leads",
    "any update on the offer letter",
    "how are we doing on the landing page",
    "catch me up on the email campaign",
    # A statement of context or plan, with no instruction in it.
    "i have shared the feedback when he replies i will let you know",
]

# Messages that must STILL become work.
DIRECTIVES = [
    "make sure the new draft is in the approval queue",
    "go ahead and create the draft and leave it in the approval queue",
    "fix the back button",
    "update my goal to be more ambitious",
    "please update the endpoint",
    "can you fix the date bug?",
    "the system incorrectly assigns dates to actions",
    "send me the report and update the sheet",
]


def test_non_directives_do_not_escalate():
    for msg in NOT_DIRECTIVES:
        assert _message_requests_change(msg) is False, f"should not escalate: {msg!r}"


def test_hold_off_messages_skip_the_llm_judgment_too():
    # A message telling us to stand down must not even buy a judgment call: it is not ambiguous.
    for msg in NOT_DIRECTIVES[:6]:
        assert message_holds_off_work(msg) is True, f"should read as hold-off: {msg!r}"
        assert message_change_signal_ambiguous(msg) is False, f"should not be ambiguous: {msg!r}"


def test_status_asks_are_settled_without_a_judgment_call():
    # No change verb survives as a verb, so there is nothing ambiguous left to spend a call on.
    for msg in ["give me an update on the product subscription work",
                "where are we with the Quest facebook leads",
                "any update on the offer letter"]:
        assert message_change_signal_ambiguous(msg) is False, f"should be settled: {msg!r}"


def test_directives_still_escalate():
    for msg in DIRECTIVES:
        assert _message_requests_change(msg) is True, f"should escalate: {msg!r}"


def test_a_verb_after_a_determiner_is_a_noun():
    assert _change_verb_used_as_verb("give me an update") is False
    assert _change_verb_used_as_verb("i want the latest update") is False
    assert _change_verb_used_as_verb("update the sheet") is True
    assert _change_verb_used_as_verb("can you update the sheet") is True


def test_a_question_behind_a_discourse_marker_is_still_a_question():
    # "so how do we move forward on this" reads as a question; the leading "so" used to hide that,
    # and "move" then forced it into a task.
    assert _message_requests_change("so how do we move forward on this") is False
    assert _message_requests_change("okay what would it take to fix the export") is False
    # It still carries a change verb, so it stays in the band the LLM judgment is for.
    assert message_change_signal_ambiguous("so how do we move forward on this") is True


def test_a_bug_report_is_not_muted_by_any_of_this():
    assert _message_requests_change("the export is broken on mobile") is True
    assert message_holds_off_work("the export is broken on mobile") is False
