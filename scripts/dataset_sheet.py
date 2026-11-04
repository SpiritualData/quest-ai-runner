#!/usr/bin/env python3
"""Export a labelled JSONL dataset to a private Google Sheet for human review, and import the corrections.

Generic: no data, names or credentials live here. The dataset is a JSONL file with one case per line and a list of labelled
items inside each case (for example candidate context items). Credentials and the user to act as are passed in by the caller.

  export:  dataset_sheet.py export --cases cases.jsonl --service-account KEY.json --subject USER@DOMAIN [--title T]
  import:  dataset_sheet.py import --sheet SHEET_ID --cases cases.jsonl --service-account KEY.json --subject USER@DOMAIN
           --reviewer NAME [--out-dir DIR]

The sheet is created as the `--subject` user (domain-wide delegation) so it is owned by that user and shared with nobody.
Import never touches the source file: it writes a new `reviewed_labels_<timestamp>.jsonl` plus a `.meta.json` with provenance
into `--out-dir` (default: the folder of the cases file), where the effective label of an item is the corrected label when one
was entered, else the original label.

Tabs: Summary (progress formulas, how to review), Cases (one row per case), Items (one row per case and item; the main work).
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

ITEM_HEAD = ["case_id", "item_id", "Input", "Item type", "Item text", "Dataset label", "Why (dataset)",
             "Reviewed", "Corrected label", "Notes", "Reviewer"]
CASE_HEAD_TAIL = ["Items reviewed", "Reviewed", "Exclude case", "Notes", "Reviewer"]


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


def build_rows(cases: list[dict], args) -> tuple[list[list[str]], list[list[str]]]:
    item_rows = [ITEM_HEAD]
    case_head = [args.case_id, *args.case_fields, "Input", "Items"] + CASE_HEAD_TAIL
    case_rows = [case_head]
    for number, case in enumerate(cases, start=2):
        items = case.get(args.items_key, [])
        for item in items:
            item_rows.append([case[args.case_id], item[args.item_id], clip(case.get(args.input_field)),
                              clip(item.get(args.type_field)), clip(item.get(args.text_field)),
                              item.get(args.label_field, ""), clip(item.get(args.why_field)), False, "", "", ""])
        reviewed_formula = (f'=COUNTIFS(Items!$A:$A,A{number},Items!$H:$H,TRUE)&" / "&COUNTIF(Items!$A:$A,A{number})')
        case_rows.append([case[args.case_id], *[clip(case.get(f)) for f in args.case_fields], clip(case.get(args.input_field)),
                          len(items), reviewed_formula, False, "keep", "", ""])
    return item_rows, case_rows


def requests_for(sheet_ids: dict, item_count: int, case_count: int, labels: list[str], case_cols: int) -> list[dict]:
    reqs: list[dict] = []
    widths = {
        "Items": [90, 110, 380, 90, 520, 100, 220, 80, 130, 260, 110],
        "Cases": None,
    }
    for name, count, cols in (("Items", item_count, len(ITEM_HEAD)), ("Cases", case_count, case_cols)):
        sid = sheet_ids[name]
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
                                                "fields": "gridProperties.frozenRowCount"}})
        reqs.append({"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": count + 1,
                                                              "startColumnIndex": 0, "endColumnIndex": cols}}}})
        reqs.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                                   "backgroundColor": {"red": .85, "green": .9, "blue": .95}}},
                                    "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
        reqs.append({"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": count + 1},
                                    "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}},
                                    "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}})
    for index, width in enumerate(widths["Items"]):
        reqs.append({"updateDimensionProperties": {"range": {"sheetId": sheet_ids["Items"], "dimension": "COLUMNS",
                                                              "startIndex": index, "endIndex": index + 1},
                                                    "properties": {"pixelSize": width}, "fields": "pixelSize"}})
    # Items: checkbox in Reviewed (H), label dropdown in Corrected label (I)
    reqs.append({"setDataValidation": {"range": {"sheetId": sheet_ids["Items"], "startRowIndex": 1, "endRowIndex": item_count + 1,
                                                  "startColumnIndex": 7, "endColumnIndex": 8},
                                        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}}})
    reqs.append({"setDataValidation": {"range": {"sheetId": sheet_ids["Items"], "startRowIndex": 1, "endRowIndex": item_count + 1,
                                                  "startColumnIndex": 8, "endColumnIndex": 9},
                                        "rule": {"condition": {"type": "ONE_OF_LIST",
                                                               "values": [{"userEnteredValue": v} for v in labels]},
                                                 "showCustomUi": True, "strict": True}}})
    # Cases: Reviewed checkbox (second last-but-two), Exclude dropdown
    reviewed_col = case_cols - 4
    reqs.append({"setDataValidation": {"range": {"sheetId": sheet_ids["Cases"], "startRowIndex": 1, "endRowIndex": case_count + 1,
                                                  "startColumnIndex": reviewed_col, "endColumnIndex": reviewed_col + 1},
                                        "rule": {"condition": {"type": "BOOLEAN"}, "strict": True}}})
    reqs.append({"setDataValidation": {"range": {"sheetId": sheet_ids["Cases"], "startRowIndex": 1, "endRowIndex": case_count + 1,
                                                  "startColumnIndex": reviewed_col + 1, "endColumnIndex": reviewed_col + 2},
                                        "rule": {"condition": {"type": "ONE_OF_LIST",
                                                               "values": [{"userEnteredValue": v} for v in ("keep", "exclude")]},
                                                 "showCustomUi": True, "strict": True}}})
    # colour the dataset label, and colour a row green once reviewed
    items = sheet_ids["Items"]
    full_row = {"sheetId": items, "startRowIndex": 1, "endRowIndex": item_count + 1, "startColumnIndex": 0,
                "endColumnIndex": len(ITEM_HEAD)}
    reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {"ranges": [full_row], "booleanRule": {
        "condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": "=$H2=TRUE"}]},
        "format": {"backgroundColor": {"red": .85, "green": .95, "blue": .85}}}}}})
    palette = {0: (1.0, .9, .6), 1: (.85, .92, 1.0)}
    for position, label in enumerate(labels[:2]):
        r, g, b = palette[position]
        reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": items, "startRowIndex": 1, "endRowIndex": item_count + 1, "startColumnIndex": 5,
                        "endColumnIndex": 6}],
            "booleanRule": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": label}]},
                            "format": {"backgroundColor": {"red": r, "green": g, "blue": b}, "textFormat": {"bold": True}}}}}})
    # warn before editing the dataset's own columns (A to G on Items)
    reqs.append({"addProtectedRange": {"protectedRange": {
        "range": {"sheetId": items, "startRowIndex": 0, "endRowIndex": item_count + 1, "startColumnIndex": 0, "endColumnIndex": 7},
        "description": "Dataset columns: correct the label in Corrected label instead", "warningOnly": True}}})
    return reqs


def export(args) -> None:
    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    sha = file_sha256(cases_path)
    labels = args.labels.split(",")
    item_rows, case_rows = build_rows(cases, args)
    sheets, drive = services(args.service_account, args.subject)
    title = args.title or f"Dataset review {datetime.now(timezone.utc):%Y-%m-%d}"
    created = sheets.spreadsheets().create(body={"properties": {"title": title}, "sheets": [
        {"properties": {"title": "Summary", "sheetId": 1}},
        {"properties": {"title": "Cases", "sheetId": 2}},
        {"properties": {"title": "Items", "sheetId": 3}}]}).execute()
    sheet_id = created["spreadsheetId"]
    n_items, n_cases = len(item_rows) - 1, len(case_rows) - 1
    summary = [
        ["Dataset review"],
        ["Items reviewed", f'=COUNTIF(Items!H2:H{n_items + 1},TRUE)&" of "&{n_items}&" ("&TEXT(COUNTIF(Items!H2:H{n_items + 1},TRUE)/{n_items},"0%")&")"'],
        ["Items corrected", f'=COUNTIF(Items!I2:I{n_items + 1},"?*")'],
        ["Cases reviewed", f'=COUNTIF(Cases!{col_letter(len(case_rows[0]) - 4)}2:{col_letter(len(case_rows[0]) - 4)}{n_cases + 1},TRUE)&" of "&{n_cases}'],
        ["Cases marked exclude", f'=COUNTIF(Cases!{col_letter(len(case_rows[0]) - 3)}2:{col_letter(len(case_rows[0]) - 3)}{n_cases + 1},"exclude")'],
        [],
        ["How to review"],
        ["1. Open the Items tab (filter by case_id, or filter Reviewed = FALSE to see what is left)."],
        ["2. Read the Input and the Item text. Compare with Dataset label (" + ", ".join(labels) + ")."],
        ["3. If the label is right, tick Reviewed. If wrong, pick the right label in Corrected label, then tick Reviewed."],
        ["4. Notes is free text (why you changed it). Type your name in Reviewer."],
        ["5. On the Cases tab, tick Reviewed when a whole case is done, or set Exclude case to exclude if the case should not be used, with a Note."],
        ["Leave the grey dataset columns (A to G) alone; the importer matches rows by case_id and item_id."],
        [],
        ["Source file sha256 (do not edit)", sha],
        ["Exported at (UTC)", datetime.now(timezone.utc).isoformat(timespec="seconds")],
    ]
    case_head = case_rows[0]
    case_cols = len(case_head)
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body={"valueInputOption": "RAW", "data": [
        {"range": "Items!A1", "values": item_rows},
        {"range": "Summary!A15:B16", "values": summary[14:]}]}).execute()
    # cases and summary contain formulas: USER_ENTERED
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=sheet_id, body={"valueInputOption": "USER_ENTERED", "data": [
        {"range": "Cases!A1", "values": [[clip(v) if isinstance(v, str) and not v.startswith("=") else v for v in row]
                                          for row in case_rows]},
        {"range": "Summary!A1", "values": summary[:14]}]}).execute()
    # Case text fields starting with = would be formulas under USER_ENTERED; rewrite them raw
    raw_cols = [[row[i] for row in case_rows] for i in range(case_cols - 6)]
    sheets.spreadsheets().values().update(spreadsheetId=sheet_id, range=f"Cases!A1:{col_letter(case_cols - 7)}{n_cases + 1}",
                                          valueInputOption="RAW", body={"values": [list(r) for r in zip(*raw_cols)]}).execute()
    reqs = requests_for({"Items": 3, "Cases": 2}, n_items, n_cases, labels, case_cols)
    reqs.append({"repeatCell": {"range": {"sheetId": 1, "startRowIndex": 0, "endRowIndex": 1},
                                "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}},
                                "fields": "userEnteredFormat.textFormat"}})
    reqs.append({"updateDimensionProperties": {"range": {"sheetId": 1, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                                "properties": {"pixelSize": 300}, "fields": "pixelSize"}})
    sheets.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": reqs}).execute()
    perms = drive.permissions().list(fileId=sheet_id, fields="permissions(emailAddress,role,type)").execute()["permissions"]
    print(json.dumps({"spreadsheetId": sheet_id, "url": f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit",
                      "items": n_items, "cases": n_cases, "permissions": perms}, indent=1))


def read_tab(sheets, sheet_id: str, tab: str) -> list[dict]:
    values = sheets.spreadsheets().values().get(spreadsheetId=sheet_id, range=tab, valueRenderOption="UNFORMATTED_VALUE").execute()
    rows = values.get("values", [])
    head = rows[0]
    return [dict(zip(head, row + [""] * (len(head) - len(row)))) for row in rows[1:]]


def truthy(value) -> bool:
    return value is True or str(value).strip().upper() == "TRUE"


def import_reviews(args) -> None:
    cases_path = Path(args.cases)
    cases = load_cases(cases_path)
    sheets, drive = services(args.service_account, args.subject)
    summary = sheets.spreadsheets().values().get(spreadsheetId=args.sheet, range="Summary!A15:B15").execute().get("values", [[]])
    sheet_sha = summary[0][1] if summary and len(summary[0]) > 1 else None
    current_sha = file_sha256(cases_path)
    if sheet_sha != current_sha and not args.allow_sha_mismatch:
        sys.exit(f"The sheet was exported from a different cases file (sheet {sheet_sha}, file {current_sha}). "
                 "Pass --allow-sha-mismatch to import anyway.")
    item_rows = {(r["case_id"], r["item_id"]): r for r in read_tab(sheets, args.sheet, "Items")}
    case_rows = {r[args.case_id]: r for r in read_tab(sheets, args.sheet, "Cases")}
    meta = drive.files().get(fileId=args.sheet, fields="modifiedTime,lastModifyingUser(displayName,emailAddress)").execute()
    labels = set(args.labels.split(","))
    out_dir = Path(args.out_dir) if args.out_dir else cases_path.parent
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"reviewed_labels_{stamp}.jsonl"
    if out.exists():
        sys.exit(f"{out} exists; refusing to overwrite")
    counts = {"cases": 0, "items": 0, "items_reviewed": 0, "items_corrected": 0, "cases_reviewed": 0, "cases_excluded": 0}
    with out.open("x", encoding="utf-8") as handle:
        for case in cases:
            case_row = case_rows.get(case[args.case_id], {})
            record_items = []
            for item in case.get(args.items_key, []):
                row = item_rows.get((case[args.case_id], item[args.item_id]), {})
                corrected = str(row.get("Corrected label", "")).strip()
                if corrected and corrected not in labels:
                    sys.exit(f"Invalid corrected label {corrected!r} for {case[args.case_id]}/{item[args.item_id]}")
                original = item.get(args.label_field)
                reviewed = truthy(row.get("Reviewed"))
                record_items.append({"item_id": item[args.item_id], "original_label": original,
                                     "label": corrected or original, "corrected": bool(corrected) and corrected != original,
                                     "reviewed": reviewed, "notes": str(row.get("Notes", "")).strip(),
                                     "reviewer": str(row.get("Reviewer", "")).strip()})
                counts["items"] += 1
                counts["items_reviewed"] += reviewed
                counts["items_corrected"] += record_items[-1]["corrected"]
            excluded = str(case_row.get("Exclude case", "keep")).strip() == "exclude"
            case_reviewed = truthy(case_row.get("Reviewed"))
            counts["cases"] += 1
            counts["cases_reviewed"] += case_reviewed
            counts["cases_excluded"] += excluded
            handle.write(json.dumps({args.case_id: case[args.case_id], "reviewed": case_reviewed, "excluded": excluded,
                                     "notes": str(case_row.get("Notes", "")).strip(),
                                     "reviewer": str(case_row.get("Reviewer", "")).strip(), "items": record_items},
                                    ensure_ascii=False) + "\n")
    provenance = {"source_cases_file": cases_path.name, "source_cases_sha256": current_sha, "sheet_id": args.sheet,
                  "sheet_modified_time": meta.get("modifiedTime"), "sheet_last_modified_by": meta.get("lastModifyingUser"),
                  "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "imported_by": args.reviewer,
                  "reviewed_file_sha256": file_sha256(out), "counts": counts,
                  "meaning": "label is the corrected label when one was entered, else the original label"}
    out.with_suffix(".meta.json").write_text(json.dumps(provenance, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"written": str(out), **counts}, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["export", "import"])
    parser.add_argument("--cases", required=True)
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--subject", required=True, help="user to act as; the sheet is owned by and private to them")
    parser.add_argument("--title")
    parser.add_argument("--sheet", help="spreadsheet id (import)")
    parser.add_argument("--reviewer", default="unknown", help="who runs the import (provenance)")
    parser.add_argument("--out-dir")
    parser.add_argument("--allow-sha-mismatch", action="store_true")
    parser.add_argument("--case-id", default="case_id")
    parser.add_argument("--case-fields", nargs="*", default=["domain", "needed_kind"])
    parser.add_argument("--input-field", default="input_text")
    parser.add_argument("--items-key", default="candidates")
    parser.add_argument("--item-id", default="item_id")
    parser.add_argument("--type-field", default="type")
    parser.add_argument("--text-field", default="text")
    parser.add_argument("--label-field", default="label")
    parser.add_argument("--why-field", default="why")
    parser.add_argument("--labels", default="needed,helpful,irrelevant", help="comma list of allowed labels, strongest first")
    args = parser.parse_args()
    if args.command == "import" and not args.sheet:
        parser.error("import needs --sheet")
    export(args) if args.command == "export" else import_reviews(args)


if __name__ == "__main__":
    main()
