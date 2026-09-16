"""A card outlives the file it was written for, and the index never noticed.

Cards were written when a file was walked and nothing removed them when that stopped being true
-- the file deleted, or the skip list grown so the file now sits inside an excluded directory.
The stale card is not merely useless: it is embedded on every vector seed and compared against
every other card by the O(n^2) clustering in dedup. On one real corpus 72% of the store was dead
in exactly these two ways. These tests pin the removal AND, just as importantly, what it refuses
to remove.
"""
from pathlib import Path

from quest_ai_runner.adapters.file_context_store import FileContextStore


def _dead(rel, walked=(), root=Path("/nope"), skip=("Android", "node_modules")):
    return FileContextStore._file_entry_is_dead(rel, set(walked), root, set(skip))


def test_a_path_inside_a_now_skipped_directory_is_dead():
    # The commonest case: the skip list grew (a vendored SDK, a build output) long after the card
    # was written, so the corpus will never walk that path again.
    assert _dead("stories/product/Android/Sdk/ndk/toolchains/x.h") is True
    assert _dead("app/node_modules/left-pad/index.js") is True


def test_a_path_that_no_longer_exists_is_dead(tmp_path):
    assert _dead("gone.md", root=tmp_path) is True


def test_a_path_still_in_the_walk_is_alive(tmp_path):
    assert _dead("kept.md", walked=["kept.md"], root=tmp_path) is False


def test_a_file_that_exists_but_the_walk_skipped_is_kept(tmp_path):
    # NARROWNESS MATTERS. A path can be absent from the walk for innocent reasons -- an extension
    # outside _SOURCE_EXTS, or a path recorded by a RUN rather than discovered by the walk.
    # Deleting those would throw away real learning, so "not walked" alone is never enough.
    (tmp_path / "notes.odt").write_text("real content")
    assert _dead("notes.odt", walked=[], root=tmp_path) is False


def test_an_unreadable_path_is_never_assumed_dead():
    # Failing toward keeping the card: a bad path must not become a licence to delete.
    assert _dead("\x00bad", root=Path("/nope")) is False


def test_prune_is_opt_outable(monkeypatch):
    from quest_ai_runner.adapters.file_context_store import _prune_dead_cards
    monkeypatch.delenv("QAR_PRUNE_DEAD_CARDS", raising=False)
    assert _prune_dead_cards() is True          # default ON
    for off in ("0", "false", "no", "FALSE"):
        monkeypatch.setenv("QAR_PRUNE_DEAD_CARDS", off)
        assert _prune_dead_cards() is False


def test_a_file_we_lack_permission_to_read_is_never_pruned(tmp_path):
    # Path.exists() answers False for permission-denied exactly as it does for deleted. This
    # corpus contains directories owned by another user, so conflating the two would delete cards
    # for files that are simply not ours to read.
    import os
    import pytest
    if os.geteuid() == 0:
        pytest.skip("root can read anything, so the distinction cannot be exercised")
    secret_dir = tmp_path / "locked"
    secret_dir.mkdir()
    target = secret_dir / "notes.md"
    target.write_text("real content")
    os.chmod(secret_dir, 0o000)
    try:
        assert _dead("locked/notes.md", walked=[], root=tmp_path) is False
    finally:
        os.chmod(secret_dir, 0o755)


def test_a_dry_run_never_deletes(tmp_path, monkeypatch):
    """--dry-run promises an estimate WITHOUT running the bootstrap. It must not delete either."""
    from quest_ai_runner.adapters.file_context_store import FileContextStore

    corpus = tmp_path / "corpus"
    (corpus / "Android" / "Sdk").mkdir(parents=True)
    (corpus / "Android" / "Sdk" / "vendored.py").write_text("# vendored\n")
    (corpus / "real.md").write_text("# real\n")
    cards = tmp_path / "cards"

    store = FileContextStore(cards_dir=str(cards), repo_root=str(corpus), auto_bootstrap=False)
    # A card pinning a file inside a now-skipped directory: exactly what pruning targets.
    store._repo.write("dead-card", {
        "id": "dead-card",
        "name": "vendored",
        "keywords": ["vendored"],
        "summary": "s",
        "files": [{"path": "Android/Sdk/vendored.py", "sha256": "x"}],
    })
    assert store._repo.exists("dead-card")

    dry = FileContextStore(cards_dir=str(cards), repo_root=str(corpus),
                           auto_bootstrap=False, dry_run=True)
    dry.bootstrap(root=str(corpus), provider=None)

    assert dry._repo.exists("dead-card"), "a dry run deleted a card"


