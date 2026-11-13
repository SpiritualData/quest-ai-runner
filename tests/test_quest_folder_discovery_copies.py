"""A QUEST_SYNC.md inside a folder of unedited copies belongs to another quest and is never a working folder."""
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_folder_index import discover_quest_folders


def test_a_copy_of_a_sync_file_under_originals_is_not_discovered(tmp_path):
    real = tmp_path / "academy"
    copy = real / "human_context" / "originals"
    copy.mkdir(parents=True)
    (real / "QUEST_SYNC.md").write_text("---\nquest_id: quest_real\n---\n")
    (copy / "QUEST_SYNC.md").write_text("---\nquest_id: quest_other\n---\n")
    assert [f.quest_id for f in discover_quest_folders(str(tmp_path))] == ["quest_real"]
