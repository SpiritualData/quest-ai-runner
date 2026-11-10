"""The "where things are" block of a deep prompt and the trail that feeds it (``mind/delegation/exploration_trail.py``).

No model and no network: a temporary working folder, a temporary trail store, and a made-up session record shaped like Claude Code's."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from hubofhubs.api import session as hub

from quest_ai_runner.the_intelligence.mind.delegation import exploration_trail as et
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_index import folder_map
from quest_ai_runner.the_intelligence.mind.delegation.prompt_budget import split_preamble


@pytest.fixture
def work(tmp_path):
    root = tmp_path / "work"
    (root / "docs").mkdir(parents=True)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "README.md").write_text("x")
    (root / "docs" / "plan.md").write_text("x")
    (root / "docs" / "notes.md").write_text("x")
    return root


@pytest.fixture
def store(tmp_path):
    return hub.TrailStore(str(tmp_path / "trails"))


def test_block_has_trail_and_map(work, store):
    store.record(str(work), "g1", {"read": ["README.md"], "changed": ["docs/plan.md"]})
    block = et.where_things_are(str(work), "g1", store)
    assert block.startswith(et.TRAIL_HEADER)
    assert "do not search for them" in block
    assert "- docs/plan.md (changed)" in block
    assert "- README.md\n" in block + "\n"
    assert "Folder map" in block and "docs/" in block and "  docs/notes.md" in block
    assert "node_modules" not in block and ".git" not in block
    assert block.rstrip().endswith("Search only for what is not listed here.")
    assert [s.name for s in split_preamble(block)] == ["where things are"]   # the prompt budget sees it as its own block


def test_map_only_without_a_trail(work, store):
    block = et.where_things_are(str(work), "g-new", store)
    assert "Files earlier runs" not in block and "Folder map" in block


def test_absent_without_a_working_folder(tmp_path, store):
    assert et.where_things_are(None, "g1", store) == ""
    assert et.where_things_are("", "g1", store) == ""
    assert et.where_things_are(str(tmp_path / "missing"), "g1", store) == ""


def test_stale_paths_are_dropped(work, store):
    store.record(str(work), "g1", {"read": ["gone.md", "README.md"], "changed": ["deleted/file.py"]})
    block = et.where_things_are(str(work), "g1", store)
    assert "gone.md" not in block and "deleted/file.py" not in block and "- README.md" in block


def test_caps_are_respected(tmp_path, store):
    big = tmp_path / "big"
    for n in range(100):
        (big / f"d{n:03d}").mkdir(parents=True)
        for m in range(10):
            (big / f"d{n:03d}" / ("f" * 30 + f"{m}.txt")).write_text("x")
    lines = folder_map(str(big))
    assert len(lines) <= 60 and sum(len(x) + 1 for x in lines) <= 2500
    for n in range(30):
        (big / f"t{n}.md").write_text("x")
    store.record(str(big), "g", {"read": [f"t{n}.md" for n in range(30)], "changed": []})
    block = et.where_things_are(str(big), "g", store)
    assert block.count("\n- t") == et.TRAIL_FILES_LISTED
    assert len(block) <= et.BLOCK_MAX_CHARS


def test_record_run_trail_reads_the_session_and_keeps_inside_paths(work, store, tmp_path, monkeypatch):
    session = tmp_path / "s.jsonl"
    use = lambda i, name, **inp: {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": i, "name": name, "input": inp}]}}
    session.write_text("".join(json.dumps(r) + "\n" for r in [
        use("1", "Read", file_path=str(work / "README.md")), use("2", "Read", file_path="/etc/hostname"),
        use("3", "Edit", file_path=str(work / "docs" / "plan.md"))]))
    monkeypatch.setattr(et, "resolve_session_file", lambda wd, sid: session)
    assert et.record_run_trail(str(work), "g1", "sid", store) is True
    assert store.runs(str(work), "g1") == [{"read": ["README.md"], "changed": ["docs/plan.md"]}]


def test_recording_failure_never_raises(work, monkeypatch):
    def boom(*a):
        raise RuntimeError("no")
    monkeypatch.setattr(et, "resolve_session_file", boom)
    assert et.record_run_trail(str(work), "g1", "sid") is False
    assert et.record_run_trail(None, "g1", "sid") is False
    assert et.record_run_trail(str(work), "g1", None) is False


def test_goal_key_falls_back_to_the_folder():
    assert et.goal_key_of({"quest_id": "q1"}) == "q1"
    assert et.goal_key_of(None) == et.UNKEYED_GOAL


def test_a_trail_is_kept_and_read_by_solution_first_then_by_the_wider_scope(tmp_path):
    from quest_ai_runner.the_intelligence.mind.delegation import exploration_trail as et

    class Store:
        def __init__(self):
            self.kept = {"solution:s1": [{"path": "a.md", "runs": 1, "changed": True}], "q1": [{"path": "a.md", "runs": 2, "changed": False}, {"path": "b.md", "runs": 1, "changed": False}]}

        def ranked(self, working_dir, key, limit=0):
            return self.kept.get(key, [])
    for name in ("a.md", "b.md"):
        (tmp_path / name).write_text("x")
    keys = et.scope_keys({"quest_id": "q1"}, ["s1", "", "s1"])
    assert keys == ["solution:s1", "q1"]
    block = et.where_things_are(str(tmp_path), keys, store=Store())
    assert "on this solution used" in block and block.index("a.md (changed)") < block.index("b.md") and block.count("- a.md") == 1
    assert et.scope_keys(None) == [et.UNKEYED_GOAL]
