"""Generate the parts table in docs/explanation/anatomy.md from the ESOI records.

    python scripts/anatomy_table.py          # print the table
    python scripts/anatomy_table.py --write  # rewrite it in place between the markers

tests/test_anatomy_table.py fails when the file is stale, so the table never drifts.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "explanation" / "anatomy.md"
BEGIN, END = "<!-- anatomy-table:begin -->", "<!-- anatomy-table:end -->"

# part -> (module, solution id). The module is the entry file; the solution id keys the records.
PARTS = [
    ("World (input)", "quest_ai_runner/the_consciousness/request_intake/poller.py", "quest-requests"),
    ("Subconscious, incl. Intuition", "quest_ai_runner/the_intelligence/subconscious/context_retriever/card_filter.py", "the-subconscious"),
    ("Mind", "quest_ai_runner/the_intelligence/mind/orchestrator.py", "the-mind"),
    ("Science", "quest_ai_runner/the_world/tools/web_search/web_research.py", "the-science"),
    ("Critical thinking", "quest_ai_runner/the_intelligence/critical_thinking/metric_translation/goal_metrics.py", "critical-thinking"),
    ("Overseer", "quest_ai_runner/the_intelligence/mind/delegation/overseer.py", "nervous-system"),
    ("Body", "quest_ai_runner/the_body/internal_context/quest/client/quest_client.py", "quest-account-truth"),
    ("World (act)", "quest_ai_runner/the_world/tools/delegated_agent_sessions/acp_deep_runner.py", "the-tools"),
    ("Higher mind", "quest_ai_runner/the_consciousness/representatives/personas.py", "quest-intents"),
    ("Rep selection", "quest_ai_runner/the_consciousness/representatives/personas.py", "quest-representatives"),
]


def rows(name):
    """The rows of one record file across the whole tree of records (the top intent's folder and the folders beside the code)."""
    from esoi import intents
    return intents.read_tree(ROOT / "esoi", name)


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
