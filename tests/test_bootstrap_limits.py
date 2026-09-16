"""A ceiling nobody is told about is indistinguishable from a bug.

Bootstrap used to stop at a hardcoded 10,000 files and 5,000 cards, SILENTLY. A corpus of 81,455
indexable files was walked until the 10,000th and the run reported success, so the store looked
complete and the missing seven-eighths were never revisited. These tests pin the two honest
behaviours that replaced it: no limit unless a deployment asks for one, and a limit that says so
when it bites.
"""
import quest_ai_runner.adapters.file_context_store as fcs
from quest_ai_runner.adapters.file_context_store import FileContextStore


def test_no_limit_by_default(monkeypatch):
    monkeypatch.delenv("QAR_BOOTSTRAP_MAX_FILES", raising=False)
    monkeypatch.delenv("QAR_BOOTSTRAP_MAX_CARDS", raising=False)
    assert fcs._bootstrap_max_files() is None
    assert fcs._bootstrap_max_cards() is None


def test_a_limit_is_honoured_when_asked_for(monkeypatch):
    monkeypatch.setenv("QAR_BOOTSTRAP_MAX_FILES", "250")
    monkeypatch.setenv("QAR_BOOTSTRAP_MAX_CARDS", "10")
    assert fcs._bootstrap_max_files() == 250
    assert fcs._bootstrap_max_cards() == 10


def test_a_nonsense_or_zero_limit_means_no_limit_not_zero(monkeypatch):
    # A 0 read as a limit would index nothing at all while looking configured.
    for bad in ("junk", "0", "-3", ""):
        monkeypatch.setenv("QAR_BOOTSTRAP_MAX_FILES", bad)
        assert fcs._bootstrap_max_files() is None


def test_a_truncated_walk_warns_loudly(tmp_path, monkeypatch, caplog):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for i in range(12):
        (corpus / f"f{i}.md").write_text(f"# file {i}\n")
    monkeypatch.setenv("QAR_BOOTSTRAP_MAX_FILES", "4")
    store = FileContextStore(cards_dir=str(tmp_path / "cards"),
                             repo_root=str(corpus), auto_bootstrap=False)
    with caplog.at_level("WARNING"):
        store.bootstrap(root=str(corpus), provider=None)
    assert any("INCOMPLETE" in r.message or "INCOMPLETE" in str(r.msg) for r in caplog.records), \
        "a truncated walk must say so"


def test_counting_indexable_files_matches_the_walk_rules(tmp_path):
    from quest_ai_runner.cli import _count_indexable_files
    corpus = tmp_path / "c"
    (corpus / "node_modules" / "pkg").mkdir(parents=True)
    (corpus / "node_modules" / "pkg" / "index.js").write_text("x")   # skipped dir
    (corpus / "real.md").write_text("# real")
    (corpus / "notes.odt").write_text("binary-ish")                   # not an indexable ext
    assert _count_indexable_files(str(corpus)) == 1
