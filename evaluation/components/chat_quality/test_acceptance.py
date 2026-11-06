"""The acceptance suite: exactly 30 diverse cases (datasets/acceptance.json), one per required category, each checkable.
Run:  PYTHONPATH=... .venv/bin/python3 -m pytest evaluation/components/chat_quality/test_acceptance.py -q"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import runner  # noqa: E402

CATEGORIES = [
    "inline answer from quest data", "must say it does not know", "note write", "goal create", "multi-step with order",
    "parallel steps", "implicit quest selection", "explicit quest pinned", "cross-quest isolation",
    "quest-tagged insight read by another member", "web question with cited source", "web 404 handled honestly",
    "deep run chosen for a real coding/file task", "deep run not chosen for a simple question",
    "queued hand-off task with a scheduled time", "scheduling/calendar ask", "request QAR cannot do",
    "bulk delete needing a confirm", "forbidden write", "ambiguous request needing one clarifying question",
    "correction of an earlier turn", "conflicting evidence", "attachment or photo", "long-conversation recall",
    "empty or vague message", "message in another language", "tone: upset user", "private data of another user",
    "tool fails, honest error report", "prioritization"]


def test_thirty_unique_cases_each_covering_one_required_category():
    cases = runner.load_cases("acceptance")
    ids = [c["id"] for c in cases]
    assert ids == [f"ACC-{n:03d}" for n in range(1, 31)] and len(set(ids)) == 30
    for category, case in zip(CATEGORIES, cases):
        assert case["area"].startswith(f"acceptance: {category}"), (case["id"], case["area"])
        assert case["evidence_notes"].startswith(("source: ",)) and "ideal:" in case["evidence_notes"]
        assert len(case["rubric"]) >= 2 and case.get("expected_routing")
        assert "forbid_writes" in case or case.get("expect_writes") or case.get("mutates") is not None or case["id"] in {"ACC-004", "ACC-005", "ACC-021"}


def test_the_suite_is_not_run_twice_by_all_and_is_in_the_component_cases():
    all_ids = {c["id"] for c in runner.load_cases("all")}
    assert not any(i.startswith("ACC-") for i in all_ids)
    from chat_quality import score
    assert {f"ACC-{n:03d}" for n in range(1, 31)} <= {c["id"] for c in score.load_cases()}


def test_no_em_dashes_in_the_suite():
    assert "—" not in (runner.DATASET_DIR / "acceptance.json").read_text()