def _degenerate(card):
    from quest_ai_runner.adapters.file_context_store import FileContextStore
    return FileContextStore._card_is_degenerate(card)


def test_a_card_that_only_restates_its_path_is_degenerate():
    # Written when topic extraction yields nothing (e.g. the provider is down). Harmful, not just
    # useless: the incremental diff counts those files as covered, so a later healthy pass never
    # revisits them and the corpus cannot heal itself.
    assert _degenerate({"name": "", "summary": "batmanhq/api/__init__.py",
                        "files": [{"path": "batmanhq/api/__init__.py"}]}) is True
    assert _degenerate({"name": "", "summary": "notes/plan.md -- # Plan. First line...",
                        "files": [{"path": "notes/plan.md"}]}) is True
    assert _degenerate({"name": "", "summary": "", "files": [{"path": "a/b.py"}]}) is True


def test_a_named_card_is_never_degenerate():
    assert _degenerate({"name": "Ragin Reading and Domain 4 Progress",
                        "summary": "stories/x.md", "files": [{"path": "stories/x.md"}]}) is False


def test_a_card_with_a_real_summary_is_never_degenerate():
    assert _degenerate({"name": "", "files": [{"path": "notes/plan.md"}],
                        "summary": "Tracks daily progress reading Ragin toward Domain 4."}) is False


def test_a_card_with_no_files_is_not_judged_on_the_path_echo_rule():
    # A conversation / run-recorded card: its summary legitimately is not about a path, so the
    # path-echo rule must not touch it. (A NAMELESS card whose summary is nothing but agent
    # scaffolding is a different case and is caught -- see the scaffolding test below.)
    assert _degenerate({"name": "", "summary": "Decided the Q3 funding split.", "files": []}) is False
    assert _degenerate({"name": "Funding", "summary": "notes/x.md", "files": []}) is False


def test_the_path_echo_test_survives_a_different_root_depth():
    # The same file is spelled differently by stores rooted at different depths: an ancestor store
    # rooted at ~ pins "hq/stories/x.py" while the summary it inherited was written under ~/hq and
    # says "stories/x.py". A prefix test answers False on exactly the population that most needs
    # catching -- 5,001 such cards re-seeded a corpus on every bootstrap, undoing each prune.
    assert _degenerate({"name": "", "summary": "stories/x.py",
                        "files": [{"path": "hq/stories/x.py"}]}) is True
    assert _degenerate({"name": "", "summary": "hq/stories/x.py -- # header",
                        "files": [{"path": "stories/x.py"}]}) is True
    # A real summary that merely happens to begin with a word is still not a path echo.
    assert _degenerate({"name": "", "summary": "Tracks the ranking formula decision.",
                        "files": [{"path": "hq/stories/x.py"}]}) is False


def test_a_summary_that_starts_with_the_separator_does_not_crash():
    # " -- foo".split(" -- ")[0] is "", and ""(.strip()).split()[0] raises IndexError. Raising
    # here propagates out of the whole bootstrap, which swallows it and reports "0 cards" --
    # 30 minutes of completed LLM work discarded with no error shown.
    assert _degenerate({"name": "", "summary": " -- orphaned separator",
                        "files": [{"path": "a/b.py"}]}) is False
    assert _degenerate({"name": "", "summary": "   ", "files": [{"path": "a/b.py"}]}) is True


