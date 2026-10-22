"""Rule Q1 (scripts/esoi_rules.py): solution tags come from esoi/tags.csv and the list cannot rot."""
import csv
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("qar_esoi_rules", ROOT / "scripts" / "esoi_rules.py")
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)

LISTED = [dict(tag="spending", meaning="Bounds spend.", risk="Spend grows unseen."),
          dict(tag="prompt", meaning="Decides prompt text.", risk="A second prompt path grows the prompt.")]


def solutions(*tag_cells):
    return [dict(solution_id=f"s{i}", tags=cell) for i, cell in enumerate(tag_cells)]


def test_a_list_used_by_two_solutions_and_not_all_passes():
    assert rules.tag_problems(LISTED, solutions("spending;prompt", "spending;prompt", "")) == []


def test_a_tag_outside_the_list_fails():
    lines = rules.tag_problems(LISTED, solutions("spending;prompt", "spending;prompt;cards", ""))
    assert any(line.startswith("Q1 s1: tag `cards` is not in esoi/tags.csv") for line in lines)


def test_unused_single_and_universal_tags_fail_and_name_the_procedure():
    lines = rules.tag_problems(LISTED, solutions("spending", "spending"))
    assert any("prompt: used by no solution" in line for line in lines)
    assert any("spending: on every solution" in line for line in lines)
    lines = rules.tag_problems(LISTED, solutions("spending;prompt", "spending", ""))
    assert any("prompt: used by one solution only (s0)" in line for line in lines)
    assert all("docs/spec-selection.md, Solution tags" in line for line in lines)


def test_a_tag_without_a_risk_fails():
    listed = LISTED + [dict(tag="memory", meaning="Keeps what was said.", risk="")]
    lines = rules.tag_problems(listed, solutions("spending;prompt;memory", "spending;prompt;memory", ""))
    assert lines == ["Q1 esoi/tags.csv memory: no risk stated (" + rules.TAG_PROCEDURE + ")"]


def test_the_repository_list_and_tags_pass():
    with (ROOT / "esoi" / "tags.csv").open(newline="") as handle:
        listed = list(csv.DictReader(handle))
    with (ROOT / "esoi" / "solutions.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rules.tag_problems(listed, rows) == []
