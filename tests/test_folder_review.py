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


# --- the risk gate: one decision for every card source -----------------------------------------
#
# A machine's daily backup snapshots (one folder per date, each a copy of the whole tree) each carry
# their own card store. A corpus rooted above them must not walk, import or mount any of them, and
# must not depend on a model happening to be available to know that.

import logging
import re

import pytest

from quest_ai_runner.adapters import card_store_mounts as mounts
from quest_ai_runner.adapters import context_risk_gate as gate_mod
from quest_ai_runner.adapters.file_context_store import FileContextStore
from tests.test_card_store_mounts import card, make_owner, reader_store

DATES = ("2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04")


class ReviewProvider:
    """Answers the folder review prompt from a policy; anything else gets no topics."""

    def __init__(self, index_folders=()):
        self.index_folders = set(index_folders)
        self.review_calls = []

    def list_models(self):
        return []

    def answer(self, messages, model=None, **kw):
        text = messages[0]["content"]
        if "FOLDERS:" not in text:
            return "[]"
        self.review_calls.append(text)
        folders = re.findall(r'^- "([^"]+)"', text, re.M)
        return json.dumps([{"folder": f, "index": f in self.index_folders, "mixed": False,
                            "reason": "test verdict"} for f in folders])


def _series_corpus(root: Path, dates=DATES) -> Path:
    """A live tree plus ``backups/<date>/hq/docs`` copies, each carrying its own card store."""
    (root / "live").mkdir(parents=True, exist_ok=True)
    (root / "live" / "real.py").write_text("def real():\n    pass\n")
    for d in dates:
        snap = root / "backups" / d
        make_owner(snap, [card(f"snap-{d}", ["hq/docs/a.py", "hq/docs/b.py"])])
    return root / "backups"


def _store(root: Path, **kw) -> FileContextStore:
    return FileContextStore(str(root / "cards_elsewhere"), repo_root=str(root),
                            auto_bootstrap=False, **kw)


def _risk_warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "look risky" in r.getMessage()]


def test_serial_names_are_dates_timestamps_and_versions_only():
    for name in ("2026-09-10", "20260910_0230", "snapshot-2026-09-10T02-30", "1757471400", "v1.2", "3.0.1"):
        assert gate_mod.name_looks_serial(name), name
    for name in ("docs", "2026", "src", "notes-2026", "backup", "v2"):
        assert not gate_mod.name_looks_serial(name), name


def test_a_series_needs_three_dated_siblings_sharing_one_structure():
    shape = {"2026-01-01": ["hq"], "2026-01-02": ["hq"], "2026-01-03": ["hq"], "docs": ["a"]}
    assert gate_mod.find_series(sorted(shape), lambda n: shape[n]) == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert gate_mod.find_series(["2026-01-01", "2026-01-02"], lambda n: ["hq"]) == []
    different = {"2026-01-01": ["a"], "2026-01-02": ["b"], "2026-01-03": ["c"]}
    assert gate_mod.find_series(sorted(different), lambda n: different[n]) == []
    assert gate_mod.find_series(["2026-01-01", "2026-01-02", "2026-01-03"], lambda n: []) == []


def test_risk_signals_count_stores_but_one_nested_store_is_normal():
    assert gate_mod.risk_signals(["product"], {}, {}) == {}
    assert gate_mod.risk_signals(["a/one", "a/two"], {}, {}) == {}
    out = gate_mod.risk_signals(["bk/d1", "bk/d2", "bk/d3", "bk/d3/inner"], {}, {})
    assert list(out) == ["bk"] and out["bk"][0][0] == "stores" and "4 card stores" in out["bk"][0][1]
    # The corpus root is never a risk folder; its children are.
    out = gate_mod.risk_signals(["a/x", "b/x", "b/y", "b/z", "a/y", "a/z"], {}, {})
    assert sorted(out) == ["a", "b"]


def test_risk_signals_report_a_series_and_a_large_folder():
    out = gate_mod.risk_signals([], {"bk": ["2026-01-01", "2026-01-02", "2026-01-03"], "": ["x", "y", "z"]},
                                {"big/sub": 1500, "big/sub/deeper": 600, "small": 3})
    assert out["bk"][0][0] == "series" and "3 dated or numbered copies" in out["bk"][0][1]
    assert "" not in out
    assert out["big/sub"] == [("large", "very large, 2100 files")]


