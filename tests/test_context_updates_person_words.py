"""A person's own words reach the run whole; only what this module composed gets shortened.

The failure this pins down actually happened. Someone answered an autopilot email with about
1,600 characters: three numbered corrections, then one standing instruction ("give me an update on
X at every run"). The block's flat 800-character per-item cap cut it mid-sentence inside item 2,
and the run replied to its author, by email: "On the grant, your message cut off there, tell me
what part of it still needs a response." Three things went wrong at once, and each has a test here:

  * The words were cut at all, though they were a person's instruction and well inside the cap a
    mailed note already passes through on the way in.
  * The run could not tell OUR cut from the end of what they wrote, so it handed the failure back
    to the person as though their mail client had dropped it.
  * The instruction past the cut was never seen, so it did not happen, and the person had to ask
    again.

Offline, hand-built bundles.
"""
from datetime import datetime, timezone

from quest_ai_runner.runner.context_updates import (
    MAX_BODY_CHARS,
    MAX_PERSON_BODY_CHARS,
    PERSON_BODY_BUDGET,
    ContextUpdate,
    ContextUpdates,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

# The shape of the real reply: long enough to pass the old cap, with the ask at the very end.
THEIR_REPLY = (
    "Thanks. Feedback: 1. Don't mistake my quest notes for things my advisor said. "
    + "The architecture is something I'd like your help with. " * 30
    + "Please give me an update on case gathering at every run."
)


def _note(body, *, ref="U1", when=NOW, verbatim=True):
    return ContextUpdate(source="quest_notes", kind="note", item_id=ref.lower(), ref=ref,
                         title="The person wrote on the quest", body=body, excerpt=body,
                         author="The person", occurred_at=when, location="their quest",
                         how_to_respond="answer it in your result", needs_response=True,
                         verbatim=verbatim)


def test_their_reply_reaches_the_run_whole_instruction_at_the_end_included():
    assert len(THEIR_REPLY) > MAX_BODY_CHARS  # the case only exists past the old cap
    text = ContextUpdates(updates=[_note(THEIR_REPLY)]).as_prompt_block()
    assert THEIR_REPLY in text              # every word of it, not just the closing ask
    # Nothing anywhere in the block reads as their sentence having ended: the index line above
    # the item previews their first words with an ellipsis, not a truncation marker.
    assert "truncated" not in text and "not shown" not in text


def test_a_body_this_module_composed_is_still_shortened_at_the_short_cap():
    """The cap is not wrong, its reach was. A rendered habit log loses nothing by being cut."""
    composed = _note("2026-09-13, yes, 4h 57m. " * 100, ref="U2", verbatim=False)
    composed.source, composed.kind = "collection_entries", "habit"
    text = ContextUpdates(updates=[composed]).as_prompt_block()
    assert "[...truncated]" in text
    assert composed.body.strip() not in text                 # actually cut
    assert "more characters of their own words" not in text  # and not claimed to be a person's


def test_a_person_past_even_the_long_cap_is_told_who_shortened_it():
    """The marker is the whole point: a run that cannot tell our cut from their sentence ending
    will blame them for it, which is exactly what happened."""
    text = ContextUpdates(updates=[_note("word " * (MAX_PERSON_BODY_CHARS // 2))]).as_prompt_block()
    assert "THIS LIST shortened them" in text
    assert "never tell them their message cut off" in text
    assert "more characters of their own words are not shown" in text


def test_the_newest_note_keeps_its_words_when_a_days_worth_arrives_at_once():
    """Newest-first spending: the note still waiting on an answer is the one shown in full, and
    the tail is shortened rather than dropped."""
    long_note = "word " * (MAX_PERSON_BODY_CHARS // 5)   # ~2,400 chars each
    older = [_note(long_note + f" older {i}", ref=f"U{i}",
                   when=datetime(2026, 9, i + 1, tzinfo=timezone.utc)) for i in range(1, 12)]
    newest = _note(THEIR_REPLY, ref="U99", when=NOW)
    block = ContextUpdates(updates=older + [newest])

    limits = block.body_limits(block.updates)
    assert limits[id(newest)] >= len(THEIR_REPLY)
    assert min(limits.values()) >= MAX_BODY_CHARS       # nothing is blanked

    text = block.as_prompt_block()
    # Bounded: twelve notes of twelve thousand characters cannot spend a hundred and forty
    # thousand of them on one brief, whoever wrote them.
    assert len(text) <= PERSON_BODY_BUDGET + len(block.updates) * (MAX_BODY_CHARS + 800)
    assert "Please give me an update on case gathering at every run." in text
    # The tail is shortened, never dropped: every note offered is still an item in the block.
    assert all(f"[U{i}]" in text for i in range(1, 12))
    assert text.count("The person wrote on the quest") == len(block.updates)


def test_an_undated_note_never_costs_a_dated_one_its_words():
    undated = _note("word " * MAX_PERSON_BODY_CHARS, ref="U1", when=None)
    dated = _note(THEIR_REPLY, ref="U2", when=NOW)
    limits = ContextUpdates(updates=[undated, dated]).body_limits([undated, dated])
    assert limits[id(dated)] >= len(THEIR_REPLY)
