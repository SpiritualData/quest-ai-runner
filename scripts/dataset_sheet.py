#!/usr/bin/env python3
"""Export labelled JSONL datasets to a private Google Sheet for human review, and import the corrections.

Generic: no data, names or credentials live here. A dataset is a JSONL file with one case per line and a list of labelled
items inside each case (for example candidate context items). Credentials and the user to act as are passed in by the caller.

  export:  dataset_sheet.py export --cases a.jsonl [b.jsonl ...] --service-account KEY.json --subject USER@DOMAIN [--title T]
  import:  dataset_sheet.py import --sheet SHEET_ID --cases a.jsonl [b.jsonl ...] --service-account KEY.json
           --subject USER@DOMAIN --reviewer NAME [--out-dir DIR]

The sheet is created as the `--subject` user (domain-wide delegation) so it is owned by that user and shared with nobody.
Several cases files go into one sheet; a case id that appears in two files is an error at export. The Summary tab lists each file
(the shortest trailing part of its path that tells it apart from the other files) with its sha256.

Import never touches a source file: for each one it checks the sha256 against the sheet (`--allow-sha-mismatch` overrides) and writes
a new `reviewed_labels_<timestamp>.jsonl` plus a `.meta.json` with provenance NEXT TO that file, holding only that file's cases
(with `--out-dir` they all go there instead, numbered when there are several). The effective label of an item is the corrected
label when one was entered, else the original label; the same holds for the need role. Per case the reviewer's free text about
missing context is carried too.

Tabs: Summary (progress formulas, how to review, files), Cases (one row per case), Items (one row per case and item; the main work).
Columns: the read-only ones come from the dataset (`--case-fields`, `--item-columns`; a dotted name reads a nested object, a list
is joined with "; ", a missing field is blank so older files still export); the editable ones are Reviewed, Corrected label,
Corrected need role, Notes, Reviewer on Items and Reviewed, Exclude case, Corrected scope need, Missing context (reviewer), Notes, Reviewer on Cases.
Every column position (validations, colouring, protection, formulas, widths) is computed from the header lists.
Everything is tuned by flags (`--items-key`, `--label-field`, ...), the defaults match a context-needs dataset.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
CELL_LIMIT = 45000  # Sheets allows 50000 characters per cell

DEFAULT_ITEM_COLUMNS = ["describe", "source_kind", "field", "author", "channel", "age_label", "quest_relation", "quest_title", "status", "path",
                        "need_role"]
DEFAULT_CASE_FIELDS = ["source_account_kind", "input_origin", "domain", "task_shape", "scope_need", "quests_involved", "request.created_at", "request.author",
                       "request.channel", "request.quest_title", "request.goal_title", "answer_requirement", "needed_kind",
                       "missing_context"]
DEFAULT_NEED_ROLES = "edit_target,fact_source,instruction,history,background,none"
DEFAULT_SCOPE_NEEDS = "in_quest,cross_quest,multi_quest,no_quest,misfiled,none"
ITEMS_EDIT_TAIL = ["Reviewed", "Corrected label", "Corrected need role", "Notes", "Reviewer"]
CASES_EDIT_TAIL = ["Items reviewed", "Reviewed", "Exclude case", "Corrected scope need",
                  "Missing context (reviewer)", "Notes", "Reviewer"]
FILES_HEADER = "Source file (do not edit)"
WIDTHS = {"case_id": 90, "item_id": 110, "Input": 380, "Item type": 90, "Item text": 520, "Dataset label": 100, "Why (dataset)": 220,
          "Reviewed": 80, "Corrected label": 130, "Corrected need role": 140, "Notes": 260, "Reviewer": 110,
          "Missing context (reviewer)": 260, "Items": 60, "Items reviewed": 90, "Exclude case": 100, "describe": 260, "path": 200,
          "answer_requirement": 260, "missing_context": 240}
DEFAULT_WIDTH = 120


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def services(service_account: str, subject: str):
    from google.oauth2 import service_account as sa
    from googleapiclient.discovery import build

    creds = sa.Credentials.from_service_account_file(service_account, scopes=SCOPES).with_subject(subject)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets, drive


def clip(value) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= CELL_LIMIT else text[:CELL_LIMIT] + " [truncated]"


def col_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def field_text(obj, name: str) -> str:
    """A field of a record as cell text: a dotted name reads nested objects, a list is joined with "; ", a missing field is blank."""
    value = obj
    for part in name.split("."):
        value = value.get(part) if isinstance(value, dict) else None
        if value is None:
            return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def item_head(args) -> list[str]:
    """Items header: dataset columns (read-only) then the editable tail; every position elsewhere is looked up in this list."""
    head = ["case_id", "item_id", "Input", "Item type", *args.item_columns, "Item text", "Dataset label", "Why (dataset)",
            *ITEMS_EDIT_TAIL]
    if len(set(head)) != len(head):
        sys.exit(f"Items columns must have distinct names: {head}")
    return head


def case_head(args) -> list[str]:
    head = [args.case_id, *args.case_fields, "Input", "Items", *CASES_EDIT_TAIL]
    if len(set(head)) != len(head):
        sys.exit(f"Cases columns must have distinct names: {head}")
    return head


def merge_cases(per_file: list[tuple[Path, list[dict]]], case_id: str) -> list[dict]:
    """All cases of all files in order; a case id in two files is an error."""
    seen: dict[str, Path] = {}
    merged = []
    for path, cases in per_file:
        for case in cases:
            cid = case[case_id]
            if cid in seen:
                sys.exit(f"Case id {cid!r} appears in both {seen[cid]} and {path}")
            seen[cid] = path
            merged.append(case)
    return merged


def file_labels(paths: list[Path]) -> list[str]:
    """The shortest trailing part of each path that tells the files apart: the basename, else as many parent folders as needed."""
    parts = [p.resolve().parts for p in paths]
    for depth in range(1, max(len(x) for x in parts) + 1):
        labels = ["/".join(x[-depth:]) for x in parts]
        if len(set(labels)) == len(labels):
            return labels
    return ["/".join(x) for x in parts]


def build_rows(cases: list[dict], args) -> tuple[list[list[str]], list[list[str]]]:
    ihead, chead = item_head(args), case_head(args)
    item_rows = [ihead]
    case_rows = [chead]
    reviewed_items = col_letter(ihead.index("Reviewed"))
    reviewed_formula_col = chead.index("Items reviewed")
    for number, case in enumerate(cases, start=2):
        items = case.get(args.items_key, [])
        for item in items:
            row = {"case_id": case[args.case_id], "item_id": item[args.item_id], "Input": clip(case.get(args.input_field)),
                   "Item type": clip(item.get(args.type_field)), "Item text": clip(item.get(args.text_field)),
                   "Dataset label": item.get(args.label_field, ""), "Why (dataset)": clip(item.get(args.why_field)),
                   "Reviewed": False, "Corrected label": "", "Corrected need role": "", "Notes": "", "Reviewer": ""}
            for column in args.item_columns:
                row[column] = clip(field_text(item, column))
            item_rows.append([row[name] for name in ihead])
        a = col_letter(0)
        formula = (f'=COUNTIFS(Items!${a}:${a},{a}{number},Items!${reviewed_items}:${reviewed_items},TRUE)'
                   f'&" / "&COUNTIF(Items!${a}:${a},{a}{number})')
        row = {args.case_id: case[args.case_id], "Input": clip(case.get(args.input_field)), "Items": len(items),
               "Items reviewed": formula, "Reviewed": False, "Exclude case": "keep", "Corrected scope need": "",
               "Missing context (reviewer)": "",
               "Notes": "", "Reviewer": ""}
        for field in args.case_fields:
            row[field] = clip(field_text(case, field))
        case_rows.append([row[name] for name in chead])
    assert chead.index("Items reviewed") == reviewed_formula_col
    return item_rows, case_rows


def width_requests(sheet_id: int, head: list[str]) -> list[dict]:
    return [{"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": index,
                                                      "endIndex": index + 1},
                                            "properties": {"pixelSize": WIDTHS.get(name, DEFAULT_WIDTH)}, "fields": "pixelSize"}}
            for index, name in enumerate(head)]


def column_range(sheet_id: int, count: int, start: int, end: int | None = None) -> dict:
    return {"sheetId": sheet_id, "startRowIndex": 1, "endRowIndex": count + 1, "startColumnIndex": start,
            "endColumnIndex": start + 1 if end is None else end}


def list_rule(values) -> dict:
    return {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in values]},
            "showCustomUi": True, "strict": True}


def requests_for(sheet_ids: dict, item_count: int, case_count: int, labels: list[str], ihead: list[str], chead: list[str],
                 need_roles: list[str], scope_needs: list[str]) -> list[dict]:
    reqs: list[dict] = []
    for name, count, head in (("Items", item_count, ihead), ("Cases", case_count, chead)):
        sid = sheet_ids[name]
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                                                "fields": "gridProperties.frozenRowCount"}})
        reqs.append({"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": count + 1,
                                                              "startColumnIndex": 0, "endColumnIndex": len(head)}}}})
        reqs.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                                   "backgroundColor": {"red": .85, "green": .9, "blue": .95}}},
                                    "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
        reqs.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": count + 1},
                                    "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                                    "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
        reqs.extend(width_requests(sid, head))
    items, cases = sheet_ids["Items"], sheet_ids["Cases"]
    # Items: Reviewed checkbox, label and need role dropdowns
    reqs.append({"setDataValidation": {"range": column_range(items, item_count, ihead.index("Reviewed")),
                                        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}}})
    reqs.append({"setDataValidation": {"range": column_range(items, item_count, ihead.index("Corrected label")),
                                        "rule": list_rule(labels)}})
    reqs.append({"setDataValidation": {"range": column_range(items, item_count, ihead.index("Corrected need role")),
                                        "rule": list_rule(need_roles)}})
    # Cases: Reviewed checkbox, Exclude dropdown
    reqs.append({"setDataValidation": {"range": column_range(cases, case_count, chead.index("Reviewed")),
                                        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}}})
    reqs.append({"setDataValidation": {"range": column_range(cases, case_count, chead.index("Exclude case")),
                                        "rule": list_rule(("keep", "exclude"))}})
    reqs.append({"setDataValidation": {"range": column_range(cases, case_count, chead.index("Corrected scope need")),
                                        "rule": list_rule(scope_needs)}})
    # a row goes green once reviewed; the dataset label is coloured
    reviewed = col_letter(ihead.index("Reviewed"))
    reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
        "ranges": [column_range(items, item_count, 0, len(ihead))], "booleanRule": {
            "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": f"=${reviewed}2=TRUE"}]},
            "format": {"backgroundColor": {"red": .85, "green": .95, "blue": .85}}}}}})
    palette = {0: (1.0, .9, .6), 1: (.85, .92, 1.0)}
    for position, label in enumerate(labels[:2]):
        r, g, b = palette[position]
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [column_range(items, item_count, ihead.index("Dataset label"))],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": label}]},
                            "format": {"backgroundColor": {"red": r, "green": g, "blue": b}, "textFormat": {"bold": True}}}}}})
    # warn before editing the dataset's own columns (everything before the editable tail)
    for sid, count, end in ((items, item_count, ihead.index("Reviewed")), (cases, case_count, chead.index("Reviewed"))):
        reqs.append({"addProtectedRange": {"protectedRange": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": count + 1, "startColumnIndex": 0, "endColumnIndex": end},
            "description": "Dataset columns: correct the label in Corrected label instead", "warningOnly": True}}})
    return reqs


def summary_rows(args, labels: list[str], need_roles: list[str], scope_needs: list[str], ihead: list[str], chead: list[str], n_items: int, n_cases: int,
                 files: list[tuple[str, str]]) -> tuple[list[list], int]:
    """Summary tab values and the 0-based row of the files table header."""
    rev, cor = col_letter(ihead.index("Reviewed")), col_letter(ihead.index("Corrected label"))
    nrole = col_letter(ihead.index("Corrected need role"))
    crev, cexc = col_letter(chead.index("Reviewed")), col_letter(chead.index("Exclude case"))
    cmiss = col_letter(chead.index("Missing context (reviewer)"))
    cscope = col_letter(chead.index("Corrected scope need"))
    last_i, last_c = n_items + 1, n_cases + 1
    done = f"COUNTIF(Items!{rev}2:{rev}{last_i},TRUE)"
    dataset_cols = f"{col_letter(0)} to {col_letter(ihead.index('Reviewed') - 1)}"
    rows = [
        ["Dataset review"],
        ["Items reviewed", f'={done}&" of "&{n_items}&" ("&TEXT({done}/{max(n_items, 1)},"0%")&")"'],
        ["Items corrected", f'=COUNTIF(Items!{cor}2:{cor}{last_i},"?*")'],
        ["Need roles corrected", f'=COUNTIF(Items!{nrole}2:{nrole}{last_i},"?*")'],
        ["Cases reviewed", f'=COUNTIF(Cases!{crev}2:{crev}{last_c},TRUE)&" of "&{n_cases}'],
        ["Cases marked exclude", f'=COUNTIF(Cases!{cexc}2:{cexc}{last_c},"exclude")'],
        ["Scope needs corrected", f'=COUNTIF(Cases!{cscope}2:{cscope}{last_c},"?*")'],
        ["Cases with missing context noted", f'=COUNTIF(Cases!{cmiss}2:{cmiss}{last_c},"?*")'],
        [],
        ["How to review"],
        ["1. Open the Items tab (filter by case_id, or filter Reviewed = FALSE to see what is left)."],
        ["2. Read the Input and the Item text. The columns between Item type and Item text describe where the item came from "
         "(source, author, channel, how old, how it relates to the quest); they are read-only. Compare with Dataset label ("
         + ", ".join(labels) + ")."],
        ["3. If the label is right, tick Reviewed. If wrong, pick the right label in Corrected label, then tick Reviewed."],
        ["4. Corrected need role says what the item is for: " + ", ".join(need_roles) + ". Leave it empty when the role "
         "shown in the need_role column is right; pick another one when it is wrong."],
        ["5. Notes is free text (why you changed it). Type your name in Reviewer."],
        ["6. On the Cases tab, tick Reviewed when a whole case is done, or set Exclude case to exclude if the case should not be used, "
         "with a Note. Corrected scope need (" + ", ".join(scope_needs) + ") fixes the case's scope_need when it is wrong. "
         "In Missing context (reviewer) write anything the answer needs that no item holds."],
        [f"Leave the grey dataset columns ({dataset_cols} on Items) alone; the importer matches rows by case_id and item_id."],
        [],
    ]
    table_row = len(rows)
    rows.append([FILES_HEADER, "sha256"])
    rows.extend([name, sha] for name, sha in files)
    rows.append(["Exported at (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds")])
    return rows, table_row


def export(args) -> None:
    paths = [Path(c) for c in args.cases]
    per_file = [(path, load_cases(path)) for path in paths]
    cases = merge_cases(per_file, args.case_id)
    files = list(zip(file_labels(paths), [file_sha256(p) for p in paths]))
    labels = args.labels.split(",")
    need_roles = args.need_roles.split(",")
    scope_needs = args.scope_needs.split(",")
    ihead, chead = item_head(args), case_head(args)
    item_rows, case_rows = build_rows(cases, args)
    sheets, drive = services(args.service_account, args.subject)
    title = args.title or f"Dataset review {datetime.now(timezone.utc):%Y-%m-%d}"
    created = sheets.spreadsheets().create(body={"properties": {"title": title}, "sheets": [
        {"properties": {"title": "Summary", "sheetId": 1}},
        {"properties": {"title": "Cases", "sheetId": 2}},
        {"properties": {"title": "Items", "sheetId": 3}}]}).execute()
    sheet_id = created["spreadsheetId"]
    n_items, n_cases = len(item_rows) - 1, len(case_rows) - 1
    summary, table_row = summary_rows(args, labels, need_roles, scope_needs, ihead, chead, n_items, n_cases, files)
    formula_col = chead.index("Items reviewed")
    formula_letter = col_letter(formula_col)
    plain_case_rows = [case_rows[0]] + [[("" if i == formula_col else v) for i, v in enumerate(row)] for row in case_rows[1:]]
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
        {"range": "Items!A1", "values": item_rows},
        {"range": "Cases!A1", "values": plain_case_rows}]}).execute()
    # formulas need USER_ENTERED (the Cases formula column and the Summary), then the file table is rewritten raw
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body={"valueInputOption": "USER_ENTERED", "data": [
        {"range": f"Cases!{formula_letter}2:{formula_letter}{n_cases + 1}",
         "values": [[row[formula_col]] for row in case_rows[1:]]},
        {"range": "Summary!A1", "values": summary}]}).execute()
    sheets.spreadsheets().values().update(spreadsheetId=sheet_id, range=f"Summary!A{table_row + 1}", valueInputOption="RAW",
                                          body={"values": summary[table_row:table_row + 1 + len(files)]}).execute()
    reqs = requests_for({"Items": 3, "Cases": 2}, n_items, n_cases, labels, ihead, chead, need_roles, scope_needs)
    reqs.append({"repeatCell": {"range": {"sheetId": 1, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                                "fields": "userEnteredFormat.textFormat"}})
    reqs.append({"updateDimensionProperties": {"range": {"sheetId": 1, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                                "properties": {"pixelSize": 300}, "fields": "pixelSize"}})
    sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": reqs}).execute()
    perms = drive.permissions().list(fileId=sheet_id, fields="permissions(emailAddress,role,type)").execute()["permissions"]
    print(json.dumps({"spreadsheetId": sheet_id, "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
                      "files": len(files), "items": n_items, "cases": n_cases, "permissions": perms}, indent=1))


def read_tab(sheets, sheet_id: str, tab: str) -> list[dict]:
    values = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=tab, valueRenderOption="UNFORMATTED_VALUE").execute()
    rows = values.get("values", [])
    head = rows[0]
    return [dict(zip(head, row + [""] * (len(head) - len(row)))) for row in rows[1:]]


def truthy(value) -> bool:
    return value is True or str(value).strip().upper() == "TRUE"


def sheet_file_shas(summary_values: list[list]) -> dict[str, str]:
    """The files table of the Summary tab as {file label: sha256}."""
    shas: dict[str, str] = {}
    rows = iter(summary_values)
    for row in rows:
        if row and row[0] == FILES_HEADER:
            break
    for row in rows:
        if len(row) < 2 or not row[0] or not row[1]:
            break
        if row[0].startswith("Exported at"):
            break
        shas[str(row[0])] = str(row[1])
    return shas


def review_records(cases: list[dict], args, item_rows: dict, case_rows: dict, labels: set, need_roles: set, scope_needs: set):
    """(records, counts) for one file's cases from the sheet rows."""
    counts = {"cases": 0, "items": 0, "items_reviewed": 0, "items_corrected": 0, "need_roles_corrected": 0, "scope_needs_corrected": 0, "cases_reviewed": 0,
              "cases_excluded": 0}
    records = []
    for case in cases:
        cid = case[args.case_id]
        case_row = case_rows.get(cid, {})
        record_items = []
        for item in case.get(args.items_key, []):
            row = item_rows.get((cid, item[args.item_id]), {})
            corrected = str(row.get("Corrected label", "")).strip()
            if corrected and corrected not in labels:
                sys.exit(f"Invalid corrected label {corrected!r} for {cid}/{item[args.item_id]}")
            role_corrected = str(row.get("Corrected need role", "")).strip()
            if role_corrected and role_corrected not in need_roles:
                sys.exit(f"Invalid corrected need role {role_corrected!r} for {cid}/{item[args.item_id]}")
            original = item.get(args.label_field)
            original_role = item.get("need_role")
            reviewed = truthy(row.get("Reviewed"))
            record_items.append({"item_id": item[args.item_id], "original_label": original,
                                 "label": corrected or original, "corrected": bool(corrected) and corrected != original,
                                 "need_role": role_corrected or original_role,
                                 "need_role_corrected": bool(role_corrected) and role_corrected != original_role,
                                 "reviewed": reviewed, "notes": str(row.get("Notes", "")).strip(),
                                 "reviewer": str(row.get("Reviewer", "")).strip()})
            counts["items"] += 1
            counts["items_reviewed"] += reviewed
            counts["items_corrected"] += record_items[-1]["corrected"]
            counts["need_roles_corrected"] += record_items[-1]["need_role_corrected"]
        scope_corrected = str(case_row.get("Corrected scope need", "")).strip()
        if scope_corrected and scope_corrected not in scope_needs:
            sys.exit(f"Invalid corrected scope need {scope_corrected!r} for {cid}")
        original_scope = case.get("scope_need")
        scope_changed = bool(scope_corrected) and scope_corrected != original_scope
        excluded = str(case_row.get("Exclude case", "keep")).strip() == "exclude"
        case_reviewed = truthy(case_row.get("Reviewed"))
        counts["cases"] += 1
        counts["scope_needs_corrected"] += scope_changed
        counts["cases_reviewed"] += case_reviewed
        counts["cases_excluded"] += excluded
        records.append({args.case_id: cid, "reviewed": case_reviewed, "excluded": excluded,
                        "scope_need": scope_corrected or original_scope, "scope_need_corrected": scope_changed,
                        "missing_context_reviewer": str(case_row.get("Missing context (reviewer)", "")).strip(),
                        "notes": str(case_row.get("Notes", "")).strip(),
                        "reviewer": str(case_row.get("Reviewer", "")).strip(), "items": record_items})
    return records, counts


