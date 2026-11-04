"""scripts/dataset_sheet.py: column layout, multi-file rows and review records, on invented data and without any Google call."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dataset_sheet as ds  # noqa: E402


def make_args(**over):
    args = ds.make_parser().parse_args(["export", "--cases", "x.jsonl", "--service-account", "k", "--subject", "s"])
    for key, value in over.items():
        setattr(args, key, value)
    return args


def case(cid, **extra):
    base = dict(case_id=cid, input_text=f"Input {cid}", domain="d", scope_need="in_quest", quests_involved=["Q1", "Q2"],
                request=dict(author="A", channel="app_chat", quest_title="Q1"),
                candidates=[dict(item_id=f"{cid}-1", type="note", text="alpha", label="needed", why="w", describe="a note",
                                 need_role="fact_source", quest_relation="same_quest"),
                            dict(item_id=f"{cid}-2", type="file", text="beta", label="irrelevant")])
    base.update(extra)
    return base


def test_field_text_reads_nested_lists_and_missing():
    c = case("c1")
    assert ds.field_text(c, "request.author") == "A"
    assert ds.field_text(c, "quests_involved") == "Q1; Q2"
    assert ds.field_text(c, "request.goal_title") == "" and ds.field_text(c, "nope.deeper") == ""


def test_default_columns_and_positions_follow_the_header_lists():
    args = make_args()
    ihead, chead = ds.item_head(args), ds.case_head(args)
    assert ihead[3] == "Item type" and ihead[4] == "describe" and ihead.index("Item text") == 4 + len(args.item_columns)
    assert ihead.index("Corrected need role") == ihead.index("Corrected label") + 1
    assert "quest_title" in args.item_columns and "scope_need" in args.case_fields and "quests_involved" in args.case_fields
    assert "Missing context (reviewer)" in chead and "Corrected scope need" in chead
    narrow = make_args(item_columns=["author"])
    assert ds.item_head(narrow).index("Reviewed") == ihead.index("Reviewed") - (len(args.item_columns) - 1)


def test_build_rows_align_with_headers_and_a_v1_case_still_exports():
    args = make_args()
    v1 = dict(case_id="old", input_text="t", candidates=[dict(item_id="i", type="note", text="x", label="needed")])
    item_rows, case_rows = ds.build_rows([case("c1"), v1], args)
    ihead = item_rows[0]
    assert all(len(r) == len(ihead) for r in item_rows) and all(len(r) == len(case_rows[0]) for r in case_rows)
    first = dict(zip(ihead, item_rows[1]))
    assert first["describe"] == "a note" and first["need_role"] == "fact_source" and first["Dataset label"] == "needed"
    old = dict(zip(ihead, item_rows[3]))
    assert old["describe"] == "" and old["Reviewed"] is False and old["Corrected need role"] == ""
    crow = dict(zip(case_rows[0], case_rows[1]))
    assert crow["request.author"] == "A" and crow["Items"] == 2 and crow["Items reviewed"].startswith("=COUNTIFS(")


def test_merge_rejects_a_case_id_in_two_files():
    with pytest.raises(SystemExit):
        ds.merge_cases([(Path("a.jsonl"), [case("c1")]), (Path("b.jsonl"), [case("c1")])], "case_id")
    assert len(ds.merge_cases([(Path("a.jsonl"), [case("c1")]), (Path("b.jsonl"), [case("c2")])], "case_id")) == 2


def test_file_labels_use_basenames_unless_they_clash(tmp_path):
    assert ds.file_labels([tmp_path / "a.jsonl", tmp_path / "b.jsonl"]) == ["a.jsonl", "b.jsonl"]
    (tmp_path / "one").mkdir(), (tmp_path / "two").mkdir()
    assert ds.file_labels([tmp_path / "one" / "cases.jsonl", tmp_path / "two" / "cases.jsonl"]) == ["one/cases.jsonl", "two/cases.jsonl"]


def test_file_labels_go_up_until_same_named_folders_differ(tmp_path):
    first, second = tmp_path / "x" / "data" / "cases.jsonl", tmp_path / "y" / "data" / "cases.jsonl"
    assert ds.file_labels([first, second]) == ["x/data/cases.jsonl", "y/data/cases.jsonl"]


def test_requests_use_computed_columns():
    args = make_args(item_columns=["author"])
    ihead, chead = ds.item_head(args), ds.case_head(args)
    reqs = ds.requests_for({"Items": 3, "Cases": 2}, 4, 2, ["needed", "helpful", "irrelevant"], ihead, chead, ["a", "b"],
                           ["in_quest", "none"])
    validations = [r["setDataValidation"]["range"]["startColumnIndex"] for r in reqs
                   if "setDataValidation" in r and r["setDataValidation"]["range"]["sheetId"] == 3]
    assert validations == [ihead.index("Reviewed"), ihead.index("Corrected label"), ihead.index("Corrected need role")]
    protected = [r["addProtectedRange"]["protectedRange"] for r in reqs if "addProtectedRange" in r]
    assert {p["range"]["sheetId"]: p["range"]["endColumnIndex"] for p in protected} == {3: ihead.index("Reviewed"),
                                                                                         2: chead.index("Reviewed")}
    assert all(p["warningOnly"] for p in protected)
    green = [r for r in reqs if "addConditionalFormatRule" in r and "CUSTOM_FORMULA" in json.dumps(r)][0]
    assert f"${ds.col_letter(ihead.index('Reviewed'))}2=TRUE" in json.dumps(green)
    widths = {r["updateDimensionProperties"]["range"]["startIndex"]: r["updateDimensionProperties"]["properties"]["pixelSize"]
              for r in reqs if "updateDimensionProperties" in r and r["updateDimensionProperties"]["range"]["sheetId"] == 3}
    assert widths[ihead.index("Item text")] == 520 and widths[ihead.index("Input")] == 380 and len(widths) == len(ihead)


def test_summary_formulas_follow_columns_and_files_table_round_trips():
    args = make_args()
    ihead, chead = ds.item_head(args), ds.case_head(args)
    rows, table = ds.summary_rows(args, ["needed", "helpful", "irrelevant"], ["a"], ["in_quest"], ihead, chead, 5, 2,
                                  [("a.jsonl", "aa"), ("b.jsonl", "bb")])
    letter = ds.col_letter(ihead.index("Reviewed"))
    assert f"Items!{letter}2:{letter}6" in rows[1][1]
    assert ds.sheet_file_shas(rows) == {"a.jsonl": "aa", "b.jsonl": "bb"} and rows[table][0] == ds.FILES_HEADER


def test_review_records_carry_need_role_scope_and_missing_context():
    args = make_args()
    c = case("c1")
    item_rows = {("c1", "c1-1"): {"Reviewed": True, "Corrected label": "helpful", "Corrected need role": "background"},
                 ("c1", "c1-2"): {"Reviewed": False}}
    case_rows = {"c1": {"Reviewed": True, "Exclude case": "keep", "Corrected scope need": "cross_quest",
                        "Missing context (reviewer)": " the budget "}}
    records, counts = ds.review_records([c], args, item_rows, case_rows, {"needed", "helpful", "irrelevant"}, {"fact_source", "background"},
                                        {"in_quest", "cross_quest"})
    rec = records[0]
    first, second = rec["items"]
    assert first["label"] == "helpful" and first["corrected"] and first["need_role"] == "background" and first["need_role_corrected"]
    assert second["need_role"] is None and not second["need_role_corrected"]
    assert rec["missing_context_reviewer"] == "the budget" and rec["scope_need"] == "cross_quest" and rec["scope_need_corrected"]
    assert counts["need_roles_corrected"] == 1 and counts["scope_needs_corrected"] == 1
    with pytest.raises(SystemExit):
        ds.review_records([c], args, {("c1", "c1-1"): {"Corrected need role": "bogus"}}, {}, {"needed"}, {"a"}, set())