def test_review_unavailable_means_nothing_from_the_series_and_one_warning(tmp_path, caplog, monkeypatch):
    _series_corpus(tmp_path)
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING):
        store.bootstrap(root=str(tmp_path))          # no provider: the review cannot run
    assert store._load_all() == {}, "no card from any snapshot may be imported"
    warnings = _risk_warnings(caplog)
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert '"backups"' in msg and "4 dated or numbered copies" in msg and '"index": true' in msg
    assert "folder_review.json" in msg


def test_review_switched_off_is_the_same_as_unavailable(tmp_path, caplog, monkeypatch):
    monkeypatch.setenv("QAR_FOLDER_REVIEW", "0")
    _series_corpus(tmp_path)
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING):
        store.bootstrap(root=str(tmp_path), provider=ReviewProvider())
    assert store._load_all() == {}
    assert len(_risk_warnings(caplog)) == 1


def test_a_model_skip_is_cached_with_its_reason_and_source(tmp_path):
    _series_corpus(tmp_path)
    provider = ReviewProvider()
    store = _store(tmp_path)
    store.bootstrap(root=str(tmp_path), provider=provider)
    assert store._load_all() == {}
    assert any("[RISK:" in c and "dated or numbered copies" in c for c in provider.review_calls), \
        "the signal goes to the model as context"
    verdict = json.loads((tmp_path / "cards_elsewhere" / "folder_review.json").read_text())["folders"]["backups"]
    assert verdict["index"] is False and verdict["source"] == "risk_signal" and verdict["reason"]
    again = ReviewProvider()
    store.bootstrap(root=str(tmp_path), provider=again)
    assert again.review_calls == [], "a cached verdict is not asked again"


def test_a_model_index_verdict_is_honoured(tmp_path, caplog):
    _series_corpus(tmp_path)
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING):
        store.bootstrap(root=str(tmp_path), provider=ReviewProvider(index_folders={"backups"}))
    assert store._load_all(), "the snapshots' stores are imported when the verdict says index"
    assert _risk_warnings(caplog) == []


@pytest.mark.parametrize("review_on", [True, False])
def test_a_human_index_true_overrides_even_with_no_review(tmp_path, monkeypatch, review_on):
    if not review_on:
        monkeypatch.setenv("QAR_FOLDER_REVIEW", "0")
    _series_corpus(tmp_path)
    cards = tmp_path / "cards_elsewhere"
    cards.mkdir()
    (cards / "folder_review.json").write_text(json.dumps(
        {"folders": {"backups": {"index": True, "reason": "keep these", "pinned": True}}}))
    store = _store(tmp_path)
    store.bootstrap(root=str(tmp_path))
    assert store._load_all()


def test_one_legitimate_nested_store_is_unchanged(tmp_path, caplog):
    make_owner(tmp_path / "product", [card("prod-card", ["pkg/m.py", "pkg/n.py"])])
    (tmp_path / "top.py").write_text("def top():\n    pass\n")
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING):
        store.bootstrap(root=str(tmp_path))
    assert len(store._load_all()) == 1
    assert _risk_warnings(caplog) == []
    mount_reader = reader_store(tmp_path)
    mount_reader.bootstrap(root=str(tmp_path))
    assert len(mount_reader._mounts) == 1


