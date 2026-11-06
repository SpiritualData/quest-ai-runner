"""scripts/qar_datasets_sheet.py: rows, privacy, status, best alternative and carry-over, on invented data and without any Google call."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import qar_datasets_sheet as qs  # noqa: E402


def sources(results=(), per_case=(), log=None, raw=None):
    return dict(results_list=list(results), results=qs.latest_by(results, lambda r: (r["component"], r["impl"], r.get("cases_sha", ""), r.get("sample", ""))),
                per_case=qs.latest_by(per_case, lambda r: (r["component"], r["impl"], r["case_id"])), log=log or {}, raw=raw or {})


qs.ANSWER_ARMS.clear()


def dataset(did="chat_quality/acceptance", cases=("A-1", "A-2"), private=False, text="Invented question"):
    return qs.describe(dict(id=did, private=private, source="repo: x", cases=[dict(id=c, text=text, category="cat") for c in cases]))


RESULTS = [dict(component="chat_quality", impl="floor", cases="5", passed="5", model_calls="0", date="2026-11-01", git_sha="aaa", cases_sha="s", sample=""),
           dict(component="chat_quality", impl="live_arm", cases="10", passed="6", model_calls="9", date="2026-11-02", git_sha="bbb", cases_sha="s", sample=""),
           dict(component="chat_quality", impl="other_live", cases="10", passed="3", model_calls="9", date="2026-11-03", git_sha="ccc", cases_sha="s", sample="")]
PER_CASE = [dict(component="chat_quality", impl="live_arm", case_id="A-1", ok="1", score="0.9", cases_sha="s", sample=""),
            dict(component="chat_quality", impl="live_arm", case_id="A-2", ok="0", score="0.2", cases_sha="s", sample="")]


def test_the_leading_arm_is_the_best_model_arm_not_the_floor():
    assert qs.leading_arm(RESULTS, "chat_quality") == "live_arm" and qs.leading_arm(RESULTS, "nothing") == ""


def test_status_from_per_case_rows_and_no_output_yet_when_no_reply_was_saved():
    drows, irows = qs.build_rows([dataset()], sources(RESULTS, PER_CASE), "owner")
    first, second = (dict(zip(irows[0], r)) for r in irows[1:])
    assert (first["Status"], second["Status"]) == ("pass", "fail") and first["BEST ALTERNATIVE"] == "live_arm"
    assert first["ITS LAST OUTPUT"] == qs.NO_OUTPUT and first["Run date"] == "2026-11-02" and first["Commit"] == "bbb"
    row = dict(zip(drows[0], drows[1]))
    assert row["STATUS"].startswith("failing 1 of 2") and "2026-11-02" in row["STATUS"] and row["Inputs"] == 2 and row["Owner"] == "owner"


def test_the_output_log_gives_the_reply_of_the_leading_arms_latest_run():
    log = {("chat_quality/acceptance", "A-1"): [dict(arm="live_arm", reply="old", passed=False, timestamp="2026-11-02T01:00:00", commit="x"),
                                                 dict(arm="live_arm", reply="newest", passed=True, score=0.8, reason="honest", timestamp="2026-11-04T01:00:00", commit="y"),
                                                 dict(arm="other_live", reply="not this arm", passed=True, timestamp="2026-11-09T01:00:00")]}
    _, irows = qs.build_rows([dataset()], sources(RESULTS, PER_CASE, log=log), "")
    row = dict(zip(irows[0], irows[1]))
    assert (row["ITS LAST OUTPUT"], row["Output from"], row["Judge reason"], row["Run date"], row["Commit"]) == ("newest", "output log", "honest", "2026-11-04", "y")


def test_a_context_dataset_never_shows_its_input_text_and_preset_priorities_are_marked():
    ctx = qs.context_dataset("team", "/nonexistent")
    assert ctx is None
    ctx = dict(id="context/team", private=True, name="n", component="context_selection", what="w", metrics="m", source="private", cases=[dict(id="PRIV-1", text="secret words", category="c")])
    _, irows = qs.build_rows([ctx, dataset()], sources(), "")
    assert all("secret" not in cell for cell in irows[1]) and irows[1][4] == ""
    assert irows[2][4] == "High" and irows[2][5] == qs.PRESET_NOTE and dict(zip(irows[0], irows[1]))["Status"] == "unknown"


def test_priority_and_notes_survive_a_rewrite_by_header_name_and_a_changed_priority_is_human():
    drows, irows = qs.build_rows([dataset(cases=("A-1", "A-2", "A-3"))], sources(), "")
    old = [dict(zip(irows[0], irows[1]), PRIORITY="Low", Notes="keep me"), dict(zip(irows[0], irows[2]), PRIORITY="High", Notes="")]
    old[0]["Dataset"], old[1]["Dataset"] = "chat_quality/acceptance", "chat_quality/acceptance"
    assert qs.carry_over(old, irows, "Inputs") == 2
    one, two, three = (dict(zip(irows[0], r)) for r in irows[1:])
    assert (one["PRIORITY"], one["Notes"], one["Priority set by"]) == ("Low", "keep me", "human")
    assert two["PRIORITY"] == "High" and two["Priority set by"] == qs.PRESET_NOTE and three["PRIORITY"] == "High"
    dold = [dict(zip(drows[0], drows[1]), PRIORITY="Medium", Notes="n")]
    assert qs.carry_over(dold, drows, "Datasets") == 1 and dict(zip(drows[0], drows[1]))["PRIORITY"] == "Medium"


def test_private_loaders_read_invented_files_and_absent_means_none(tmp_path):
    (tmp_path / "poorly_answered_inputs.jsonl").write_text(json.dumps(dict(case_id="P-1", input_text="Invented", failure_kind="stalled")) + "\n")
    (tmp_path / "cases.jsonl").write_text(json.dumps(dict(case_id="c1", input_text="hidden")) + "\n")
    assert qs.poor_answer_dataset(str(tmp_path))["cases"][0]["text"] == "Invented" and qs.poor_answer_dataset(str(tmp_path / "no")) is None
    ctx = qs.context_dataset("personal", str(tmp_path))
    assert ctx["cases"] == [dict(id="PRIV-c1", text="", category="")] and ctx["id"] == "context/personal"


def test_the_repository_datasets_are_listed_with_their_counts():
    found = {d["id"]: len(d["cases"]) for d in qs.repo_datasets()}
    assert found["chat_quality/acceptance"] == 30 and "end_to_end/cases" in found and "chat_quality/case_sources" not in found
