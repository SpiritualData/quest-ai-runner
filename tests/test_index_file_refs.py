"""Cards name their files by NUMBER, not by copying the path back.

Asking the model to echo each path verbatim spends generated tokens re-emitting strings the caller
already holds: stage 1 alone measured ~32,550 output tokens of nothing but paths, and stage 2 pays
it again for every card it proposes, including the 52% that dedup discards. An index costs two or
three tokens where a path costs fifteen, and the grouping decision is identical.
"""
from quest_ai_runner.adapters.file_context_store import _numbered_tree, _resolve_file_refs

ORDERED = ["src/alpha.py", "src/beta.py", "docs/guide.md"]
ALLOWED = set(ORDERED)


def test_the_file_list_is_presented_numbered():
    assert _numbered_tree(ORDERED).splitlines() == [
        "1. src/alpha.py", "2. src/beta.py", "3. docs/guide.md"]


def test_numbers_resolve_to_paths():
    assert _resolve_file_refs([1, 3], ORDERED, ALLOWED) == ["src/alpha.py", "docs/guide.md"]
    assert _resolve_file_refs(["2"], ORDERED, ALLOWED) == ["src/beta.py"]


def test_a_model_that_answers_with_paths_anyway_still_works():
    # No reason to discard an otherwise good card over the form of one field.
    assert _resolve_file_refs(["src/beta.py"], ORDERED, ALLOWED) == ["src/beta.py"]


def test_out_of_range_and_unknown_refs_are_dropped_not_invented():
    assert _resolve_file_refs([99, 0, -1], ORDERED, ALLOWED) == []
    assert _resolve_file_refs(["nope/x.py"], ORDERED, ALLOWED) == []
    assert _resolve_file_refs("not-a-list", ORDERED, ALLOWED) == []
    assert _resolve_file_refs([True], ORDERED, ALLOWED) == []      # bool is not an index


def test_duplicates_collapse():
    assert _resolve_file_refs([1, 1, "src/alpha.py"], ORDERED, ALLOWED) == ["src/alpha.py"]
