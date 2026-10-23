"""ESOI_SPEC.md sync and the "send only on change" polls, against a MOCK client. No network."""
import os

from quest_ai_runner.runner.quest_folder_sync import (
    NEXT_STEPS_ENTRY_NAME, NextSteps, SPEC_FILE_NAME, SYNC_FILE_NAME, publish_next_steps,
    pull_quest_to_folder, push_folder_to_quest, push_quest_state, sync_quest_folder,
)
from tests.test_quest_folder_sync import QUEST_ID, MockQuestFolderClient

SPEC = {
    "subjectType": "quest", "subjectId": QUEST_ID, "digest": "d1", "refreshedAt": None,
    "records": [
        {"recordId": "rec_p1", "type": "problem",
         "fields": {"name": "No signups", "description": "Nobody has paid yet.", "empty": ""}},
        {"recordId": "rec_s1", "type": "solution",
         "fields": {"name": "Paid ads", "approach": ["Reddit", "Meta"]}},
    ],
}


class SpecClient(MockQuestFolderClient):
    def __init__(self, spec=SPEC, **kw):
        super().__init__(**kw)
        self.spec = spec
        self.spec_calls = []

    def get_spec(self, subject_type, subject_id):
        self.spec_calls.append((subject_type, subject_id))
        return self.spec


def test_pull_writes_spec_file(tmp_path):
    client = SpecClient()
    result = pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    text = (tmp_path / SPEC_FILE_NAME).read_text()
    assert result.spec_records == 2
    assert client.spec_calls == [("quest", QUEST_ID)]
    assert "### P: No signups (rec_p1)" in text
    assert "### S: Paid ads (rec_s1)" in text
    assert "**description:** Nobody has paid yet." in text
    assert "- Reddit" in text and "empty" not in text
    assert "spec-digest: d1" in text


def test_unchanged_spec_does_not_rewrite(tmp_path):
    client = SpecClient()
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    spec_path, sync_path = tmp_path / SPEC_FILE_NAME, tmp_path / SYNC_FILE_NAME
    os.utime(spec_path, (1000, 1000))
    os.utime(sync_path, (1000, 1000))
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    assert spec_path.stat().st_mtime == 1000
    assert sync_path.stat().st_mtime == 1000


def test_changed_spec_is_rewritten_and_human_text_survives(tmp_path):
    client = SpecClient()
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    path = tmp_path / SPEC_FILE_NAME
    path.write_text(path.read_text() + "\nMy own remark outside the block.\n")
    client.spec = {**SPEC, "digest": "d2", "records": SPEC["records"][:1]}
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    text = path.read_text()
    assert "My own remark outside the block." in text
    assert "Paid ads" not in text and "spec-digest: d2" in text


def test_no_spec_flag_and_unreadable_spec_leave_file_alone(tmp_path):
    client = SpecClient()
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path), spec=False)
    assert not (tmp_path / SPEC_FILE_NAME).exists() and client.spec_calls == []
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    before = (tmp_path / SPEC_FILE_NAME).read_text()
    client.spec = None
    result = sync_quest_folder(client, QUEST_ID, str(tmp_path), direction="pull")
    assert result.spec_records is None
    assert (tmp_path / SPEC_FILE_NAME).read_text() == before


def test_push_with_nothing_to_send_makes_no_client_call(tmp_path):
    client = SpecClient()
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    client.get_calls.clear()
    client.notes_calls.clear()
    push_folder_to_quest(client, QUEST_ID, str(tmp_path))
    assert push_quest_state(client, QUEST_ID, str(tmp_path)) == {
        "pushed": {}, "blocked": [], "held": "", "unwritable": []}
    assert client.get_calls == [] and client.notes_calls == [] and client.add_note_calls == []


def test_edited_goal_block_still_reaches_quest(tmp_path):
    client = SpecClient()
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    path = tmp_path / SYNC_FILE_NAME
    path.write_text(path.read_text().replace("**Status:** In progress", "**Status:** Completed"))
    client.get_calls.clear()
    push_quest_state(client, QUEST_ID, str(tmp_path))
    assert client.get_calls == [QUEST_ID]


def test_unchanged_next_steps_entry_is_not_put_again(tmp_path):
    steps = NextSteps(steps=["Do the thing"], updated="2026-10-23")

    class EntryClient(SpecClient):
        def __init__(self):
            super().__init__()
            self.entries, self.updates = [], 0

        def list_context_entries(self, quest_id):
            return list(self.entries)

        def create_context_entry(self, quest_id, name, content):
            entry = {"id": "ce_1", "name": name, "content": content}
            self.entries.append(entry)
            return entry

        def update_context_entry(self, quest_id, entry_id, content=None):
            self.updates += 1
            return {"id": entry_id}

    client = EntryClient()
    publish_next_steps(client, QUEST_ID, str(tmp_path), steps)
    assert client.entries[0]["name"] == NEXT_STEPS_ENTRY_NAME
    publish_next_steps(client, QUEST_ID, str(tmp_path), steps)
    assert client.updates == 0
    publish_next_steps(client, QUEST_ID, str(tmp_path), NextSteps(steps=["Other"], updated="2026-10-23"))
    assert client.updates == 1
