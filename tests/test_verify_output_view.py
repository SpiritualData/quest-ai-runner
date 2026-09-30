"""The judge must not mistake its own view limit for a cut-off in the worker's output."""
from quest_ai_runner.core.orchestrator import (VERIFY_GOAL_PROMPT, VERIFY_OUTPUT_MAX_CHARS,
                                               verify_output_view)


def test_a_long_brief_under_the_cap_is_shown_whole():
    brief = "x" * 9000 + " the end."          # the 2026-09-30 brief: ~8k, cut at 6000 before
    assert verify_output_view(brief) == brief


def test_an_oversized_output_keeps_head_and_tail_and_says_the_gap_is_not_a_cutoff():
    text = "HEAD " + "a" * (VERIFY_OUTPUT_MAX_CHARS * 2) + " TAIL-ENDS-HERE."
    view = verify_output_view(text)
    assert view.startswith("HEAD ") and view.endswith("TAIL-ENDS-HERE.")
    assert "THIS VIEW ONLY" in view and "NOT a cut-off" in view
    assert len(view) < VERIFY_OUTPUT_MAX_CHARS + 500


def test_empty_output_is_empty():
    assert verify_output_view(None) == "" and verify_output_view("") == ""


def test_the_prompt_tells_the_judge_a_view_note_is_not_truncation():
    assert "THIS VIEW" in VERIFY_GOAL_PROMPT
