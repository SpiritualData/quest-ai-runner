"""The card updater never teaches an invented read shape for a note's full_ref.

Its example used to be {"query": {"kind": "...", "id": "..."}}, a shape no read adapter accepts.
Cards stored it, later planners copied it from those cards, and in one Quest turn eight reads in
that shape were refused (qualitative eval, 2026-10-07).
"""
from quest_ai_runner.core.orchestrator import CARD_UPDATE_PROMPT


def test_full_ref_is_copied_from_a_real_read_never_invented():
    assert '"kind": "...", "id"' not in CARD_UPDATE_PROMPT
    assert "never invent a shape" in CARD_UPDATE_PROMPT
