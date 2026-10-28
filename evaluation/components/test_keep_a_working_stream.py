"""Offline tests for the keep_a_working_stream component: the case file is well formed and the receipts scorer
separates a request that honours its earlier context from one that does not. No model call, no network.
Run: .venv/bin/python3 -m pytest evaluation/components/test_keep_a_working_stream.py -q -p no:cacheprovider"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from keep_a_working_stream import score  # noqa: E402
from quest_ops.fake_quest import WORLD  # noqa: E402

CASES = {c["id"]: c for c in json.loads((HERE / "keep_a_working_stream" / "cases.json").read_text())}


def scripted(*calls, reply="done", steps=2):
    """A stand-in request handler: replays tool calls through the world's real handlers (receipts) and returns."""
    def fn(case, world):
        for name, args in calls:
            world.registry.get(name).handler(args, None)
        return {"reply": reply, "steps": steps}
    return fn


def test_cases_have_expectations_and_unique_ids():
    assert len(CASES) >= 6
    for case in CASES.values():
        assert case["message"] and case["expect"]


def test_good_request_passes_with_no_continuity_error():
    row = score.score_case(scripted(("create_quest_goal", {"title": "WK: Run the sleep trial", "period": "2026_W44"})),
                           CASES["CS-02"])
    assert row["ok"] and row["continuity_errors"] == 0


def test_declined_proposal_refired_is_a_continuity_error():
    row = score.score_case(scripted(("create_quest_goal", {"title": "Run the stretch trial", "period": "2026_W44"}),
                                    ("create_quest_habit", {"name": "Stretch before bed"})), CASES["CS-01"])
    assert not row["ok"] and row["continuity_errors"] == 1


def test_correction_not_applied_is_a_continuity_error():
    row = score.score_case(scripted(("create_quest_goal", {"title": "Run the sleep trial", "period": "2026_W44"})),
                           CASES["CS-02"])
    assert row["continuity_errors"] == 1


def test_missing_second_request_is_a_task_error_not_continuity():
    row = score.score_case(scripted(("create_quest_goal", {"title": "Swim twice", "period": "2026_W44"})), CASES["CS-03"])
    assert not row["ok"] and row["continuity_errors"] == 0


def test_duplicate_write_fails_and_reply_contradicting_receipt_is_flagged():
    twice = scripted(("create_quest_goal", {"title": "Stretch three times", "period": "2026_W44"}),
                     ("create_quest_goal", {"title": "Stretch three times", "period": "2026_W44"}),
                     reply="You have no goals yet.")
    row = score.score_case(twice, CASES["CS-04"])
    assert not row["ok"] and row["continuity_errors"] == 1


def test_single_step_request_is_marked_and_aggregated():
    row = score.score_case(scripted(("create_quest_goal", {"title": "Swim", "period": "x"}),
                                    ("create_quest_habit", {"name": "Evening walk"}), steps=1), CASES["CS-03"])
    assert row["ok"] and row["single_step"]
    assert "0 per 100 steps" in score.aggregate([row])


def test_read_tools_are_not_write_receipts():
    row = score.score_case(scripted(("create_quest_goal", {"title": "Stretch three times", "period": "x"}),
                                    ("list_quest_goals", {})), CASES["CS-04"])
    assert row["ok"] and row["got"].startswith("create_quest_goal")
