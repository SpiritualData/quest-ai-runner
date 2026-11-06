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
    assert "failing 1 of 2" in row["STATUS"] and "2026-11-02" in row["STATUS"] and row["Inputs"] == 2 and row["Owner"] == "owner"


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
    assert all("secret" not in cell for cell in irows[1]) and dict(zip(irows[0], irows[1]))["PRIORITY"] == ""
    second = dict(zip(irows[0], irows[2]))
    assert second["PRIORITY"] == "High" and second["Priority set by"] == qs.PRESET_NOTE and dict(zip(irows[0], irows[1]))["Status"] == "unknown"


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


def test_each_environment_has_its_own_row_and_the_status_names_both():
    log = {("chat_quality/acceptance", "A-1"): [dict(arm="prod_qar_readonly", environment=qs.PROD_ENV, reply="prod reply", passed=True, score=0.9, timestamp="2026-11-06T01:00:00", commit="p"),
                                                 dict(arm="dev_quest_chat", reply="dev reply", passed=False, timestamp="2026-11-05T01:00:00", commit="d")]}
    qs.ANSWER_ARMS["chat_quality"] = "dev_quest_chat"
    try:
        drows, irows = qs.build_rows([dataset(cases=("A-1", "A-2"))], sources(log=log), "", "run.sh")
    finally:
        qs.ANSWER_ARMS.clear()
    rows = [dict(zip(irows[0], r)) for r in irows[1:]]
    a1 = {r["Environment"]: r for r in rows if r["Input id"] == "A-1"}
    assert set(a1) == {qs.DEV_ENV, qs.PROD_ENV}
    assert (a1[qs.PROD_ENV]["BEST ALTERNATIVE"], a1[qs.PROD_ENV]["ITS LAST OUTPUT"], a1[qs.PROD_ENV]["Status"]) == ("prod_qar_readonly", "prod reply", "pass")
    assert (a1[qs.DEV_ENV]["BEST ALTERNATIVE"], a1[qs.DEV_ENV]["ITS LAST OUTPUT"], a1[qs.DEV_ENV]["Status"]) == ("dev_quest_chat", "dev reply", "fail")
    assert [r["Environment"] for r in rows if r["Input id"] == "A-2"] == [qs.DEV_ENV]       # no result anywhere: one unknown row
    status = dict(zip(drows[0], drows[1]))
    assert qs.PROD_ENV + ": passing 1 of 1 (1 of 2 inputs have a result)" in status["STATUS"] and qs.DEV_ENV + ": failing 0 of 1" in status["STATUS"]
    assert status["How to run"] == "run.sh acceptance prod   (or: run.sh acceptance dev)"


def test_carry_over_keeps_human_columns_per_environment_and_from_a_sheet_without_the_column():
    log = {("chat_quality/acceptance", "A-1"): [dict(arm="prod_qar_readonly", reply="r", passed=True, timestamp="2026-11-06T01:00:00"),
                                                 dict(arm="dev_quest_chat", reply="r", passed=True, timestamp="2026-11-05T01:00:00")]}
    qs.ANSWER_ARMS["chat_quality"] = "dev_quest_chat"
    try:
        _, irows = qs.build_rows([dataset(cases=("A-1",))], sources(log=log), "")
    finally:
        qs.ANSWER_ARMS.clear()
    old_with = [dict(Dataset="chat_quality/acceptance", **{"Input id": "A-1", "Environment": qs.PROD_ENV}, PRIORITY="Low", Notes="prod note")]
    assert qs.carry_over(old_with, [list(r) for r in irows], "Inputs") == 1
    rows = [list(r) for r in irows]
    qs.carry_over(old_with, rows, "Inputs")
    by_env = {dict(zip(rows[0], r))["Environment"]: dict(zip(rows[0], r)) for r in rows[1:]}
    assert by_env[qs.PROD_ENV]["Notes"] == "prod note" and by_env[qs.DEV_ENV]["Notes"] == ""
    old_without = [dict(Dataset="chat_quality/acceptance", **{"Input id": "A-1"}, PRIORITY="Medium", Notes="old sheet")]
    rows = [list(r) for r in irows]
    assert qs.carry_over(old_without, rows, "Inputs") == 2 and all(dict(zip(rows[0], r))["Notes"] == "old sheet" for r in rows[1:])


def test_the_owned_tabs_are_added_first_in_order_and_the_sheet_is_set_up_to_read():
    reqs = qs.tab_requests({"Datasets": 1, "Inputs": 2, "How to use": 3, "Other": 4})
    adds = [r["addSheet"]["properties"] for r in reqs if "addSheet" in r]
    assert [(a["title"], a["index"]) for a in adds] == [("Datasets", 0), ("Inputs", 1), ("How to use", 2)]
    assert [list(r)[0] for r in reqs[:3]] == ["deleteSheet"] * 3 and {r["deleteSheet"]["sheetId"] for r in reqs[:3]} == {1, 2, 3}
    setup = qs.requests_for({"Datasets": 5, "Inputs": 9})
    assert any(r.get("updateSheetProperties", {}).get("properties", {}).get("gridProperties") == {"frozenRowCount": 1} for r in setup)
    width = [r for r in setup if "updateDimensionProperties" in r and r["updateDimensionProperties"]["range"]["sheetId"] == qs.TAB_IDS["Inputs"]
             and r["updateDimensionProperties"]["range"]["startIndex"] == qs.INPUTS_HEAD.index("Input text")][0]
    assert width["updateDimensionProperties"]["properties"]["pixelSize"] >= 500
    assert any("repeatCell" in r and r["repeatCell"]["cell"]["userEnteredFormat"].get("wrapStrategy") == "WRAP" for r in setup)


def test_optional_review_fields_of_a_private_row_become_columns(tmp_path):
    row = dict(case_id="P-1", input_text="New wording", original_input_text="Old wording", failure_kind="stalled", what_went_wrong="it stalled",
               ideal_handling="ask one question", input_note="reworded")
    same = dict(case_id="P-2", input_text="Same", original_input_text="Same")
    (tmp_path / "poorly_answered_inputs.jsonl").write_text(json.dumps(row) + "\n" + json.dumps(same) + "\n")
    ds = qs.describe(qs.poor_answer_dataset(str(tmp_path)))
    _, irows = qs.build_rows([ds], sources(), "")
    one, two = (dict(zip(irows[0], r)) for r in irows[1:])
    assert (one["What went wrong"], one["Ideal handling"], one["Input note"], one["Original input text"]) == ("it stalled", "ask one question", "reworded", "Old wording")
    assert two["Original input text"] == "" and two["What went wrong"] == ""