def test_mount_mode_neither_claims_nor_mounts_a_blocked_series(tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_FOLDER_REVIEW", "0")
    _series_corpus(tmp_path)
    reader = reader_store(tmp_path)
    reader.bootstrap(root=str(tmp_path))
    assert reader._mounts == []
    cards = tmp_path / ".quest-context"
    cards.mkdir(exist_ok=True)
    (cards / "folder_review.json").write_text(json.dumps({"folders": {"backups": {"index": True}}}))
    allowed = reader_store(tmp_path)
    allowed.bootstrap(root=str(tmp_path))
    assert len(allowed._mounts) == len(DATES)


def test_discovery_does_not_enter_a_turned_away_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_FOLDER_REVIEW", "0")
    _series_corpus(tmp_path)
    cards = tmp_path / "cards_elsewhere"
    gate = gate_mod.folder_gate(tmp_path, set(), None, None, cards)
    assert "backups" in gate.blocked
    assert mounts.discover_nested_stores(tmp_path, cards, set(), gate) == []
    assert len(mounts.discover_nested_stores(tmp_path, cards, set())) == len(DATES)
    assert _store(tmp_path)._discover_nested_card_dirs(tmp_path, set(), gate) == []


def test_migrate_imports_report_names_the_blocked_folder_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_FOLDER_REVIEW", "0")
    _series_corpus(tmp_path)
    cards = make_owner(tmp_path, [card("own", ["live/real.py", "live/real2.py"])])
    before = sorted(p.name for p in cards.iterdir())
    plan = mounts.plan_migrate_imports(str(cards), str(tmp_path))
    assert [d for d, _ in plan.risk_blocked] == ["backups"]
    assert plan.mounts == [] and "RISK, NOT READ backups" in plan.render()
    assert sorted(p.name for p in cards.iterdir()) == before


def test_ancestor_import_drops_files_in_a_turned_away_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_FOLDER_REVIEW", "0")
    work = tmp_path / "hq" / "work"
    (work / "live.py").parent.mkdir(parents=True)
    (work / "live.py").write_text("def live():\n    pass\n")
    (work / "live2.py").write_text("def live2():\n    pass\n")
    for d in DATES:
        (work / "snaps" / d / "data").mkdir(parents=True)
        (work / "snaps" / d / "data" / "f.py").write_text("def f():\n    pass\n")
    make_owner(tmp_path / "hq", [
        card("mixed", ["work/live.py", "work/live2.py", "work/snaps/2026-01-01/data/f.py"]),
        card("only-snaps", ["work/snaps/2026-01-02/data/f.py", "work/snaps/2026-01-03/data/f.py"]),
    ], with_files=False)
    blocked = _store(work)
    blocked.bootstrap(root=str(work))
    got = {fe["path"] for c in blocked._load_all().values() for fe in c["files"]}
    assert got == {"live.py", "live2.py"}, "only the in-scope, non-snapshot files are covered"
    assert len(blocked._load_all()) == 1

    cards = work / "cards_elsewhere"
    (cards / "folder_review.json").parent.mkdir(exist_ok=True)
    (cards / "folder_review.json").write_text(json.dumps({"folders": {"snaps": {"index": True}}}))
    allowed = _store(work)
    allowed.bootstrap(root=str(work))
    assert len(allowed._load_all()) == 2


def test_an_ancestor_store_that_is_one_of_a_series_of_copies_is_not_imported(tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_FOLDER_REVIEW", "0")
    for d in DATES:
        snap = tmp_path / "snap" / d
        (snap / "work").mkdir(parents=True)
        (snap / "work" / "m.py").write_text("def m():\n    pass\n")
        make_owner(snap, [card(f"c-{d}", ["work/m.py", "work/n.py"])], with_files=False)
    walk_root = tmp_path / "snap" / DATES[1] / "work"
    (walk_root / "n.py").write_text("def n():\n    pass\n")
    store = FileContextStore(str(tmp_path / "c1"), repo_root=str(walk_root), auto_bootstrap=False)
    store.bootstrap(root=str(walk_root))
    assert store._load_all() == {}
    cards = tmp_path / "c2"
    cards.mkdir()
    (cards / "folder_review.json").write_text(json.dumps({"folders": {"../..": {"index": True}}}))
    store2 = FileContextStore(str(cards), repo_root=str(walk_root), auto_bootstrap=False)
    store2.bootstrap(root=str(walk_root))
    assert store2._load_all()


# --- a lone backup copy of the corpus itself ------------------------------------------------------


def _live_corpus(root: Path) -> None:
    for sub in ("docs", "src", "notes"):
        (root / sub).mkdir(parents=True, exist_ok=True)
        (root / sub / "a.md").write_text(f"# {sub}\nnotes\n")


def _snapshot(root: Path, name: str, *, recorded_root=None, layout=(), store_id=None):
    """One backup folder with a card store, optionally recording another root or the live layout."""
    from quest_ai_runner.adapters.card_store_identity import update_identity
    snap = root / "backups" / name
    make_owner(snap, [card(f"snap-{name}", ["hq/docs/a.py", "hq/docs/b.py"])])
    for sub in layout:
        (snap / sub).mkdir(exist_ok=True)
        (snap / sub / "a.md").write_text("# copy\n")
    fields = {}
    if recorded_root is not None:
        fields["root"] = str(recorded_root)
    if store_id is not None:
        fields["store_id"] = store_id
    if fields:
        assert update_identity(str(snap / ".quest-context"), **fields)
    return snap


def test_serial_names_take_word_prefixes_and_time_suffixes():
    for name in ("hq-docs-2026-09-30-0230", "hq_backup_20260930", "hq.docs.2026.09.30", "snap-2.1.0-rc1"):
        assert gate_mod.name_looks_serial(name), name
    for name in ("hq-docs", "backup_2026", "release-2", "x" * 90 + "-2026-09-30"):
        assert not gate_mod.name_looks_serial(name), name


def test_risk_signals_report_a_copy_of_the_corpus():
    out = gate_mod.risk_signals(["bk/one"], {}, {}, {"bk/one": "its recorded root is this corpus's root"})
    assert out["bk/one"][0][0] == "copy" and out["bk/one"][0][0] in gate_mod.HARD_RISK_KINDS


@pytest.mark.parametrize("how", ["recorded_root", "layout", "own_store_id"])
def test_a_lone_backup_copy_of_the_corpus_is_neither_indexed_nor_imported(tmp_path, caplog, how):
    _live_corpus(tmp_path)
    own_cards = tmp_path / "cards_elsewhere"
    if how == "own_store_id":
        from quest_ai_runner.adapters.card_store_identity import write_meta
        own_cards.mkdir()
        write_meta(str(own_cards), {"store_id": "st_live"})
    kwargs = {"recorded_root": tmp_path} if how == "recorded_root" else \
        {"layout": ("docs", "src", "notes")} if how == "layout" else {"store_id": "st_live"}
    _snapshot(tmp_path, "hq_backup_20260930", **kwargs)
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING):
        store.bootstrap(root=str(tmp_path))         # no provider: the review cannot run
    assert not any(i.startswith("snap") for i in store._load_all()), "nothing from the copy is imported"
    assert len(_risk_warnings(caplog)) == 1
    assert "hq_backup_20260930" in _risk_warnings(caplog)[0].getMessage()
    gate = gate_mod.folder_gate(tmp_path, set(), None, None, own_cards)
    assert gate.excludes_dir(tmp_path / "backups" / "hq_backup_20260930")
    assert not gate.excludes_dir(tmp_path / "docs")


def test_a_lone_backup_copy_is_open_when_a_person_says_index(tmp_path):
    _live_corpus(tmp_path)
    _snapshot(tmp_path, "hq_backup_20260930", recorded_root=tmp_path)
    cards = tmp_path / "cards_elsewhere"
    cards.mkdir()
    (cards / "folder_review.json").write_text(json.dumps(
        {"folders": {"backups/hq_backup_20260930": {"index": True}}}))
    gate = gate_mod.folder_gate(tmp_path, set(), None, None, cards)
    assert not gate.excludes_dir(tmp_path / "backups" / "hq_backup_20260930")


def test_a_nested_store_that_is_not_a_copy_stays_imported(tmp_path, caplog):
    """One shared folder name is a coincidence, and a store recording its own root is not a copy."""
    _live_corpus(tmp_path)
    (tmp_path / "product").mkdir()
    make_owner(tmp_path / "product", [card("prod-card", ["pkg/m.py", "pkg/n.py"])])
    (tmp_path / "product" / "docs").mkdir()             # one of the root's names, not most of them
    (tmp_path / "product" / "docs" / "x.md").write_text("# x\n")
    store = _store(tmp_path)
    with caplog.at_level(logging.WARNING):
        store.bootstrap(root=str(tmp_path))
    assert "prod-card" in " ".join(store._load_all())
    assert _risk_warnings(caplog) == []
    gate = gate_mod.folder_gate(tmp_path, set(), None, None, tmp_path / "cards_elsewhere")
    assert not gate.excludes_dir(tmp_path / "product")
