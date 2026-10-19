"""qar_solution.md (the readable top intent) says what the top intent's records in docs/structure/*.csv (the ones esoi checks) say."""
import csv
import re
from pathlib import Path

STRUCTURE = Path(__file__).resolve().parent.parent / "docs" / "structure"
TEXT = (STRUCTURE / "qar_solution.md").read_text()


def records(name, keep=lambda row: True):
    with open(STRUCTURE / name, newline="") as fh:
        return [row for row in csv.DictReader(fh) if keep(row)]


def bullets(item_id):
    """The bullets of the markdown item with this id, by their bold label."""
    block = re.search(rf"^### {item_id} .*?\n(.*?)(?=^### |^## |^[^-#\n])", TEXT, re.M | re.S).group(1)
    return {label.rstrip(":"): value for label, value in re.findall(r"^- \*\*([^*]+?)\*\* (.*)$", block, re.M)}


def test_problems_and_ideals_match_the_readable_version():
    problem_labels = {"problem": "What is the problem?", "who_has_it": "Who or what type of person has the problem?",
                      "why_it_matters": "Why does this problem matter?", "why_hard": "Why is the problem hard to resolve?",
                      "existing_attempts": "What solutions or workarounds already attempt to address the problem?"}
    for row in records("problems.csv", lambda row: re.fullmatch(r"QP\d+", row["problem_id"])):
        shown = bullets(row["problem_id"])
        assert {key: shown[label] for key, label in problem_labels.items()} == {key: row[key] for key in problem_labels}
    ideal_labels = {"name": "Name", "principle": "Principle", "state_description": "State Description",
                    "hints_of_measurement": "Hints of Measurement"}
    for row in records("ideals.csv", lambda row: re.fullmatch(r"QI\d+", row["ideal_id"])):
        shown = bullets(row["ideal_id"])
        assert {key: shown[label] for key, label in ideal_labels.items()} == {key: row[key] for key in ideal_labels}
        assert shown["Opposes"].replace(", ", ";") == row["problem_ids"]


def test_solution_alternatives_and_metrics_match_the_readable_version():
    row = records("solutions.csv", lambda row: row["solution_id"] == "QS1")[0]
    shown = bullets(row["solution_id"])
    assert shown["Name"] == row["name"] and shown["Scoped Purpose"] == row["scoped_purpose"]
    assert shown["Goals"] == row["goals"] and shown["Measures of Success"] == row["measures_of_success"]
    for row in records("implementations.csv", lambda row: row["solution_id"] == "QS1"):
        shown = bullets(row["implementation_id"].split(":")[1])
        assert shown["Description"] == row["description"] and shown["Disadvantages"] == row["disadvantages"]
    for row in records("metrics.csv", lambda row: row["solution_ids"] == "QS1"):
        assert bullets(row["metric_id"])["Name"] == f"{row['name']}: {row['scoped_purpose']}"


def test_the_detailed_records_table_matches_the_parent_links():
    table = dict(re.findall(r"^\| (QP\d) \| (.*) \|$", TEXT, re.M))
    for parent, listed in table.items():
        with open(STRUCTURE / "problems.csv", newline="") as fh:
            linked = [r["problem_id"] for r in csv.DictReader(fh) if r["parent_problem_ids"] == parent]
        assert sorted(linked) == sorted(listed.split(", "))