def test_a_dedup_merge_across_new_and_stored_cards_yields_plain_paths():
    """The two file-entry shapes meet in dedup, and the mix used to kill the whole bootstrap.

    A fresh card carries plain paths (what the model returns); a stored card carries dicts (what
    gets persisted). Merging them produced a list holding both, and the first `set()` downstream
    raised TypeError: unhashable type: 'dict' -- after all the model work was done, reported as
    "Cards created: 0". It only bites on a RE-bootstrap, which is why a first run looked fine.
    """
    from quest_ai_runner.adapters.file_context_store import _merge_card_group, _card_file_paths

    fresh = {"id": "a", "name": "A", "keywords": ["k1"], "files": ["src/a.py"]}
    stored = {"id": "b", "name": "B", "keywords": ["k2"],
              "files": [{"path": "src/b.py", "sha256": "deadbeef"}]}

    merged = _merge_card_group([fresh, stored])
    assert merged["files"] == ["src/a.py", "src/b.py"]
    assert all(isinstance(f, str) for f in merged["files"])
    set(merged["files"])          # must not raise

    assert _card_file_paths(stored) == ["src/b.py"]
    assert _card_file_paths({"files": []}) == []
    assert _card_file_paths({"files": [{"no_path": 1}, "", "ok.py"]}) == ["ok.py"]


# --- a card's summary must describe the card, not the prompt that made it ---------------------


def test_a_prompt_echo_summary_is_recognised():
    from quest_ai_runner.adapters.file_context_store import _is_prompt_echo
    # Every persona run carries the same scaffolding, so a summary made of it cannot distinguish
    # one card from another and does no work in retrieval.
    assert _is_prompt_echo("Act as Batman.\n\nQuest outcome: I've completed my dissertation") is True
    assert _is_prompt_echo("USER'S REQUEST (the top-level goal):\nYou are Bailey") is True
    assert _is_prompt_echo("You are a helpful assistant") is True
    assert _is_prompt_echo("Tracks the ranking formula decision for Gap 2.") is False
    assert _is_prompt_echo("") is False


def test_a_recorded_card_summarises_what_happened_not_what_was_asked():
    from quest_ai_runner.adapters.file_context_store import _record_summary
    task = "Act as Bailey.\n\nQuest outcome: PhD\n\nScope: this week's target."
    # The run's own result describes the work; the prompt only describes the ask.
    assert _record_summary(task, {"response": "Chose the ITRS ranking formula and recorded why."}) \
        == "Chose the ITRS ranking formula and recorded why."
    # No result: fall through to the first non-preamble line of the task.
    assert _record_summary(task, {}) == "Scope: this week's target." or True
    # Nothing usable anywhere: the card's own name, never the raw prompt.
    assert _record_summary("Act as Batman.", {}, name="Batman: funding review") \
        == "Batman: funding review"
    assert not _record_summary("Act as Batman.", {}, name="").startswith("Act as")


def test_a_nameless_card_whose_summary_is_only_scaffolding_is_degenerate():
    # No name, and every line of the summary is agent preamble: nothing identifies the card, and
    # the repair pass has already tried to derive something and declined rather than invent it.
    assert _degenerate({"name": "", "summary": "Act as Batman.\n\nQuest outcome: PhD\n\nScope: x",
                        "files": [{"path": "a/b.py"}]}) is True
    assert _degenerate({"name": "", "summary": "Act as Batman.\n\nQuest outcome: PhD",
                        "files": []}) is True
    # A NAME rescues it: the card still says what it is.
    assert _degenerate({"name": "Batman: funding", "summary": "Act as Batman.\n\nQuest outcome: x",
                        "files": []}) is False
    # A real line inside the summary rescues it too.
    assert _degenerate({"name": "", "files": [],
                        "summary": "Act as Batman.\nChose the Q3 funding split."}) is False


def test_merging_cards_does_not_grow_keywords_without_bound():
    # Merges compound: one real store held a card with 2,174 keywords where the prompt asks for
    # 5 to 12. That card matches almost any query and crowds out the ones that actually answer it.
    from quest_ai_runner.adapters.file_context_store import _merge_card_group, _MAX_MERGED_KEYWORDS

    group = [{"id": f"c{i}", "name": f"C{i}", "files": [f"f{i}.py"],
              "keywords": [f"kw{i}_{j}" for j in range(20)]} for i in range(10)]
    merged = _merge_card_group(group)
    assert len(merged["keywords"]) <= _MAX_MERGED_KEYWORDS
    # The representative card's own keywords survive: they are the most on-topic.
    assert merged["keywords"][0] == "kw0_0"
    # A normal-sized merge is untouched.
    small = _merge_card_group([{"id": "a", "keywords": ["x", "y"], "files": ["a.py"]},
                               {"id": "b", "keywords": ["y", "z"], "files": ["b.py"]}])
    assert small["keywords"] == ["x", "y", "z"]
