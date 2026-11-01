"""Offline tests for the work-trace value judge's prompt: no model call. Run: .venv/bin/python3 -m pytest evaluation/components/test_work_trace_judge.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402,F401
from work_trace_experience import score  # noqa: E402

VIEW = {"live": [], "settled": [{"group": "summary", "text": "Read papers by Santos et al., 2026 and 4 more"}],
        "expanded": [{"group": "papers", "text": "Sagittal plane joint mechanics (Wells et al., 2025)"}]}
CASE = {"message": "q", "reply": "r"}


def test_each_element_is_labelled_with_the_moment_the_person_sees_it():
    prompt = score.judge_prompt(CASE, score.unique(score.with_moments(VIEW)))
    summary_line = [l for l in prompt.splitlines() if l.startswith("1. [summary]")][0]
    row_line = [l for l in prompt.splitlines() if l.startswith("2. [papers]")][0]
    assert "only thing shown unless the person taps it" in summary_line
    assert "only after the person taps" in row_line


def test_the_rubric_does_not_call_a_summary_a_repeat_of_rows_it_opens_onto():
    prompt = score.judge_prompt(CASE, score.unique(score.with_moments(VIEW)))
    assert "same moment" in prompt
    assert "is not repetition, because the person has not opened them" in prompt
    assert "repeats another element," not in prompt   # the unconditional wording that failed both shapes


def test_an_element_in_two_moments_is_still_counted_once():
    both = {"live": [{"group": "g", "text": "t"}], "settled": [{"group": "g", "text": "t"}], "expanded": []}
    assert len(score.unique(score.with_moments(both))) == 1
