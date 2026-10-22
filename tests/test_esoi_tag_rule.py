"""Rule Q1 (scripts/esoi_rules.py): solution tags come from esoi/tags.csv, each tag links the problems it mitigates,
and a solution's connected problems count as its tags in the lookup."""
import csv
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("qar_esoi_rules", ROOT / "scripts" / "esoi_rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)

PROBLEMS = {"esoi-r1-duplicate-solution", "esoi-r2-overlap-not-leveraged", "prob-spend"}
LISTED = [dict(tag="spending", meaning="Bounds spend.", problem_ids="esoi-r2-overlap-not-leveraged;prob-spend"),
          dict(tag="prompt", meaning="Decides prompt text.", problem_ids="esoi-r1-duplicate-solution")]


def solutions(*tag_cells):
    return [dict(solution_id=f"s{i}", tags=cell) for i, cell in enumerate(tag_cells)]


def test_a_list_used_by_some_solutions_and_not_all_passes():
    assert rules.tag_problems(LISTED, solutions("spending;prompt", "spending", ""), PROBLEMS) == []


def test_a_tag_outside_the_list_fails():
    lines = rules.tag_problems(LISTED, solutions("spending;prompt", "spending;prompt;cards", ""), PROBLEMS)
    assert any(line.startswith("Q1 s1: tag `cards` is not in esoi/tags.csv") for line in lines)


def test_unused_and_universal_tags_fail_and_name_the_procedure():
    lines = rules.tag_problems(LISTED, solutions("spending", "spending"), PROBLEMS)
    assert any("prompt: used by no solution" in line for line in lines)
    assert any("spending: on every solution" in line for line in lines)
    assert all("docs/spec-selection.md, Solution tags" in line for line in lines)


def test_a_tag_on_one_solution_is_a_note_not_a_failure():
    found = solutions("spending;prompt", "spending", "")
    assert rules.tag_problems(LISTED, found, PROBLEMS) == []
    assert rules.tag_notes(LISTED, found) == [
        "note: Q1 esoi/tags.csv prompt: used by one solution only (s0); check the back-fill (" + rules.TAG_PROCEDURE + ")"]


def test_a_tag_must_link_problems_that_exist():
    listed = LISTED + [dict(tag="memory", meaning="Keeps what was said.", problem_ids=""),
                       dict(tag="cards", meaning="Reads cards.", problem_ids="esoi-r3-co-change-missed")]
    lines = rules.tag_problems(listed, solutions("spending;prompt;memory;cards", "spending;memory;cards", ""), PROBLEMS)
    assert lines == [
        "Q1 esoi/tags.csv memory: links no problem, so it names no risk it mitigates (" + rules.TAG_PROCEDURE + ")",
        "Q1 esoi/tags.csv cards: linked problem `esoi-r3-co-change-missed` is not a problem record ("
        + rules.TAG_PROCEDURE + ")"]


def test_connected_problems_count_as_tags_in_the_lookup_without_being_copied():
    rows = [dict(solution_id="a", tags="spending", problem_ids="prob-spend"),
            dict(solution_id="b", tags="", problem_ids="prob-spend;prob-other"),
            dict(solution_id="c", tags="prompt", problem_ids="prob-other")]
    assert rules.tags_of(rows[0]) == ["spending", "prob-spend"]
    assert rules.solutions_sharing(["prob-spend"], rows) == ["a", "b"]
    assert rules.solutions_sharing(["prompt"], rows) == ["c"]
    assert rows[1]["tags"] == ""


def test_the_repository_list_and_tags_pass():
    with (ROOT / "esoi" / "tags.csv").open(newline="") as handle:
        listed = list(csv.DictReader(handle))
    with (ROOT / "esoi" / "solutions.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    with (ROOT / "esoi" / "problems.csv").open(newline="") as handle:
        problems = {row["problem_id"] for row in csv.DictReader(handle)}
    assert rules.tag_problems(listed, rows, problems) == []
