"""The records in esoi/*.csv are the only copy of the top intent: no readable restatement sits beside them to drift, and
every problem of the top intent is elaborated by detailed problems that name it as their parent."""
import csv
import re
from pathlib import Path

STRUCTURE = Path(__file__).resolve().parent.parent / "esoi"

RESTATEMENTS = ("qar_solution.md", "structure_network.md", "principles.csv", "STANDARD.md")


def records(name, keep=lambda row: True):
    with open(STRUCTURE / name, newline="") as fh:
        return [row for row in csv.DictReader(fh) if keep(row)]


def test_no_restatement_of_the_records_or_the_skill_sits_beside_them():
    assert [name for name in RESTATEMENTS if (STRUCTURE / name).exists()] == []


def test_every_top_problem_is_elaborated_by_detailed_problems():
    top = [row["problem_id"] for row in records("problems.csv", lambda row: re.fullmatch(r"QP\d+", row["problem_id"]))]
    assert top, "the top intent has problems"
    elaborated = {row["parent_problem_ids"] for row in records("problems.csv") if row["parent_problem_ids"]}
    assert sorted(set(top) - elaborated) == []
