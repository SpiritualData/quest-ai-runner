#!/usr/bin/env python3
"""Show QAR's evaluation datasets in an existing Google Sheet: one row per dataset and one per input, with the status of the leading
implementation and how it last answered.

Generic: no data, names or credentials live here. The caller passes the credentials, the user to act as and the sheet, and names the private
datasets by path (they are read, never copied into this repository). Uses the Sheets calls of ``dataset_sheet.py``.

  qar_datasets_sheet.py --sheet SHEET_ID --service-account KEY.json --subject USER@DOMAIN
        [--poor-answers FILE_OR_DIR] [--context-dataset LABEL=FOLDER ...] [--output-dir DIR] [--raw-dir DIR] [--owner TEXT] [--dry-run]

Tabs written (only these three, always the first three tabs in this order, so they open first; every other tab of the sheet is left alone,
and the sheet is never created or shared here):
  Datasets    one row per dataset: id, name, what it tests, owner, PRIORITY (a dropdown a human sets), STATUS (one line per environment, from the
              results), inputs count, metrics that use it, private or repo, source label, HOW TO RUN (the exact command), notes.
  Inputs      (a private set may add What went wrong, Ideal handling, Input note and the Original input text, read from optional fields of its rows)
              one row per input and ENVIRONMENT that has a result (the latest result of each): dataset, input id, input text (repo datasets and the
              poorly answered set; never a context dataset), category, PRIORITY, status, environment ("dev eval world" or "production (writes
              blocked)", from the log line, else from the arm), the BEST ALTERNATIVE (the arm that answers in that environment), ITS LAST OUTPUT (the
              reply from ``QAR_EVAL_OUTPUT_DIR`` lines, else the runner's raw result file, else "no recorded output yet"), score, judge reason,
              run date and commit, notes.
  How to use  what each editable column means, how priority is set, how to run a dataset, how to refresh.

The editable columns (Priority, Notes) are carried over on every rewrite by header name: a row that still exists keeps what a human entered. A
new row gets a preset priority only where ``PRESET_HIGH`` says so (shown as "preset, human may change"); otherwise it is blank for a human to set.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dataset_sheet as ds  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
COMPONENTS = REPO / "evaluation" / "components"
TABS = ("Datasets", "Inputs", "How to use")
TAB_IDS = {"Datasets": 9101, "Inputs": 9102, "How to use": 9103}   # TABS is also the tab order: index 0, 1, 2
DEV_ENV, PROD_ENV, OTHER_ENV = "dev eval world", "production (writes blocked)", "offline or local"
BOUNDED_ENV = "production (bounded writes)"
PROD_ARMS = {"chat_quality": ["prod_qar", "prod_qar_readonly"]}   # the arms that answer on production: bounded writes, then every write blocked
ARM_ENVIRONMENT = {"dev_quest_chat": DEV_ENV, "qar_live": DEV_ENV, "prod_qar_readonly": PROD_ENV, "prod_qar": BOUNDED_ENV}   # for a log line that names no environment
PRIORITIES = ("High", "Medium", "Low")
PRESET_NOTE = "preset, human may change"
PRESET_HIGH = ("chat_quality/acceptance", "poor_answers")
NO_OUTPUT = "no recorded output yet"
REPLY_LIMIT = 8000
DATASETS_HEAD = ["Dataset id", "Name", "What it tests", "Owner", "PRIORITY", "Priority set by", "STATUS", "Inputs", "Metrics that use it",
                 "Private or repo", "Source", "How to run", "Notes"]
INPUTS_HEAD = ["Dataset", "Input id", "Group", "Input text", "Category", "What went wrong", "Ideal handling", "Input note", "Original input text", "PRIORITY", "Priority set by", "Status", "Environment", "BEST ALTERNATIVE",
               "ITS LAST OUTPUT", "Messages (in order)", "Metric", "Before", "After", "Moved?", "Output from", "Acted as", "Score", "Tokens", "Model calls", "Lane gaps", "Judge reason", "Run date", "Commit", "Notes"]
EDITABLE = {"Datasets": ["PRIORITY", "Notes"], "Inputs": ["PRIORITY", "Notes"]}
KEYS = {"Datasets": ("Dataset id",), "Inputs": ("Dataset", "Input id", "Environment")}
WIDTHS = {"Dataset id": 170, "Name": 200, "What it tests": 320, "Owner": 90, "PRIORITY": 90, "Priority set by": 150, "STATUS": 260, "Inputs": 60,
          "Metrics that use it": 260, "Private or repo": 90, "Source": 260, "Notes": 260, "Dataset": 170, "Input id": 110, "Group": 110, "Acted as": 170, "Messages (in order)": 520, "Metric": 160, "Before": 70, "After": 70, "Moved?": 120, "Input text": 560, "Environment": 150, "What went wrong": 360, "Ideal handling": 360, "Input note": 260, "Original input text": 360, "How to run": 380,
          "Category": 180, "Status": 80, "BEST ALTERNATIVE": 140, "ITS LAST OUTPUT": 520, "Output from": 120, "Score": 60, "Tokens": 80, "Model calls": 80, "Lane gaps": 140, "Judge reason": 300,
          "Run date": 90, "Commit": 90}

CHAT = ("chat_quality", "metric-answer-a-turn, QME1, metric-qar-overall")
DESCRIPTIONS = {
    "chat_quality/acceptance": ("Acceptance cases", "30 cases, one per required behaviour of the chat: grounded inline answers, routing, honest limits, exact changes", CHAT),
    "chat_quality/explicit": ("Explicit quest", "Questions and changes with the conversation pinned to the quest", CHAT),
    "chat_quality/implicit": ("Implicit quest", "No quest pinned: the assistant must work out which quest is meant", CHAT),
    "chat_quality/multistep": ("Multi-step", "Requests that decompose into several steps with order or independence constraints", CHAT),
    "chat_quality/web": ("Web", "Questions that need a web search and a cited, honest answer", CHAT),
    "chat_quality/reliability": ("Reliability", "Repeat-prone cases that guard against regressions in behaviour that once failed", CHAT),
    "chat_quality/experiments": ("Experiments", "Experiment-tracking requests (create, update, conclude)", CHAT),
    "chat_quality/experiments_controls": ("Experiment controls", "Control cases beside the experiments dataset", CHAT),
    "end_to_end/cases": ("End to end", "Whole-path turns on a small world, scored by the world afterwards", ("end_to_end", "metric-answer-end-to-end, QME1, metric-qar-overall")),
    "end_to_end/bulk": ("End to end, bulk analysis", "Turns that read and classify many records", ("end_to_end", "metric-answer-end-to-end")),
    "end_to_end/guidance": ("End to end, guidance", "Turns that give product guidance from the documented facts", ("end_to_end", "metric-answer-end-to-end")),
    "poor_answers": ("Poorly answered inputs", "Real inputs production answered badly, judged on honest, correctly routed, non-fabricating handling",
                     ("chat_quality", "metric-answer-a-turn, QME1, metric-qar-overall (conditional source, share in esoi.toml)")),
}
ANSWER_ARMS = {"chat_quality": "dev_quest_chat", "end_to_end": "qar_live"}   # the implementation that gives the reply a person reads; other components use the leading arm
END_TO_END_FILES = {"end_to_end/cases": "cases.json", "end_to_end/bulk": "cases_bulk.json", "end_to_end/guidance": "cases_guidance.json"}


def read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def cases_of(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["cases"] if isinstance(data, dict) and "cases" in data else data


def case_text(case: dict) -> str:
    return case.get("message") or " / ".join(case.get("messages") or [])


def repo_datasets(components: Path = COMPONENTS) -> list[dict]:
    """The datasets the repository holds: every chat_quality dataset file (not the digest of private sources, not a file starting with an
    underscore) and the end_to_end case files."""
    found = []
    for path in sorted((components / "chat_quality" / "datasets").glob("*.json")):
        did = f"chat_quality/{path.stem}"
        if path.name.startswith("_") or did not in DESCRIPTIONS:
            continue
        found.append(dict(id=did, private=False, source=f"repo: evaluation/components/chat_quality/datasets/{path.name}",
                          cases=[dict(id=c["id"], text=case_text(c), category=c.get("area", "")) for c in cases_of(path)]))
    for did, name in END_TO_END_FILES.items():
        path = components / "end_to_end" / name
        if path.is_file():
            found.append(dict(id=did, private=False, source=f"repo: evaluation/components/end_to_end/{name}",
                              cases=[dict(id=c["id"], text=case_text(c), category=c.get("stratum", "") or c.get("kind", "")) for c in cases_of(path)]))
    return found


def poor_answer_dataset(path: str) -> dict | None:
    """The poorly answered set (a file, or a folder holding ``poorly_answered_inputs.jsonl``), or None when it is not there."""
    target = Path(path).expanduser()
    target = target / "poorly_answered_inputs.jsonl" if target.is_dir() else target
    if not target.is_file():
        return None
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    return dict(id="poor_answers", private=True, source="private: the poorly answered set (QAR_PRIVATE_POOR_ANSWERS_DATASET)",
                cases=[dict(id=r["case_id"], text=r.get("input_text", ""), category=r.get("failure_kind", ""), group=r.get("group", ""), went_wrong=r.get("what_went_wrong", ""),
                            ideal=r.get("ideal_handling", ""), note=r.get("input_note", ""),
                            original=r.get("original_input_text", "") if r.get("original_input_text", "") != r.get("input_text", "") else "")
                       for r in rows if r.get("case_id")])


def context_dataset(label: str, folder: str) -> dict | None:
    """A private context dataset: ids only, never an input text (the sheet shows counts and status for these)."""
    target = Path(folder).expanduser() / "cases.jsonl"
    if not target.is_file():
        return None
    ids = [json.loads(line).get("case_id") for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    return dict(id=f"context/{label}", private=True, name=f"Private context requests ({label})", component="context_selection",
                what="Real requests with labelled candidate items: does the selector pick the items the answer needs (ids and status only here)",
                metrics="metric-select-context (private case source)", source=f"private: QAR_PRIVATE_CONTEXT_DATASET folder ({label})",
                cases=[dict(id=f"PRIV-{i}", text="", category="") for i in ids if i])


def describe(dataset: dict) -> dict:
    """Fill name, what it tests, component and metrics from the table (a context dataset brings its own)."""
    if dataset["id"] in DESCRIPTIONS:
        name, what, (component, metrics) = DESCRIPTIONS[dataset["id"]]
        dataset.update(name=name, what=what, component=component, metrics=metrics)
    return dataset


def number(text, default=0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def leading_arm(results: list[dict], component: str) -> str:
    """The leading implementation of a component: of the arms whose latest row called a model (all arms when none did), the one with the best
    balanced score, else the best pass rate; the later date breaks a tie."""
    latest = {}
    for row in results:
        if row["component"] == component:
            latest[row["impl"]] = row
    live = {k: v for k, v in latest.items() if number(v.get("model_calls")) > 0} or latest
    if not live:
        return ""

    def rank(item):
        row = item[1]
        value = number(row.get("balanced"), -1) if row.get("balanced") else number(row.get("passed")) / max(number(row.get("cases")), 1)
        return value, row.get("date", "")
    return max(live.items(), key=rank)[0]


def latest_by(rows: list[dict], key) -> dict:
    found = {}
    for row in rows:
        found[key(row)] = row
    return found


def read_output_log(folder: str) -> dict:
    """``{(dataset, case id): [lines, oldest first]}`` from ``QAR_EVAL_OUTPUT_DIR``-style files (``<dir>/<dataset>/<case id>.jsonl``)."""
    found: dict = {}
    if not folder or not Path(folder).is_dir():
        return found
    for path in sorted(Path(folder).glob("*/*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entry = json.loads(line)
                found.setdefault((entry.get("dataset", path.parent.name), entry.get("case_id", path.stem)), []).append(entry)
                found.setdefault(("*", entry.get("case_id", path.stem)), []).append(entry)   # a case id names one case whatever folder its source wrote it to
    return found


def read_raw(folder: str) -> dict:
    """``{case id: record}`` from the chat runner's raw result files (one JSON per case: evidence, passed, score, judged)."""
    found = {}
    if folder and Path(folder).is_dir():
        for path in sorted(Path(folder).glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            record["file_date"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d")
            found[record.get("id", path.stem)] = record
    return found


def judge_reason(record: dict) -> str:
    judged = record.get("judged") or {}
    return str(judged.get("summary") or (judged.get("verdict") or {}).get("summary") or "")


def messages_text(messages) -> str:
    """The assistant messages of a turn in order as one cell: ``(1) first | (2) second``."""
    return " | ".join(f"({m.get('n', i + 1)}) {m.get('text', '')}" for i, m in enumerate(messages or []) if isinstance(m, dict))


def acted_as_text(value) -> str:
    """The account a run acted as, from the log line's ``acted_as`` (a dict of identity fields), as one short text."""
    if isinstance(value, dict):
        return " ".join(str(value.get(k)) for k in ("email", "user_id", "id", "error") if value.get(k))
    return str(value or "")


def environment_of(entry_environment, arm: str) -> str:
    return entry_environment or ARM_ENVIRONMENT.get(arm, OTHER_ENV)


def input_state(dataset: dict, case: dict, arm: str, sources: dict) -> dict:
    """What is known of one input for one arm: status, reply, score, reason, date, commit and environment, from the output log first (the
    latest line of that arm), then the runner's raw result file (dev runs only), then the per-case results."""
    component, case_id = dataset["component"], case["id"]
    lines = sources["log"].get((dataset["id"], case_id), []) + sources["log"].get((dataset["id"].split("/")[-1], case_id), []) or sources["log"].get(("*", case_id), [])
    mine = [e for e in lines if e.get("arm") == arm]
    if mine:
        last = max(mine, key=lambda e: e.get("timestamp", ""))
        return dict(status="pass" if last.get("passed") else "fail", reply=last.get("reply", ""), source="output log", score=last.get("score", ""),
                    acted_as=acted_as_text(last.get("acted_as")), messages=messages_text(last.get("messages")), metric=last.get("metric") or {},
                    tokens=sum(int(last.get(k) or 0) for k in ("tokens_in", "tokens_out")) or "", calls=last.get("model_calls", ""),
                    gaps=", ".join(last.get("lane_gaps") or []), reason=last.get("reason", ""), date=str(last.get("timestamp", ""))[:10], commit=last.get("commit", ""),
                    environment=environment_of(last.get("environment"), arm))
    environment = environment_of("", arm)
    raw = sources["raw"].get(case_id) if component == "chat_quality" and environment not in (PROD_ENV, BOUNDED_ENV) else None
    if raw and (raw.get("evidence") or {}).get("reply") is not None and raw.get("passed") is not None:
        return dict(status="pass" if raw["passed"] else "fail", reply=raw["evidence"].get("reply", ""), source="runner raw result",
                    score=raw.get("score") if raw.get("score") is not None else "", reason=judge_reason(raw), date=raw["file_date"], commit="",
                    environment=environment)
    row = sources["per_case"].get((component, arm, case_id))
    if row:
        ran = sources["results"].get((component, arm, row.get("cases_sha", ""), row.get("sample", "")), {})
        return dict(status="pass" if row.get("ok") in ("1", "True", "true") else "fail", reply=NO_OUTPUT, source="", score=row.get("score", ""),
                    reason="", date=ran.get("date", ""), commit=ran.get("git_sha", ""), environment=environment)
    return dict(status="unknown", reply=NO_OUTPUT, source="", score="", reason="", date="", commit="", environment=environment)


def dataset_status(states: list[dict]) -> str:
    known = [s for s in states if s["status"] != "unknown"]
    if not known:
        return "not run"
    passed = sum(s["status"] == "pass" for s in known)
    last = max(known, key=lambda s: s["date"])
    where = f"last run {last['date'] or 'date unknown'}" + (f", commit {last['commit']}" if last["commit"] else "")
    covered = f" ({len(known)} of {len(states)} inputs have a result)" if len(known) < len(states) else ""
    return f"{'passing' if passed == len(known) else 'failing'} {passed} of {len(known)}{covered}, {where}"


def arms_of(dataset: dict, sources: dict) -> list[str]:
    """The arms that answer this dataset, one per environment: the dev arm (or the leading arm of the component), then the production arm."""
    component = dataset["component"]
    arms = [ANSWER_ARMS.get(component) or leading_arm(sources["results_list"], component)]
    arms += PROD_ARMS.get(component, [])
    return arms


def run_command(dataset: dict, prefix: str) -> str:
    """The exact command that runs this dataset: the deployment's wrapper for a chat dataset (``prefix``), else the component's own run."""
    component = dataset["component"]
    if component == "chat_quality" and prefix:
        name = dataset["id"].split("/")[-1]
        return f"{prefix} {name} prod   (or: {prefix} {name} prod --readonly, or: {prefix} {name} dev)"
    if dataset["id"].startswith("context/"):
        return f"esoi run {component} (the private case source is read from its environment variable)"
    return f"esoi run {component} --impl {ANSWER_ARMS.get(component) or '<arm>'}"


def build_rows(datasets: list[dict], sources: dict, owner: str, command_prefix: str = "") -> tuple[list[list], list[list]]:
    drows, irows = [DATASETS_HEAD], [INPUTS_HEAD]
    for dataset in datasets:
        arms = arms_of(dataset, sources)
        preset = "High" if dataset["id"] in PRESET_HIGH else ""
        by_env: dict = {}
        for case in dataset["cases"]:
            states = [(arm, input_state(dataset, case, arm, sources)) for arm in arms]
            shown = [(arm, st) for arm, st in states if st["status"] != "unknown"] or states[:1]
            text = "" if dataset["id"].startswith("context/") else ds.clip(case["text"])
            for arm, state in shown:
                by_env.setdefault(state["environment"], []).append(state)
                irows.append([dataset["id"], case["id"], case.get("group", ""), text, case["category"], ds.clip(case.get("went_wrong", "")), ds.clip(case.get("ideal", "")),
                              ds.clip(case.get("note", "")), ds.clip(case.get("original", "")), preset, PRESET_NOTE if preset else "", state["status"], state["environment"], arm,
                              ds.clip(state["reply"])[:REPLY_LIMIT], ds.clip(state.get("messages", ""))[:REPLY_LIMIT], (state.get("metric") or {}).get("name", ""),
                              (state.get("metric") or {}).get("before", ""), (state.get("metric") or {}).get("after", ""), (state.get("metric") or {}).get("moved", ""),
                              state["source"], state.get("acted_as", ""), state["score"], state.get("tokens", ""), state.get("calls", ""),
                              state.get("gaps", ""), state["reason"], state["date"], state["commit"], ""])
        sizes = len(dataset["cases"])
        envs = env_order(set(by_env) | {environment_of("", arm) for arm in arms})
        status = " | ".join(f"{env}: {dataset_status(states_for(env, arms, dataset, sources))}" for env in envs)
        drows.append([dataset["id"], dataset["name"], dataset["what"], owner, preset, PRESET_NOTE if preset else "", status,
                      sizes, dataset["metrics"], "private" if dataset["private"] else "repo", dataset["source"], run_command(dataset, command_prefix), ""])
    return drows, irows


def env_order(envs) -> list[str]:
    return sorted(envs, key=lambda e: (e != DEV_ENV, e != BOUNDED_ENV, e != PROD_ENV, e))


def states_for(env: str, arms: list[str], dataset: dict, sources: dict) -> list[dict]:
    """Every input's state for the arm of ``env`` (an input with no result there counts as not run, so the status says how many have one)."""
    arm = next((a for a in arms if environment_of("", a) == env), arms[0])
    return [input_state(dataset, case, arm, sources) for case in dataset["cases"]]


def carry_over(old_rows: list[dict], new_rows: list[list], tab: str) -> int:
    """Copy the human-entered columns of ``tab`` from the old rows (dicts by header name) onto the rows whose key still exists; a changed
    priority is labelled as set by a human. A row of a sheet written before the Environment column carried over to the first row of the same
    input. Returns the number of rows carried."""
    head, keys = new_rows[0], KEYS[tab]
    old = {tuple(str(r.get(k, "")) for k in keys): r for r in old_rows}
    short = {}
    for r in old_rows:
        if "Environment" in r:
            continue
        short.setdefault(tuple(str(r.get(k, "")) for k in keys[:2]), r)
    carried = 0
    for row in new_rows[1:]:
        before = old.get(tuple(str(row[head.index(k)]) for k in keys))
        if before is None:
            before = short.get(tuple(str(row[head.index(k)]) for k in keys[:2])) if tab == "Inputs" else None
        if before is None:
            continue
        carried += 1
        for column in EDITABLE[tab]:
            if column in before:
                row[head.index(column)] = before[column]
        priority = row[head.index("PRIORITY")]
        preset = "High" if row[0] in PRESET_HIGH else ""
        row[head.index("Priority set by")] = PRESET_NOTE if priority and priority == preset else ("human" if priority else "")
    return carried


def how_to_use(sheet_stamp: str) -> list[list]:
    return [["How to use this sheet"], [],
            ["What it is", "Every evaluation dataset of the quest-ai-runner and each of its inputs, with how it was last answered in each environment. Rewritten by a command; only the editable columns below survive a rewrite."],
            ["Datasets tab", "One row per dataset. STATUS has one entry per environment, computed from the results (passing or failing, how many inputs have a result, the date and commit of the last run); 'not run' means no result is recorded. HOW TO RUN is the exact command."],
            ["Inputs tab", "One row per input and environment that has a result. Environment says where the answer came from: 'dev eval world' (a disposable development account) or 'production (bounded writes)' (the real production brain and data; only the writes the case itself allows, such as one reminder or one note, are made, and the matching items are counted before and after) or 'production (writes blocked)' (the same with every state change recorded and not made). BEST ALTERNATIVE is the implementation that answers in that environment. ITS LAST OUTPUT is the reply that implementation gave on its latest run on this input; 'no recorded output yet' means no run has saved a reply."],
            ["PRIORITY (editable)", "A dropdown: High, Medium or Low. A human sets it. Blank means not yet set. The acceptance cases and the poorly answered inputs are preset to High (the column Priority set by says 'preset, human may change'); change them freely."],
            ["Notes (editable)", "Free text on a dataset or an input. Never overwritten."],
            ["Do not edit", "Every other column is rewritten from the data at each refresh; edits there are lost."],
            ["Privacy", "Input text is shown for repository datasets and the poorly answered set. Context datasets show ids and status only. The sheet is private to its owner; do not share it."],
            ["How rows are matched", "Priority and Notes are carried over by Dataset id (Datasets) and Dataset, Input id and Environment (Inputs). A removed input loses its row."],
            ["How to run a dataset", "One command for every chat dataset: the HOW TO RUN column of the Datasets tab, for example `run-qar-dataset.sh poor_answers prod` (production, bounded writes; add `--readonly` to block every write) or `... dev` (the dev eval world). It runs the cases, then refreshes this sheet. Optional `--only ID,ID` runs some inputs."],
            ["Where outputs come from", "Lines appended to the output directory (QAR_EVAL_OUTPUT_DIR) by every chat_quality and end_to_end run, each naming its environment; else the chat runner's raw result files; else the per-case results (status only)."],
            ["How to refresh", "Run the refresh command of the deployment (it passes the credentials, the sheet and the private dataset paths to scripts/qar_datasets_sheet.py). It costs no model call. The run command above refreshes by itself at the end."],
            ["Last refreshed (UTC)", sheet_stamp]]


def requests_for(rows: dict[str, int]) -> list[dict]:
    reqs = []
    for tab, head, count in (("Datasets", DATASETS_HEAD, rows["Datasets"]), ("Inputs", INPUTS_HEAD, rows["Inputs"])):
        sid = TAB_IDS[tab]
        reqs += [{"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}}, "fields": "gridProperties.frozenRowCount"}},
                 {"setBasicFilter": {"filter": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": count + 1, "startColumnIndex": 0, "endColumnIndex": len(head)}}}},
                 {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": .85, "green": .9, "blue": .95}}}, "fields": "userEnteredFormat(textFormat,backgroundColor)"}},
                 {"repeatCell": {"range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": count + 1}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}},
                 {"setDataValidation": {"range": ds.column_range(sid, count, head.index("PRIORITY")), "rule": ds.list_rule(PRIORITIES)}}]
        reqs += [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1}, "properties": {"pixelSize": WIDTHS.get(name, 120)}, "fields": "pixelSize"}} for i, name in enumerate(head)]
        for column in ("PRIORITY", "Notes"):
            reqs.append({"repeatCell": {"range": ds.column_range(sid, count, head.index(column)), "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1.0, "green": .97, "blue": .8}}}, "fields": "userEnteredFormat.backgroundColor"}})
        status = head.index("STATUS" if tab == "Datasets" else "Status")
        for word, color in (("fail", (.98, .8, .8)), ("pass", (.82, .94, .82))):
            reqs.append({"addConditionalFormatRule": {"index": 0, "rule": {"ranges": [ds.column_range(sid, count, status)], "booleanRule": {
                "condition": {"type": "TEXT_STARTS_WITH" if tab == "Inputs" else "TEXT_CONTAINS", "values": [{"userEnteredValue": ("failing" if tab == "Datasets" and word == "fail" else "passing" if tab == "Datasets" else word)}]},
                "format": {"backgroundColor": {"red": color[0], "green": color[1], "blue": color[2]}}}}}})
    reqs += [{"repeatCell": {"range": {"sheetId": TAB_IDS["How to use"], "startRowIndex": 0, "endRowIndex": 1}, "cell": {"userEnteredFormat": {"textFormat": {"bold": True, "fontSize": 14}}}, "fields": "userEnteredFormat.textFormat"}},
             {"updateDimensionProperties": {"range": {"sheetId": TAB_IDS["How to use"], "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1}, "properties": {"pixelSize": 200}, "fields": "pixelSize"}},
             {"updateDimensionProperties": {"range": {"sheetId": TAB_IDS["How to use"], "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2}, "properties": {"pixelSize": 760}, "fields": "pixelSize"}},
             {"repeatCell": {"range": {"sheetId": TAB_IDS["How to use"], "startRowIndex": 1, "endRowIndex": 20}, "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP", "verticalAlignment": "TOP"}}, "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)"}}]
    return reqs


def gather(args) -> tuple[list[dict], dict]:
    datasets = repo_datasets()
    if args.poor_answers and (poor := poor_answer_dataset(args.poor_answers)):
        datasets.append(poor)
    for spec in args.context_dataset or []:
        label, _, folder = spec.partition("=")
        if folder and (ctx := context_dataset(label, folder)):
            datasets.append(ctx)
    datasets = [describe(d) for d in datasets]
    results = read_csv(COMPONENTS / "results.csv")
    sources = dict(results_list=results, results=latest_by(results, lambda r: (r["component"], r["impl"], r.get("cases_sha", ""), r.get("sample", ""))),
                   per_case=latest_by(read_csv(COMPONENTS / "per_case.csv"), lambda r: (r["component"], r["impl"], r["case_id"])),
                   log=read_output_log(args.output_dir), raw=read_raw(args.raw_dir))
    return datasets, sources


def read_old(sheets, sheet_id: str, existing: dict, tab: str) -> list[dict]:
    return ds.read_tab(sheets, sheet_id, tab) if tab in existing else []


def tab_requests(existing: dict) -> list[dict]:
    """Delete this script's tabs and add them again as the first three tabs, in ``TABS`` order, whatever the sheet holds before or after them."""
    reqs = [{"deleteSheet": {"sheetId": existing[t]}} for t in TABS if t in existing]
    return reqs + [{"addSheet": {"properties": {"title": t, "sheetId": TAB_IDS[t], "index": i}}} for i, t in enumerate(TABS)]


def export(args) -> dict:
    datasets, sources = gather(args)
    drows, irows = build_rows(datasets, sources, args.owner, args.run_command)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if args.dry_run:
        return dict(datasets=len(drows) - 1, inputs=len(irows) - 1)
    sheets, drive = ds.services(args.service_account, args.subject)
    meta = sheets.spreadsheets().get(spreadsheetId=args.sheet, fields="sheets.properties(sheetId,title)").execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    carried = {"Datasets": carry_over(read_old(sheets, args.sheet, existing, "Datasets"), drows, "Datasets"),
               "Inputs": carry_over(read_old(sheets, args.sheet, existing, "Inputs"), irows, "Inputs")}
    reqs = tab_requests(existing)
    sheets.spreadsheets().batchUpdate(spreadsheetId=args.sheet, body={"requests": reqs}).execute()
    sheets.spreadsheets().values().batchUpdate(spreadsheetId=args.sheet, body={"valueInputOption": "RAW", "data": [
        {"range": "Datasets!A1", "values": drows}, {"range": "Inputs!A1", "values": irows},
        {"range": "How to use!A1", "values": how_to_use(stamp)}]}).execute()
    sheets.spreadsheets().batchUpdate(spreadsheetId=args.sheet, body={"requests": requests_for({"Datasets": len(drows) - 1, "Inputs": len(irows) - 1})}).execute()
    after = sheets.spreadsheets().get(spreadsheetId=args.sheet, fields="sheets.properties(title,index)").execute()
    order = [s["properties"]["title"] for s in sorted(after["sheets"], key=lambda s: s["properties"]["index"])]
    return dict(spreadsheetId=args.sheet, datasets=len(drows) - 1, inputs=len(irows) - 1, carried=carried, tab_order=order)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sheet", required=True, help="the existing spreadsheet id (never created here)")
    parser.add_argument("--service-account", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--poor-answers", help="file, or folder holding poorly_answered_inputs.jsonl")
    parser.add_argument("--context-dataset", action="append", metavar="LABEL=FOLDER", help="a private context dataset folder (cases.jsonl): ids and status only")
    parser.add_argument("--output-dir", default="", help="the QAR_EVAL_OUTPUT_DIR directory")
    parser.add_argument("--raw-dir", default="", help="the chat runner's raw result files")
    parser.add_argument("--owner", default="", help="text for the Owner column of every dataset")
    parser.add_argument("--run-command", default="", help="the deployment's wrapper that runs a chat dataset (shown in the HOW TO RUN column)")
    parser.add_argument("--dry-run", action="store_true", help="build the rows and print the counts; no Google call")
    return parser


def main() -> None:
    print(json.dumps(export(make_parser().parse_args()), indent=1))


if __name__ == "__main__":
    main()
