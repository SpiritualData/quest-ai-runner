"""The generic search stages live in the ESOI library (esoi.spec_search), not here: QAR defines none of them again."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "quest_ai_runner"
LIBRARY_OWNED = {"fuse", "gate_decision", "tokens", "STOP", "narrow_lexical", "narrow_lexical_scored", "semantic_scored",
                 "parse_ids", "attach_links", "served_by_solution", "link_map", "RecordView", "TagTable"}


def defined_names(tree):
    names = set()
    for node in tree.body:   # module level: what a module offers, not a local variable
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def test_qar_defines_none_of_the_stages_the_library_owns():
    offenders = {}
    for path in PACKAGE.rglob("*.py"):
        found = defined_names(ast.parse(path.read_text(encoding="utf-8"))) & LIBRARY_OWNED
        if found:
            offenders[str(path.relative_to(PACKAGE))] = sorted(found)
    assert not offenders, f"define these in esoi.spec_search, not in QAR: {offenders}"


def test_the_library_has_them():
    from esoi import spec_index, spec_search
    assert callable(spec_search.fuse) and callable(spec_search.gate_decision) and callable(spec_index.tokens)
