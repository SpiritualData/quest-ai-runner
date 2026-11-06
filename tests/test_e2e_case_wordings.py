"""The end_to_end cases test meaning, not one wording: other correct replies pass, a real defect still fails. Offline."""
import json
import sys
from pathlib import Path

COMPONENTS = Path(__file__).resolve().parent.parent / "evaluation" / "components"
sys.path.insert(0, str(COMPONENTS))
from end_to_end import score  # noqa: E402

CASES = {c["id"]: c for c in json.loads((COMPONENTS / "end_to_end" / "cases.json").read_text())}


def problems(case_id, reply):
    return score.reply_problems(reply, CASES[case_id]["expect"])


def test_docs_share_accepts_other_wordings_of_one_quest_scope():
    reply = ("Invite your physio by email to your fitness quest only. Your other quests stay private, and you can stop sharing "
             "whenever you like from the same screen.")
    assert problems("E2E-DOCS-01", reply) == []


def test_jarvis_briefing_may_say_the_word_revenue_but_not_invent_a_figure():
    ok = "Book the plumber first. Photograph the candle scents next, since revenue cannot start until the launch is ready, then ride easy."
    assert not any("revenue" in p for p in problems("E2E-JARVIS-01", ok))
    bad = "Book the plumber. The shop made 400 USD in revenue so far, so photograph the scents."
    assert any("revenue" in p for p in problems("E2E-JARVIS-01", bad))
