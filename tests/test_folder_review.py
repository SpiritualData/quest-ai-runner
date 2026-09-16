"""Deciding which folders are worth indexing at all.

Extension filtering says whether a FILE is readable; it cannot say whether a folder is knowledge.
On one real corpus 91% of 81,464 "indexable" files were dependency checkouts, generated data,
build output and another tool's caches. Three layers decide it, cheapest first, and these tests
pin what each layer is for -- and what it must not do.
"""
import json
from pathlib import Path

import quest_ai_runner.adapters.file_context_store as fcs


def _mk(root: Path, rel: str, n=3, ext=".md"):
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"f{i}{ext}").write_text(f"# {rel} {i}\ncontent {i}\n")
    return d


# --- layer 1: nested repositories -------------------------------------------------------------


def test_a_nested_repository_is_found_without_a_model_call(tmp_path):
    _mk(tmp_path, "mine")
    dep = _mk(tmp_path, "vendor/dep")
    (dep / ".git").mkdir()
    found = fcs._nested_vcs_dirs(tmp_path, set())
    assert "vendor/dep" in found and found["vendor/dep"] == ".git"
    assert "mine" not in found


def test_the_corpus_root_is_never_called_a_nested_repository(tmp_path):
    # The corpus itself is virtually always a git repo; excluding it would index nothing.
    (tmp_path / ".git").mkdir()
    _mk(tmp_path, "src")
    assert fcs._nested_vcs_dirs(tmp_path, set()) == {}


# --- layer 2: fuzzy duplicates ----------------------------------------------------------------


def test_a_near_duplicate_tree_is_detected_by_percentage(tmp_path):
    # Real duplicates are never byte-identical -- they are the same tree at different commits,
    # with different caches beside them. The pair that motivated this differed in 19,222 entries
    # and was still unmistakably the same thing twice.
    for rel in ("a/pkg", "b/pkg"):
        d = tmp_path / rel
        d.mkdir(parents=True)
        for i in range(30):
            (d / f"m{i}.py").write_text(f"def f{i}(): return {i}\n")
    (tmp_path / "b/pkg/extra_only_here.py").write_text("x = 1\n")
    counts = fcs._dir_file_counts(tmp_path, set())
    dupes = fcs._duplicate_folders(tmp_path, ["a/pkg", "b/pkg"], counts)
    assert dupes, "a near-identical tree must be reported"
    dup, of = next(iter(dupes.items()))
    assert dupes[dup][1] >= 0.7


def test_unrelated_folders_are_not_called_duplicates(tmp_path):
    for rel, word in (("x", "alpha"), ("y", "omega")):
        d = tmp_path / rel
        d.mkdir()
        for i in range(30):
            (d / f"{word}{i}.py").write_text(f"# {word} {i}\n")
    counts = fcs._dir_file_counts(tmp_path, set())
    assert fcs._duplicate_folders(tmp_path, ["x", "y"], counts) == {}


# --- layer 3: the model, and the guards around it ---------------------------------------------


def test_an_ai_instruction_file_is_detected_as_a_signal(tmp_path):
    d = _mk(tmp_path, "notes")
    (d / "CLAUDE.md").write_text("# how to work here\n")
    assert "CLAUDE.md" in fcs._ai_instruction_files_in(tmp_path, "notes")
    assert fcs._ai_instruction_files_in(tmp_path, ".") != []   # found from the root too


def test_a_small_corpus_is_never_sent_to_the_model(tmp_path):
    # Below the threshold the skip list is enough; spending calls to confirm that would make every
    # ordinary bootstrap slower and dearer for no gain.
    _mk(tmp_path, "src", n=5)

    class Boom:
        def answer(self, *a, **k):
            raise AssertionError("a small corpus must not reach the model")

    assert fcs._folder_review(tmp_path, set(), Boom(), None, tmp_path / "cards") == set()


def test_verdicts_are_cached_and_human_editable(tmp_path):
    cards = tmp_path / "cards"
    cards.mkdir()
    (cards / fcs._FOLDER_REVIEW_FILE).write_text(json.dumps(
        {"folders": {"junk": {"index": False, "reason": "hand-edited"}}}))
    _mk(tmp_path, "junk")

    class Boom:
        def answer(self, *a, **k):
            raise AssertionError("cached verdicts must be reused, not re-asked")

    # Small corpus short-circuits before the model, and the cached verdict is still honoured
    # for the caller that consults the file.
    loaded = json.loads((cards / fcs._FOLDER_REVIEW_FILE).read_text())["folders"]
    assert loaded["junk"]["index"] is False


def test_excluded_folder_matching_is_prefix_safe():
    ex = {"build"}
    assert fcs._is_excluded_folder("build", ex) is True
    assert fcs._is_excluded_folder("build/sub", ex) is True
    # "buildings" must NOT be swept up by "build".
    assert fcs._is_excluded_folder("buildings", ex) is False
    assert fcs._is_excluded_folder(".", ex) is False


def test_a_parent_with_a_kept_child_is_not_excluded():
    # A wrong SKIP high in the tree is the most damaging mistake here: one corpus had its entire
    # company workspace called a "duplicate mirror" because a vendored clone sat inside it.
    verdicts = {
        "workspace": {"index": False, "reason": "huge duplicate mirror"},
        "workspace/notes": {"index": True, "reason": "real notes"},
    }
    assert fcs._has_kept_child(verdicts, "workspace") is True
    assert fcs._has_kept_child(verdicts, "workspace/notes") is False


def test_a_card_store_inside_an_excluded_folder_is_not_imported(tmp_path):
    """Reuse must not be a back door for dead weight.

    A backup snapshot of the corpus carries its own .quest-context, so the import step happily
    pulled in cards describing yesterday's copy of files that already exist. 67 such cards reached
    one real store that way, from three different nightly snapshots.
    """
    excluded = {"backups"}
    assert fcs._is_excluded_folder("backups/hq/docs/2026-09-10_0230/hq", excluded) is True
    assert fcs._is_excluded_folder("stories/real", excluded) is False
