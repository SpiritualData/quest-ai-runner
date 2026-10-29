"""Generate the parts table in docs/anatomy.md from the ESOI records.

    python scripts/anatomy_table.py          # print the table
    python scripts/anatomy_table.py --write  # rewrite it in place between the markers

tests/test_anatomy_table.py fails when the file is stale, so the table never drifts.
"""
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "anatomy.md"
BEGIN, END = "<!-- anatomy-table:begin -->", "<!-- anatomy-table:end -->"

# part -> (module, solution id). The module is the entry file; the solution id keys the records.
PARTS = [
    ("World (input)", "quest_ai_runner/runner/poller.py", "run-queued-tasks"),
    ("Subconscious, incl. Intuition", "quest_ai_runner/core/card_filter.py", "admit-to-the-mind"),
    ("Mind", "quest_ai_runner/core/orchestrator.py", "keep-a-working-stream"),
    ("Science", "quest_ai_runner/adapters/web_research.py", "know-the-world"),
    ("Critical thinking", "quest_ai_runner/core/spec_slice.py", "think-critically"),
    ("Overseer", "quest_ai_runner/core/overseer.py", "watch-a-run"),
    ("Body", "quest_ai_runner/runner/quest_client.py", "act-as-the-body"),
    ("World (act)", "quest_ai_runner/adapters/acp_deep_runner.py", "act-in-the-world"),
    ("Higher mind", "quest_ai_runner/runner/personas.py", "hold-the-long-view"),
    ("Rep selection", "quest_ai_runner/runner/personas.py", "select-a-rep"),
]


def rows(name):
    with open(ROOT / "esoi" / name, newline="") as handle:
        return list(csv.DictReader(handle))


def table():
    solutions = {row["solution_id"]: row for row in rows("solutions.csv")}
    metrics = {row["metric_id"]: row for row in rows("metrics.csv")}
    lines = ["| Part | QAR component | Solution | Metric id | Status |", "|---|---|---|---|---|"]
    for part, module, solution_id in PARTS:
        solution = solutions[solution_id]
        metric_id = solution["metric_ids"].split(";")[0]
        has_arm = bool(metrics[metric_id]["command"].strip())
        today = Path(solution["selected"]).stem
        status = ("arm built" if has_arm else "spec only, no arm yet") + f", selected: {today}"
        lines.append(f"| {part} | `{module}` | `{solution_id}` | `{metric_id}` | {status} |")
    return "\n".join(lines)


def render(text):
    head, rest = text.split(BEGIN, 1)
    tail = rest.split(END, 1)[1]
    return f"{head}{BEGIN}\n{table()}\n{END}{tail}"


if __name__ == "__main__":
    if "--write" in sys.argv:
        DOC.write_text(render(DOC.read_text()))
    else:
        print(table())
