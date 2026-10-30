"""A read an adapter clipped at its size limit is shown to the model AS clipped.

Cause found in a recorded chat miss: a question that needed an old entry of a stored list was answered
"nothing is recorded", because the list read was clipped by its adapter ("...[truncated]") and the
rows after the clip were simply not in front of the model. A cheap model reads a missing row as a
missing record. The fix is structural: the renderer sees the adapter's own clip marker (data, not
model output) and puts a note beside that read, in the planner's view and in the answer's view.

The compact planner profile also tells the model that grep reaches files only and where the
person's own records are read (the full profile already said so), at no more tokens than before.

Fully offline, no model calls.
"""
from quest_ai_runner.core.planner_context import (
    CUT_READ_NOTE,
    _render_gathered,
    _render_gathered_for_planner,
    _summarize_observation,
    read_was_cut,
)
from quest_ai_runner.core.planner_prompt import PLANNER_PROMPT_COMPACT
from quest_ai_runner.core.prompt_budget import estimate_tokens

CUT = {"kind": "query", "rel_path": "entries", "locator": "filter={}",
       "text": '[{"id":"a","notes":"easy run"},{"id":"b","notes":"easy run"}\n…[truncated]'}
WHOLE = {"kind": "query", "rel_path": "entries", "locator": "filter={}",
         "text": '[{"id":"a","notes":"easy run"}]'}


def test_a_clipped_read_is_recognised_in_every_spelling():
    assert read_was_cut(CUT)
    for tail in ("\n[truncated]", " [...truncated]", "…[truncated]\n"):
        assert read_was_cut({"kind": "read", "text": "body" + tail})
    assert not read_was_cut(WHOLE)
    assert not read_was_cut({"kind": "read", "text": "the word [truncated] in the middle of a sentence"})
    assert not read_was_cut({"kind": "error", "text": "x [truncated]"})
    assert not read_was_cut("not a dict")


def test_the_answer_view_carries_the_note_only_beside_the_clipped_read():
    shown = _render_gathered([WHOLE, CUT])
    assert shown.count(CUT_READ_NOTE) == 1
    assert shown.index(CUT_READ_NOTE) > shown.index("…[truncated]")
    assert CUT_READ_NOTE not in _render_gathered([WHOLE])


def test_the_planner_view_carries_it_and_so_does_the_compressed_older_read():
    fresh = [{"kind": "query", "rel_path": f"r{i}", "locator": "x", "text": f"row {i}"} for i in range(8)]
    assert CUT_READ_NOTE in _render_gathered_for_planner([CUT], 4, 6)
    older = _render_gathered_for_planner([CUT] + fresh, 4, 6)
    assert "[CUT" in older and "EARLIER READS" in older
    assert "[CUT" in _summarize_observation(CUT)
    assert "[CUT" not in _summarize_observation(WHOLE)


def test_the_compact_profile_says_where_the_persons_records_are_read():
    assert "grep reaches files only" in PLANNER_PROMPT_COMPACT
    assert "list_sources / list_operations" in PLANNER_PROMPT_COMPACT


def test_the_compact_profile_did_not_get_longer():
    assert estimate_tokens(PLANNER_PROMPT_COMPACT) <= 3858   # 2026-10-30, before this change
