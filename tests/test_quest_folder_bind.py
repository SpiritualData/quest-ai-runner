"""quest_folder_bind: the safe write of the lane's quest -> folder map, and the autopilot brief's FIRST STEP."""
import json

import pytest

from quest_ai_runner.intelligence.higher_mind.objectives.quest_folder_bind import (
    QuestFolderBindError, bind_quest_folder, folder_candidates, read_map, resolve_root)
from quest_ai_runner.intelligence.proactivity.autopilot_briefs import FOLDER_FIRST_STEP
from .test_autopilot import _ContextEntryClient, _goal, _goals_payload, _now, _quest, AutopilotPass


@pytest.fixture
def lane(tmp_path):
    root = tmp_path / "stories"
    root.mkdir()
    return root, tmp_path / "map.json"


def test_bind_creates_folder_records_entry_and_keeps_others(lane):
    root, map_file = lane
    map_file.write_text(json.dumps({"q_old": str(root / "old")}))
    result = bind_quest_folder(str(map_file), "q1", str(root / "area" / "one"), root=str(root))
    assert result.added and (root / "area" / "one" / "CLAUDE.md").exists()
    assert read_map(str(map_file)) == {"q_old": str(root / "old"), "q1": str(root / "area" / "one")}
    assert not list(map_file.parent.glob("map.json.*.tmp")) and map_file.read_text().endswith("\n")


def test_bind_is_idempotent(lane):
    root, map_file = lane
    first = bind_quest_folder(str(map_file), "q1", str(root / "a" / "one"), root=str(root))
    again = bind_quest_folder(str(map_file), "q1", str(root / "a" / "one"), root=str(root))
    assert first.added and not again.added
    assert read_map(str(map_file)) == {"q1": str(root / "a" / "one")}


def test_bind_refuses_a_folder_bound_to_another_quest(lane):
    root, map_file = lane
    bind_quest_folder(str(map_file), "q1", str(root / "a" / "one"), root=str(root))
    before = map_file.read_text()
    with pytest.raises(QuestFolderBindError):
        bind_quest_folder(str(map_file), "q2", str(root / "a" / "one"), root=str(root))
    assert map_file.read_text() == before


def test_bind_refuses_to_move_a_bound_quest(lane):
    root, map_file = lane
    bind_quest_folder(str(map_file), "q1", str(root / "a" / "one"), root=str(root))
    with pytest.raises(QuestFolderBindError):
        bind_quest_folder(str(map_file), "q1", str(root / "a" / "two"), root=str(root))
    assert not (root / "a" / "two").exists()


def test_bind_refuses_outside_the_root_and_writes_nothing(lane, tmp_path):
    root, map_file = lane
    for bad in (tmp_path / "elsewhere", root / ".." / "escape", root):
        with pytest.raises(QuestFolderBindError):
            bind_quest_folder(str(map_file), "q1", str(bad), root=str(root))
    assert not map_file.exists() and not (tmp_path / "elsewhere").exists() and not (tmp_path / "escape").exists()


def test_candidates_mark_bound_folders_and_write_nothing(lane):
    root, map_file = lane
    (root / "area" / "home").mkdir(parents=True)
    (root / "area" / "home" / "CLAUDE.md").write_text("x")
    (root / "area" / "plain").mkdir()
    (root / "other").mkdir()
    (root / "other" / "GOALS.md").write_text("x")
    map_file.write_text(json.dumps({"q9": str(root / "area" / "home")}))
    before = sorted(p.as_posix() for p in root.rglob("*"))
    found = {c.path: c for c in folder_candidates(str(root), str(map_file))}
    assert sorted(p.as_posix() for p in root.rglob("*")) == before
    assert set(found) == {str(root / "area" / "home"), str(root / "other")}
    assert found[str(root / "area" / "home")].bound_to == "q9" and found[str(root / "area" / "home")].area == "area"
    assert found[str(root / "other")].bound_to is None


def test_root_defaults_to_corpus_stories():
    assert resolve_root(None, "/c") == "/c/stories" and resolve_root("/r", "/c") == "/r" and resolve_root(None, None) is None


def _brief(binding, folder_map=None):
    client = _ContextEntryClient(quests=[_quest("q1")], goals_by_quest={"q1": _goals_payload(("day", "2026-07-12", [_goal("g1")]))})
    AutopilotPass(client, team_id="team1", now=_now, quest_folder_map=folder_map, quest_folder_binding=binding).run({"text": "pass"})
    return client.created_tasks[0]["text"]


def test_first_step_is_in_the_brief_only_for_an_unmapped_quest(lane):
    root, map_file = lane
    binding = {"map_file": str(map_file), "config_path": "/lane/qar.toml", "root": str(root)}
    text = _brief(binding)
    assert text.index("FIRST STEP") < text.index("Scope")  # prepended, ahead of the run's own content
    assert "quest-ai-runner quest-folder candidates --config /lane/qar.toml" in text
    assert "quest-folder bind --config /lane/qar.toml --quest-id q1 --folder <folder>" in text and f"{root}/<area>/<short-slug>" in text
    assert "FIRST STEP" not in _brief(binding, {"q1": str(root / "mapped")})   # mapped: unchanged
    assert "FIRST STEP" not in _brief(None)                                      # no map file: off
    map_file.write_text(json.dumps({"q1": str(root / "bound-meanwhile")}))
    assert "FIRST STEP" not in _brief(binding)                                   # bound by another run since startup
    assert FOLDER_FIRST_STEP.count("FIRST STEP") == 1