def import_reviews(args) -> None:
    paths = [Path(c) for c in args.cases]
    per_file = [(path, load_cases(path)) for path in paths]
    merge_cases(per_file, args.case_id)
    labels_of_files = file_labels(paths)
    sheets, drive = services(args.service_account, args.subject)
    summary = sheets.spreadsheets().values().get(spreadsheetId=args.sheet, range="Summary!A1:B300").execute().get("values", [])
    sheet_shas = sheet_file_shas(summary)
    current = [file_sha256(p) for p in paths]
    for label, sha in zip(labels_of_files, current):
        if sheet_shas.get(label) != sha and not args.allow_sha_mismatch:
            sys.exit(f"The sheet was exported from a different file for {label} (sheet {sheet_shas.get(label)}, file {sha}). "
                     "Pass --allow-sha-mismatch to import anyway.")
    item_rows = {(r["case_id"], r["item_id"]): r for r in read_tab(sheets, args.sheet, "Items")}
    case_rows = {r[args.case_id]: r for r in read_tab(sheets, args.sheet, "Cases")}
    meta = drive.files().get(fileId=args.sheet, fields="modifiedTime,lastModifyingUser(displayName,emailAddress)").execute()
    labels, need_roles = set(args.labels.split(",")), set(args.need_roles.split(","))
    scope_needs = set(args.scope_needs.split(","))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    results = []
    for number, ((path, cases), sha) in enumerate(zip(per_file, current), start=1):
        out_dir = Path(args.out_dir) if args.out_dir else path.parent
        suffix = f"_{number}" if args.out_dir and len(paths) > 1 else ""
        out = out_dir / f"reviewed_labels_{stamp}{suffix}.jsonl"
        if out.exists():
            sys.exit(f"{out} exists; refusing to overwrite")
        records, counts = review_records(cases, args, item_rows, case_rows, labels, need_roles, scope_needs)
        with out.open("x", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        provenance = {"source_cases_file": path.name, "source_cases_sha256": sha, "sheet_id": args.sheet,
                      "sheet_modified_time": meta.get("modifiedTime"), "sheet_last_modified_by": meta.get("lastModifyingUser"),
                      "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "imported_by": args.reviewer,
                      "reviewed_file_sha256": file_sha256(out), "counts": counts,
                      "meaning": "label and need_role are the corrected value when one was entered, else the original"}
        out.with_suffix(".meta.json").write_text(json.dumps(provenance, indent=1) + "\n", encoding="utf-8")
        results.append({"written": str(out), **counts})
    print(json.dumps(results[0] if len(results) == 1 else results, indent=1))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["export", "import"])
    parser.add_argument("--cases", required=True, nargs="+", help="one or more cases files (JSONL)")
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--subject", required=True, help="user to act as; the sheet is owned by and private to them")
    parser.add_argument("--title")
    parser.add_argument("--sheet", help="spreadsheet id (import)")
    parser.add_argument("--reviewer", default="unknown", help="who runs the import (provenance)")
    parser.add_argument("--out-dir", help="write every reviewed file here instead of next to its source file")
    parser.add_argument("--allow-sha-mismatch", action="store_true")
    parser.add_argument("--case-id", default="case_id")
    parser.add_argument("--case-fields", nargs="*", default=DEFAULT_CASE_FIELDS,
                        help="read-only case columns; dotted names read nested objects, lists are joined with '; '")
    parser.add_argument("--item-columns", nargs="*", default=DEFAULT_ITEM_COLUMNS,
                        help="read-only item columns between Item type and Item text (same name rules as --case-fields)")
    parser.add_argument("--input-field", default="input_text")
    parser.add_argument("--items-key", default="candidates")
    parser.add_argument("--item-id", default="item_id")
    parser.add_argument("--type-field", default="type")
    parser.add_argument("--text-field", default="text")
    parser.add_argument("--label-field", default="label")
    parser.add_argument("--why-field", default="why")
    parser.add_argument("--labels", default="needed,helpful,irrelevant", help="comma list of allowed labels, strongest first")
    parser.add_argument("--need-roles", default=DEFAULT_NEED_ROLES, help="comma list of allowed corrected need roles")
    parser.add_argument("--scope-needs", default=DEFAULT_SCOPE_NEEDS, help="comma list of allowed corrected scope needs")
    return parser


def main() -> None:
    parser = make_parser()
    args = parser.parse_args()
    if args.command == "import" and not args.sheet:
        parser.error("import needs --sheet")
    export(args) if args.command == "export" else import_reviews(args)


if __name__ == "__main__":
    main()
