"""The three parts of work quality, scored on saved overlay replay records with no new chat turn (metrics ``metric-work-classification``,
``metric-work-esoi-application`` and ``metric-work-harness`` of ``esoi-application``), and every failing judged item attributed to one part.

1. Classification: the deliverable was filed under a solution of the kind the case expects (picked among the quest's records or created), read from the
   turn's recorded writes and the queued task's brief. Pass = the solution's name holds one of the case's ``expected_solution_terms``.
2. ESOI application: the deliverable met its solution's own rubric, measured by code before and after. Pass = a measured increment whose after is above
   its before (in the turn, or on the queued task's completion) and the judge's DELIVERABLE RUBRIC items all pass.
3. Harness: the work ran to a deliverable within its budget on the harness it was given. Pass = the queued task (or the in-turn writer when nothing was
   queued) produced a deliverable: a kept or attempted body, or kept parts, with no run error.

Weights are fixed before scoring: each part is the share of runs that pass it (every run weighs the same); an item's part comes from the table
``ITEM_PARTS`` (by the start of the case's own pass-criteria wording), with the exceptions said in ``part_of``. Run:
    python work_parts.py --records DIR [--records DIR ...] --dataset DATASET_DIR
"""
import argparse
import json
from pathlib import Path

#: Judged item (by the start of its wording) -> the part it measures. Fixed before scoring.
ITEM_PARTS = (("Works out what to do", 1), ("The cue is an encouragement to act", 1),
              ("A goal whose title starts", 2), ("The goal holds the deliverable", 2), ("No invented", 2), ("Repeat run", 2), ("STATE CHECK", 2),
              ("Changes state", 2), ("No duplicate", 2), ("Names the actor", 2), ("VALUE GATE", 2), ("The increment is measured", 2),
              ("The deliverable is kept", 2), ("A human ask", 2), ("When the turn queues", 2), ("DELIVERABLE RUBRIC", 2), ("When the metric", 2),
              ("Reply states", 2), ("An honest", 2))


def rows_of(folder: Path, last: int = 0):
    """The replay records of ``folder`` (one JSON-lines file per case), the last ``last`` of each case when given (one round's runs)."""
    for path in sorted(folder.glob("*.jsonl")):
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line) if line.strip() else None
            except ValueError:
                continue
            if row and row.get("case_id") and row.get("judge_input"):
                rows.append(row)
        yield from (rows[-last:] if last else rows)


def solution_named(row) -> str:
    """The deliverable's solution the run filed under: a created solution record (its recorded insert), else the solution the queued brief names."""
    for w in row.get("attempted_writes") or []:
        if "intent_records" in str(w.get("collection") or "") and w.get("method") == "insert_one" and '"type": "solution"' in str(w.get("filter") or ""):
            text = str(w.get("filter") or "")
            at = text.find('"name": "')
            if at >= 0:
                return text[at + 9:].split('"', 1)[0]
    for w in row.get("attempted_writes") or []:
        brief = str((w.get("document") or {}).get("text") or "")
        if brief.startswith(("Carry out the", "Make the deliverable of one solution")) and "The solution: " in brief:
            return brief.split("The solution: ", 1)[1].split(" (record", 1)[0]
    return ""


def measured_of(row):
    """The measured increment of the run: the queued task's completion measure when there is one, else the turn's own (value gate)."""
    for task in row.get("queued_tasks") or []:
        m = task.get("measured") or {}
        if m.get("increment"):
            return m, task
    gate = row.get("value_gate") or {}
    return ({"increment": gate.get("increment"), "after": (gate.get("increment") or {}).get("after"), "before": (gate.get("increment") or {}).get("before")}
            if gate.get("increment") else {}), None


def parts_of_run(row, case):
    verdict = row.get("verdict") or {}
    failed = [f.get("item", "") for f in verdict.get("failed") or []]
    terms = [t.lower() for t in case.get("expected_solution_terms") or []]
    name = solution_named(row)
    p1 = bool(terms) and any(t in name.lower() for t in terms)
    measured, task = measured_of(row)
    rubric_failed = any(i.startswith("DELIVERABLE RUBRIC") for i in failed)
    p2 = bool(measured) and float(measured.get("after") or 0) > float(measured.get("before") or 0) and not rubric_failed
    if task is not None:
        p3 = not task.get("error") and bool((task.get("measured") or {}).get("body") or task.get("attempted_keep"))
    else:
        inc = (row.get("value_gate") or {}).get("increment") or {}
        depth = inc.get("depth") or {}
        p3 = bool(inc) and depth.get("stopped") != "budget"
    produced = p3
    return {"solution": name, "p1": p1, "p2": p2, "p3": p3, "failed": [(i, part_of(i, produced, row)) for i in failed]}


def part_of(item: str, produced: bool, row) -> int:
    """The part a failing item belongs to: by ``ITEM_PARTS``, except that a deliverable item fails on the harness (3) when the work produced no
    deliverable, and the act-on-the-cue item fails on classification (1) only when no progress step ran at all (no increment recorded)."""
    part = next((p for start, p in ITEM_PARTS if item.startswith(start)), 2)
    if part == 2 and not produced and (item.startswith(("The goal holds the deliverable", "DELIVERABLE RUBRIC", "VALUE GATE"))):
        return 3
    if item.startswith("The cue is an encouragement to act") and (row.get("value_gate") or {}).get("increment"):
        return 2 if produced else 3
    return part


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", action="append", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--last", type=int, default=3, help="the last N records of each case in each folder (one round's runs); 0 for all")
    args = parser.parse_args(argv)
    cases = {}
    for line in (Path(args.dataset) / "poorly_answered_inputs.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            c = json.loads(line)
            cases[c["case_id"]] = c
    totals = {1: [0, 0], 2: [0, 0], 3: [0, 0]}
    blame = {1: 0, 2: 0, 3: 0}
    for folder in args.records:
        for row in rows_of(Path(folder), args.last):
            case = cases.get(row["case_id"]) or {}
            if not case.get("expected_solution_terms"):
                continue
            got = parts_of_run(row, case)
            for part, key in ((1, "p1"), (2, "p2"), (3, "p3")):
                totals[part][0] += int(got[key])
                totals[part][1] += 1
            for _, part in got["failed"]:
                blame[part] += 1
            print(f"{Path(folder).name:8} {row['case_id']:5} {str(row.get('timestamp'))[:16]} score {row.get('score')} | 1 {int(got['p1'])} 2 {int(got['p2'])} 3 {int(got['p3'])}"
                  f" | solution {got['solution'][:40]!r} | failing items by part " + json.dumps({p: sum(1 for _, q in got['failed'] if q == p) for p in (1, 2, 3)}))
    print("part pass rates: " + ", ".join(f"{p}: {v[0]}/{v[1]}" for p, v in totals.items()) + " | failing items by part: " + json.dumps(blame))


if __name__ == "__main__":
    main()
