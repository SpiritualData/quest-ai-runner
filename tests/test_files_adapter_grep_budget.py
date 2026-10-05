"""grep stays bounded on a huge tree: a time budget, a file cap, a focus directory, QAR_SKIP_DIRS."""
from __future__ import annotations

from quest_ai_runner.adapters.files_adapter import FilesAdapter


def build(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "a.md").write_text("registration deadline\n")
    (tmp_path / "bulk").mkdir()
    for i in range(30):
        (tmp_path / "bulk" / f"f{i}.md").write_text("registration noise\n")
    return FilesAdapter(str(tmp_path))


def test_zero_budget_returns_partial_with_a_note(tmp_path, monkeypatch):
    adapter = build(tmp_path)
    monkeypatch.setenv("QAR_GREP_BUDGET_SECONDS", "0.000001")
    obs = adapter.grep("registration")
    assert obs.kind == "grep" and "stopped early" in obs.text


def test_file_cap_stops_the_walk(tmp_path, monkeypatch):
    adapter = build(tmp_path)
    monkeypatch.setenv("QAR_GREP_MAX_FILES", "5")
    obs = adapter.grep("registration", max_hits=100)
    assert len(obs.hits) <= 5 and "file cap" in obs.text


def test_focus_directory_is_searched_first(tmp_path, monkeypatch):
    adapter = build(tmp_path)
    monkeypatch.chdir(tmp_path / "work")
    obs = adapter.grep("registration")
    assert [h["rel_path"] for h in obs.hits] == ["work/a.md"]
    # Nothing in the focus directory: widen to the whole root.
    obs = adapter.grep("noise", max_hits=3)
    assert len(obs.hits) == 3 and all(h["rel_path"].startswith("bulk/") for h in obs.hits)


def test_env_skip_dirs_are_never_searched(tmp_path, monkeypatch):
    monkeypatch.setenv("QAR_SKIP_DIRS", "bulk")
    adapter = build(tmp_path)
    obs = adapter.grep("registration", max_hits=100)
    assert [h["rel_path"] for h in obs.hits] == ["work/a.md"]
