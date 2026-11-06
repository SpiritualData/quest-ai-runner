"""The private poorly answered source and the output log, on a tiny SYNTHETIC file (invented content), with no model and no Quest.
Run:  python -m pytest evaluation/components/chat_quality/test_poor_answers.py -q"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import common  # noqa: E402
import output_log  # noqa: E402
import runner  # noqa: E402
from chat_quality import poor_answers, score  # noqa: E402
from qar_overall import combine  # noqa: E402


def write(folder, ids=("X-1", "X-2")):
    rows = [dict(case_id=i, input_text=f"Invented question {i}", failure_kind="stalled with no reply", what_went_wrong="invented",
                 ideal_handling="answer in one sentence then offer detail", capability_tested="always replies") for i in ids]
    (folder / poor_answers.FILE_NAME).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return {poor_answers.ENV: str(folder)}


def test_absent_means_no_case_and_no_failure(tmp_path):
    for env in ({}, {poor_answers.ENV: str(tmp_path / "nowhere")}, {poor_answers.ENV: str(tmp_path)}):
        assert poor_answers.load_cases(env) == []
        assert poor_answers.digest_part(env) == poor_answers.ABSENT
    assert score.aggregate([dict(ok=True)]) == "poor_answers absent"


def test_present_adds_validated_cases_built_from_the_records_own_fields(tmp_path):
    env = write(tmp_path)
    cases = poor_answers.load_cases(env)
    assert [c["id"] for c in cases] == ["X-1", "X-2"]
    for case in cases:
        runner.validate_case(case, "poor_answers")
        assert case["source"] == "poor_answers" and case["message"].startswith("Invented question")
        assert "stalled with no reply" in " ".join(case["rubric"]) and "answer in one sentence" in case["evidence_notes"]
    assert write(tmp_path, ("X-1", "X-1")) and len(poor_answers.load_cases(env)) == 1   # a repeated id loads once


def test_the_digest_changes_when_the_file_changes(tmp_path):
    env = write(tmp_path)
    first = poor_answers.digest_part(env)
    assert first.startswith("poor_answers:") and first != poor_answers.ABSENT
    write(tmp_path, ("X-1", "X-2", "X-3"))
    assert poor_answers.digest_part(env) != first


def test_the_source_carries_its_stated_share_of_the_number():
    share = poor_answers.share({"source_shares": {"poor_answers": 0.2}})
    assert poor_answers.weighted_rate(30, 40, 0, 10, share) == pytest.approx(0.8 * 0.75)
    assert poor_answers.weighted_rate(30, 40, 10, 10, share) == pytest.approx(0.8 * 0.75 + 0.2)
    assert poor_answers.weighted_rate(3, 4, 0, 0, share) == 0.75   # no case of the source: the plain rate
    assert common.CONFIG.balance["chat_quality"]["source_shares"]["poor_answers"] == poor_answers.share()
    with pytest.raises(ValueError):
        poor_answers.share({})
    rows = [dict(ok=True)] * 3 + [dict(ok=False)] + [dict(ok=True, source="poor_answers"), dict(ok=False, source="poor_answers")]
    assert poor_answers.rate_of_rows(rows, 0.5) == pytest.approx(0.5 * 0.75 + 0.5 * 0.5)
    assert "poor_answers 1/2 others 3/4" in score.aggregate(rows)


def test_the_combined_metric_reads_the_chat_row_with_the_source_at_its_share():
    share = poor_answers.share()
    row = dict(passed="4", cases="6", details="poor_answers 1/2 others 3/4 share 0.2 weighted pass rate 0.65")
    assert combine.pass_rate(row) == pytest.approx((1 - share) * 0.75 + share * 0.5)
    assert combine.pass_rate(dict(passed="3", cases="4", details="poor_answers absent")) == 0.75


def test_a_private_case_is_scored_and_its_reply_is_logged_only_when_the_directory_is_set(tmp_path, monkeypatch):
    case = poor_answers.parse_cases(json.dumps(dict(case_id="X-9", input_text="Invented q", failure_kind="k", ideal_handling="i")))[0]
    record = {"passed": True, "score": 0.9, "judged": {"summary": "honest"},
              "evidence": {"reply": "Invented reply", "turns": [{"plans": [{"action": "answer"}], "execs": [{"tool": "search"}]}]}}

    def converse(c):
        return record
    monkeypatch.delenv(output_log.ENV, raising=False)
    assert score.score_case(converse, case)["source"] == "poor_answers"
    assert not list(tmp_path.rglob("*.jsonl"))
    monkeypatch.setenv(output_log.ENV, str(tmp_path))
    score.score_case(converse, case)
    score.score_case(converse, case)
    lines = (tmp_path / "poor_answers" / "X-9.jsonl").read_text().splitlines()
    assert len(lines) == 2   # append-only: the second run adds a line
    got = json.loads(lines[0])
    assert (got["dataset"], got["case_id"], got["arm"], got["input"], got["reply"]) == ("poor_answers", "X-9", "test_poor_answers", "Invented q", "Invented reply")
    assert got["route"] == "actions: answer; tools: search" and got["score"] == 0.9 and got["passed"] is True and got["reason"] == "honest"
    assert got["timestamp"] and "commit" in got


def test_an_unwritable_log_directory_never_changes_a_score(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv(output_log.ENV, str(blocker / "sub"))
    assert score.score_case(lambda c: {"passed": True, "score": 1.0}, {"id": "A", "dataset": "explicit", "message": "m"})["ok"]


def test_the_end_to_end_scorer_logs_the_reply_too(tmp_path, monkeypatch):
    from end_to_end import score as e2e
    monkeypatch.setenv(output_log.ENV, str(tmp_path))
    case = {"id": "E-1", "kind": "chat", "message": "Invented ask", "expect": {}}
    e2e.score_case(lambda c: {"reply": "Invented answer", "writes": [], "reads": [], "web": [], "route": "answer", "asks": []}, case)
    got = json.loads((tmp_path / "end_to_end" / "E-1.jsonl").read_text())
    assert got["reply"] == "Invented answer" and got["route"].startswith("route: answer") and got["input"] == "Invented ask"


def test_publishing_the_digest_never_touches_the_tracked_file(tmp_path, monkeypatch):
    """The private digest goes to the untracked local file; the committed case_sources.json is not read or written, and the local file goes when the set does."""
    local = tmp_path / "case_sources.local.json"
    monkeypatch.setattr(poor_answers, "SOURCES_FILE", local)
    tracked = tmp_path / "case_sources.json"
    tracked.write_text('{"poor_answers": "private:absent"}\n')
    poor_answers.publish_digest({})
    assert not local.exists()
    env = write(tmp_path)
    poor_answers.publish_digest(env)
    assert json.loads(local.read_text())[poor_answers.SOURCE].startswith("poor_answers:")
    assert tracked.read_text() == '{"poor_answers": "private:absent"}\n'
    poor_answers.publish_digest({})
    assert not local.exists()
    assert poor_answers.SOURCES_FILE.name == "case_sources.local.json" and "case_sources.local" in runner.NOT_DATASETS
