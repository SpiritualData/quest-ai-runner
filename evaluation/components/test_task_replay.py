"""Offline tests for task_replay: every case names its source, the work checks read the scratch folder as the source tests
asserted, the scorer accepts only what a done verifier judged, and the broken baseline fails every case. No worker, no model.
Run: .venv/bin/python3 -m pytest evaluation/components/test_task_replay.py -q -p no:cacheprovider"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402,F401
from task_replay import replay, score  # noqa: E402

CASES = json.loads((HERE / "task_replay" / "cases.json").read_text())
BY_ID = {c["id"]: c for c in CASES}


def folder(case):
    root = Path(tempfile.mkdtemp(prefix="qar_replay_test_"))
    replay.materialize(case, root)
    return root


def test_every_case_names_its_source_and_a_goal_text():
    assert len(BY_ID) == len(CASES) >= 4
    assert all(c["source"].startswith("tests/") and c["goal"] for c in CASES)


def test_the_workspace_is_built_from_the_fixture():
    root = folder(BY_ID["RT-02"])
    lines = (root / "app.py").read_text().splitlines()
    assert lines[0] == "# filler line 1" and "wait = 1" in "\n".join(lines) and len(lines) > 500
    assert (folder(BY_ID["RT-03"]) / "esoi" / "solutions.csv").is_file()


def test_the_work_checks_read_the_folder_as_the_source_tests_asserted():
    case = BY_ID["RT-01"]
    root = folder(case)
    before = replay.snapshot(root)
    assert replay.work_check(case["work_check"], before, before) is False
    (root / "docs" / "notes.md").write_text(before["docs/notes.md"].replace("paused pending review", "active"))
    assert replay.work_check(case["work_check"], before, replay.snapshot(root)) is True
    stay = BY_ID["RT-04"]
    kept = replay.snapshot(folder(stay))
    assert replay.work_check(stay["work_check"], kept, kept) is True
    assert replay.work_check(stay["work_check"], kept, {**kept, "docs/notes.md": "x"}) is False
    assert replay.work_check(None, kept, kept) is None


def fake(verdicts, work_ok=None, spawns=1):
    return lambda case: {"verdicts": verdicts, "spawns": [{}] * spawns, "work_ok": work_ok, "created": []}


def test_only_a_first_verdict_of_met_is_accepted():
    met, no = {"met": True, "reason": ""}, {"met": False, "reason": "not done"}
    assert score.score_case(fake([met]), BY_ID["RT-01"])["ok"]
    assert not score.score_case(fake([no, met], spawns=2), BY_ID["RT-01"])["ok"]
    assert not score.score_case(fake([]), BY_ID["RT-01"])["ok"]
    assert score.score_case(fake([met], work_ok=False), BY_ID["RT-01"])["wrong_accept"]


def test_a_run_with_no_worker_is_an_error_not_a_score():
    row = score.score_case(fake([], spawns=0), BY_ID["RT-01"])
    assert not row["ok"] and "nothing was replayed" in row["note"]


def test_the_broken_baseline_fails_every_case():
    broken = common.load_impl("task_replay.broken.trust_the_worker:build", "task_replay")(common.CallMeter(max_calls=1))
    assert not any(score.score_case(broken, c)["ok"] for c in CASES)


def test_the_arms_differ_only_in_what_surrounds_the_goal():
    assert replay.CURRENT["iteration"] == "slice-and-writeback" and replay.CURRENT["tool"]
    assert replay.FLOOR["iteration"] == "generic-doctrine" and not replay.FLOOR["tool"] and replay.FLOOR["max_iterations"] == 1
