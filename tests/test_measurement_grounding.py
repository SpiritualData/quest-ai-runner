"""A measurement is stated only as the records show it (chat_quality IMP-023, 2026-10-29).

The reply to "Am I actually getting any faster?" listed a 10K time trial twice ("Both timed 10K efforts in
your Run Log are identical") although the Run Log held one and the second was a journal line repeating the
same 54:10. The answer prompt's evidence gate and the support check now say that the same figure in two
records is one event by default, and that a figure belongs to the record it was found in.
"""
from quest_ai_runner.core import context_doctrine as cd
from quest_ai_runner.core.auxiliary_prompts import VERIFY_SUPPORT_RULES
from quest_ai_runner.core.orchestrator import _grounding_block


def test_the_evidence_gate_counts_a_repeated_figure_once_and_names_its_record():
    gate = cd.EVIDENCE_GATE
    assert "ONE event written down twice" in gate
    assert "never call a mention in one record an entry of another" in gate
    assert "say what you could not check" in gate
    assert "{" not in gate and "}" not in gate and "—" not in gate


def test_the_answer_block_carries_the_measurement_rule():
    block = _grounding_block("ctx", [], False)
    assert "ONE event written down twice" in block


def test_the_support_check_flags_a_measurement_counted_twice_or_misattributed():
    assert "counts one event as two" in " ".join(VERIFY_SUPPORT_RULES.split())
    assert "—" not in VERIFY_SUPPORT_RULES
