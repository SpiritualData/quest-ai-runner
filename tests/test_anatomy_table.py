"""docs/anatomy.md stays true: its table is generated from the ESOI records, its modules exist, and the README
carries the same diagram A."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("anatomy_table", ROOT / "scripts" / "anatomy_table.py")
anatomy_table = importlib.util.module_from_spec(spec)
spec.loader.exec_module(anatomy_table)


def first_diagram(path):
    return re.search(r"```mermaid\n(.*?)```", path.read_text(), re.S).group(1)


def test_table_is_not_stale():
    text = anatomy_table.DOC.read_text()
    assert anatomy_table.render(text) == text, "run: python scripts/anatomy_table.py --write"


def test_every_part_names_a_real_module():
    for part, module, solution_id in anatomy_table.PARTS:
        assert (ROOT / module).exists(), f"{part}: {module}"


def test_readme_diagram_matches_the_anatomy_page():
    assert first_diagram(ROOT / "README.md") == first_diagram(anatomy_table.DOC)
