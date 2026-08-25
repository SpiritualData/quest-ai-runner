"""quest_folder_sync — pull/push a Quest quest <-> a local folder, against a MOCK client.

No network: a tiny in-memory Quest client records get/note calls and serves canned quest state.
"""
import tempfile
from pathlib import Path

import pytest

from quest_ai_runner.runner.quest_folder_sync import (
    NEXT_STEPS_ENTRY_NAME,
    NEXT_STEPS_NOTE_MARKER,
    NextSteps,
    QuestFolderSyncError,
    publish_next_steps,
    pull_quest_to_folder,
    push_folder_to_quest,
    read_next_steps,
    render_sync_file,
    sync_quest_folder,
    write_next_steps,
)

QUEST_ID = "quest_c18a9d1409ff"


class MockQuestFolderClient:
    """In-memory stand-in for QuestClient's account-wide quest surface. No HTTP."""

    def __init__(self, state=None, notes=None):
        self._state = state if state is not None else {
            "outcome": "The Super Psychic Academy reaches 100 paying clients",
            "completed": False,
            "category": "Career",
            "current_state": "Website is live. A few friends trialed it for free.",
            "strategies": [
                {"id": "strat_ads", "title": "Run paid ads on Reddit and Meta", "accepted": True},
                {"id": "strat_seo", "title": "Unaccepted idea", "accepted": False},
            ],
        }
        self._notes = list(notes or [])
        self._next_note_id = 1
        self.get_calls = []
        self.notes_calls = []
        self.add_note_calls = []  # list of (quest_id, text)

    def get_my_quest(self, quest_id):
        self.get_calls.append(quest_id)
        return {"quest_id": quest_id, "state": dict(self._state)}

    def list_quest_notes(self, quest_id):
        self.notes_calls.append(quest_id)
        return [dict(n) for n in self._notes]

    def add_quest_note(self, quest_id, text):
        self.add_note_calls.append((quest_id, text))
        note = {
            "note_id": f"note_{self._next_note_id}",
            "text": text,
            "author_kind": "ai",
            "created_at": "2026-07-08T00:00:00",
        }
        self._next_note_id += 1
        self._notes.append(note)
        return [dict(n) for n in self._notes]


# --- rendering round-trip --------------------------------------------------------

def test_render_includes_goal_and_notes():
    state = {"outcome": "Reach 100 clients", "completed": False, "current_state": "Some progress"}
    notes = [{"note_id": "note_1", "text": "tested with friends", "created_at": "2026-07-01"}]
    text = render_sync_file("", QUEST_ID, state, notes)
    assert "Reach 100 clients" in text
    assert "Some progress" in text
    assert "tested with friends" in text
    assert "<!-- id:note_1 -->" in text
    assert f"quest_id: {QUEST_ID}" in text
    assert "## Notes to push to Quest" in text


def test_render_preserves_human_authored_content():
    existing = f"---\nquest_id: {QUEST_ID}\n---\n\n# My notes\n\nHand-written prose to keep.\n"
    out = render_sync_file(existing, QUEST_ID, {"outcome": "X"}, [])
    assert "Hand-written prose to keep." in out
    assert "QAR:MANAGED:goal" in out


def test_render_is_idempotent():
    state = {"outcome": "X", "current_state": "Y"}
    notes = [{"note_id": "n1", "text": "t"}]
    once = render_sync_file("# Head\n", QUEST_ID, state, notes)
    twice = render_sync_file(once, QUEST_ID, state, notes)
    assert once == twice


def test_render_overwrites_only_managed_region_on_change():
    v1 = render_sync_file("# Keep me\n", QUEST_ID, {"outcome": "OLD"}, [])
    v2 = render_sync_file(v1, QUEST_ID, {"outcome": "NEW"}, [])
    assert "NEW" in v2 and "OLD" not in v2
    assert "# Keep me" in v2


def test_render_scaffolds_to_push_section_once():
    once = render_sync_file("", QUEST_ID, {"outcome": "X"}, [])
    assert once.count(_TO_PUSH_HEADING := "## Notes to push to Quest") == 1
    # Adding a human bullet under the section must survive a re-render.
    with_bullet = once.replace(_TO_PUSH_HEADING, _TO_PUSH_HEADING + "\n- a fresh local finding", 1)
    twice = render_sync_file(with_bullet, QUEST_ID, {"outcome": "X"}, [])
    assert "a fresh local finding" in twice
    assert twice.count(_TO_PUSH_HEADING) == 1


# --- pull -------------------------------------------------------------------------

def test_pull_writes_sync_file_with_quest_data():
    client = MockQuestFolderClient(notes=[{"note_id": "note_1", "text": "first note"}])
    with tempfile.TemporaryDirectory() as d:
        res = pull_quest_to_folder(client, QUEST_ID, d)
        content = Path(d, "QUEST_SYNC.md").read_text()
    assert client.get_calls == [QUEST_ID]
    assert res.pulled and res.direction == "pull" and res.notes_pulled == 1
    assert "The Super Psychic Academy reaches 100 paying clients" in content
    assert "first note" in content
    assert "Run paid ads on Reddit and Meta" in content
    assert "Unaccepted idea" not in content  # only accepted strategies render


def test_pull_is_idempotent_no_rewrite_when_unchanged():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        pull_quest_to_folder(client, QUEST_ID, d)
        first = Path(d, "QUEST_SYNC.md").read_text()
        pull_quest_to_folder(client, QUEST_ID, d)
        second = Path(d, "QUEST_SYNC.md").read_text()
    assert first == second


def test_pull_preserves_existing_human_content():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        Path(d, "QUEST_SYNC.md").write_text("# Hand-written header\n\nKeep this.\n")
        pull_quest_to_folder(client, QUEST_ID, d)
        content = Path(d, "QUEST_SYNC.md").read_text()
    assert "Keep this." in content
    assert "100 paying clients" in content


def test_pull_missing_quest_raises():
    client = MockQuestFolderClient(state={})
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(QuestFolderSyncError):
            pull_quest_to_folder(client, QUEST_ID, d)


# --- push -------------------------------------------------------------------------

def test_push_posts_unsynced_bullet_and_marks_it():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        pull_quest_to_folder(client, QUEST_ID, d)
        path = Path(d, "QUEST_SYNC.md")
        path.write_text(path.read_text().replace(
            "## Notes to push to Quest",
            "## Notes to push to Quest\n- leads.csv now has 12 signups",
        ))
        res = push_folder_to_quest(client, QUEST_ID, d)
        content = path.read_text()
    assert res.pushed and res.direction == "push" and res.notes_pushed == 1
    assert client.add_note_calls == [(QUEST_ID, "leads.csv now has 12 signups")]
    assert "<!-- id:note_1 --> leads.csv now has 12 signups" in content


def test_push_skips_already_synced_bullets():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        pull_quest_to_folder(client, QUEST_ID, d)
        path = Path(d, "QUEST_SYNC.md")
        path.write_text(path.read_text().replace(
            "## Notes to push to Quest",
            "## Notes to push to Quest\n- <!-- id:note_9 --> already synced earlier",
        ))
        push_folder_to_quest(client, QUEST_ID, d)
    assert client.add_note_calls == []  # nothing new to push


def test_push_missing_file_raises():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(QuestFolderSyncError):
            push_folder_to_quest(client, QUEST_ID, d)


def test_push_api_failure_leaves_bullet_unsynced_and_uncounted():
    """add_quest_note returning [] (the client's failure mode) must NOT mark the bullet as
    synced or count it as pushed — it stays queued for the next push."""
    client = MockQuestFolderClient()
    client.add_quest_note = lambda quest_id, text: []  # simulate QuestClient's failure return
    with tempfile.TemporaryDirectory() as d:
        pull_quest_to_folder(client, QUEST_ID, d)
        path = Path(d, "QUEST_SYNC.md")
        path.write_text(path.read_text().replace(
            "## Notes to push to Quest",
            "## Notes to push to Quest\n- a finding the API lost",
        ))
        res = push_folder_to_quest(client, QUEST_ID, d)
        content = path.read_text()
    assert res.notes_pushed == 0
    assert "- a finding the API lost" in content          # still queued, plain bullet
    assert "<!-- id:" not in content.split("## Notes to push to Quest")[1]


def test_push_marks_bullet_even_when_server_rewrites_text():
    """If the server transforms the note text (trim, formatting), the exact-text match fails;
    the push must still mark the bullet with the LAST note's id — otherwise the bullet is
    re-posted as a duplicate on every future push."""
    client = MockQuestFolderClient()

    def _rewriting_add(quest_id, text):
        client.add_note_calls.append((quest_id, text))
        client._notes.append({"note_id": "note_42", "text": text.upper()})  # server rewrote it
        return [dict(n) for n in client._notes]

    client.add_quest_note = _rewriting_add
    with tempfile.TemporaryDirectory() as d:
        pull_quest_to_folder(client, QUEST_ID, d)
        path = Path(d, "QUEST_SYNC.md")
        path.write_text(path.read_text().replace(
            "## Notes to push to Quest",
            "## Notes to push to Quest\n- server will shout this",
        ))
        res = push_folder_to_quest(client, QUEST_ID, d)
        content = path.read_text()
        # A second push must not re-post it.
        push_folder_to_quest(client, QUEST_ID, d)
    assert res.notes_pushed == 1
    assert "<!-- id:note_42 --> server will shout this" in content
    assert len(client.add_note_calls) == 1


# --- the one entry point -----------------------------------------------------------

def test_sync_quest_folder_pull_default():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        res = sync_quest_folder(client, QUEST_ID, d)
    assert res.direction == "pull" and res.pulled and not res.pushed


def test_sync_quest_folder_both_pulls_then_pushes():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        # Seed a local bullet before the "both" sync so push has something to send.
        pull_quest_to_folder(client, QUEST_ID, d)
        path = Path(d, "QUEST_SYNC.md")
        path.write_text(path.read_text().replace(
            "## Notes to push to Quest", "## Notes to push to Quest\n- a queued local finding",
        ))
        res = sync_quest_folder(client, QUEST_ID, d, direction="both")
    assert res.pulled and res.pushed and res.direction == "both"
    assert res.notes_pushed == 1
    assert client.add_note_calls == [(QUEST_ID, "a queued local finding")]


def test_sync_quest_folder_unknown_direction_raises():
    client = MockQuestFolderClient()
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(ValueError):
            sync_quest_folder(client, QUEST_ID, d, direction="sideways")


# --- the canonical next-steps artifact ---------------------------------------------

class MockContextEntryClient(MockQuestFolderClient):
    """Adds the context-entry surface (list/create/update), which unlike notes can be REPLACED."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.entries = []          # [{id, name, content}]
        self.creates = []          # names created
        self.updates = []          # (entry_id, content)
        self.list_error = None

    def list_context_entries(self, quest_id):
        if self.list_error:
            raise self.list_error
        return [{"id": e["id"], "name": e["name"]} for e in self.entries]

    def create_context_entry(self, quest_id, name, content):
        entry = {"id": f"entry_{len(self.entries) + 1}", "name": name, "content": content}
        self.entries.append(entry)
        self.creates.append(name)
        return dict(entry)

    def update_context_entry(self, quest_id, entry_id, *, name=None, content=None):
        for e in self.entries:
            if e["id"] == entry_id:
                if content is not None:
                    e["content"] = content
                if name is not None:
                    e["name"] = name
                self.updates.append((entry_id, content))
                return dict(e)
        return {}


def _steps(*items, updated="2026-08-12", carrying_over=()):
    return NextSteps(steps=list(items), carrying_over=list(carrying_over),
                     source="the autopilot pass", scope="day:2026-08-12", updated=updated)


def test_next_steps_block_round_trips_and_is_idempotent(tmp_path):
    write_next_steps(str(tmp_path), QUEST_ID, _steps("Draft chapter 2", "Email the committee"))
    once = Path(tmp_path, "QUEST_SYNC.md").read_text()
    write_next_steps(str(tmp_path), QUEST_ID, _steps("Draft chapter 2", "Email the committee"))
    twice = Path(tmp_path, "QUEST_SYNC.md").read_text()
    assert once == twice                                    # same conclusion -> byte-identical file
    body = read_next_steps(str(tmp_path))
    assert "1. Draft chapter 2" in body and "2. Email the committee" in body


def test_a_refresh_replaces_the_artifact_instead_of_appending(tmp_path):
    """The whole point: ONE current answer, never an accumulating log."""
    write_next_steps(str(tmp_path), QUEST_ID, _steps("Old step"))
    write_next_steps(str(tmp_path), QUEST_ID, _steps("New step"))
    content = Path(tmp_path, "QUEST_SYNC.md").read_text()
    assert "New step" in content
    assert "Old step" not in content
    assert content.count("QAR:MANAGED:next_steps START") == 1


def test_next_steps_never_touches_human_owned_content(tmp_path):
    client = MockQuestFolderClient()
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    path = Path(tmp_path, "QUEST_SYNC.md")
    path.write_text(path.read_text().replace(
        "## Notes to push to Quest",
        "## Notes to push to Quest\n- a finding I queued myself",
    ) + "\n## My own section\n\nHand-written prose.\n")
    write_next_steps(str(tmp_path), QUEST_ID, _steps("Do the next thing"))
    content = path.read_text()
    assert "a finding I queued myself" in content
    assert "Hand-written prose." in content
    assert "100 paying clients" in content                  # the goal block survived too
    # And a later pull must not disturb the next-steps block either.
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    assert "Do the next thing" in path.read_text()


def test_next_steps_bullets_are_not_mistaken_for_notes_to_push(tmp_path):
    """The carry-over bullets sit in a managed block. If the push parser walked into them they
    would be posted to Quest as notes on every push, which is silent note spam from a file the
    runner wrote itself."""
    client = MockQuestFolderClient()
    pull_quest_to_folder(client, QUEST_ID, str(tmp_path))
    write_next_steps(str(tmp_path), QUEST_ID,
                     _steps("Do the next thing", carrying_over=["Unfinished from last week"]))
    push_folder_to_quest(client, QUEST_ID, str(tmp_path))
    assert client.add_note_calls == []


def test_read_next_steps_is_none_when_there_is_no_artifact(tmp_path):
    assert read_next_steps(str(tmp_path)) is None           # no file at all
    Path(tmp_path, "QUEST_SYNC.md").write_text("# Just my own notes\n")
    assert read_next_steps(str(tmp_path)) is None           # a file, but no managed block


def test_publish_upserts_one_context_entry_rather_than_spamming(tmp_path):
    """Quest notes are append-only, so a refreshing artifact lives in a context ENTRY, which can be
    replaced in place. Refreshing three times must leave exactly one object on the quest."""
    client = MockContextEntryClient()
    for step in ("Step one", "Step two", "Step three"):
        res = publish_next_steps(client, QUEST_ID, str(tmp_path), _steps(step))
    assert client.creates == [NEXT_STEPS_ENTRY_NAME]        # created ONCE
    assert len(client.entries) == 1
    assert len(client.updates) == 2                          # then replaced in place
    assert "Step three" in client.entries[0]["content"]
    assert "Step one" not in client.entries[0]["content"]
    assert client.add_note_calls == []                       # and never as a note
    assert res.quest_target == "context_entry" and res.created is False
    assert res.quest_ref == client.entries[0]["id"]


def test_publish_leaves_an_unrelated_entry_alone(tmp_path):
    client = MockContextEntryClient()
    client.entries.append({"id": "entry_x", "name": "Literature review", "content": "keep me"})
    publish_next_steps(client, QUEST_ID, str(tmp_path), _steps("Step one"))
    assert client.updates == []                              # the human's entry was not touched
    assert client.entries[0]["content"] == "keep me"
    assert client.creates == [NEXT_STEPS_ENTRY_NAME]


def test_publish_writes_nothing_to_quest_when_the_entry_listing_fails(tmp_path):
    """A blind create on a quest that already has the entry is exactly the duplication this design
    exists to avoid, so a failed READ must not become a write. The local file still lands."""
    client = MockContextEntryClient()
    client.list_error = RuntimeError("entries endpoint down")
    res = publish_next_steps(client, QUEST_ID, str(tmp_path), _steps("Step one"))
    assert client.creates == [] and client.updates == [] and client.add_note_calls == []
    assert res.quest_target == "none" and "could not read" in res.detail
    assert "Step one" in Path(tmp_path, "QUEST_SYNC.md").read_text()


def test_publish_falls_back_to_a_marked_note_only_without_context_entry_support(tmp_path):
    """An older/limited client still gets the artifact onto the quest, marked so a consumer can
    find the latest one. This appends by nature: notes have no update route."""
    client = MockQuestFolderClient()                          # notes only
    res = publish_next_steps(client, QUEST_ID, str(tmp_path), _steps("Step one"))
    assert len(client.add_note_calls) == 1
    assert client.add_note_calls[0][1].startswith(NEXT_STEPS_NOTE_MARKER)
    assert res.quest_target == "note"
    assert "accumulate" in res.detail                         # the tradeoff is stated, not hidden


def test_publish_survives_a_quest_side_failure_with_the_local_file_intact(tmp_path):
    class BoomClient(MockContextEntryClient):
        def create_context_entry(self, quest_id, name, content):
            raise RuntimeError("boom")

    res = publish_next_steps(BoomClient(), QUEST_ID, str(tmp_path), _steps("Step one"))
    assert res.quest_target == "none" and "failed" in res.detail
    assert "Step one" in Path(tmp_path, "QUEST_SYNC.md").read_text()


# --- QuestClient account-wide quest endpoint shaping (no network) ------------------

def test_quest_client_account_quest_endpoints_shape_requests():
    from quest_ai_runner.runner.quest_client import QuestClient

    calls = []

    class CapturingClient(QuestClient):
        def _request(self, method, path, *, params=None, body=None):
            calls.append((method, path, params, body))
            return {"ok": True} if method == "GET" else []

    c = CapturingClient("http://x", "qsk_test")  # no team_id needed
    c.list_my_quests()
    c.get_my_quest(QUEST_ID)
    c.list_quest_notes(QUEST_ID)
    c.add_quest_note(QUEST_ID, "a note")

    assert calls[0] == ("GET", "/api/quests/me", None, None)
    assert calls[1] == ("GET", f"/api/quests/{QUEST_ID}/state", None, None)
    assert calls[2] == ("GET", f"/api/quests/{QUEST_ID}/notes", None, None)
    assert calls[3] == ("POST", f"/api/quests/{QUEST_ID}/notes", None, {"text": "a note"})


def test_quest_client_context_entry_endpoints_shape_requests():
    from quest_ai_runner.runner.quest_client import QuestClient

    calls = []

    class CapturingClient(QuestClient):
        def _request(self, method, path, *, params=None, body=None):
            calls.append((method, path, params, body))
            return [] if method == "GET" else {"id": "entry_1"}

    c = CapturingClient("http://x", "qsk_test")
    c.list_context_entries(QUEST_ID)
    c.create_context_entry(QUEST_ID, "Next steps", "1. do the thing")
    c.update_context_entry(QUEST_ID, "entry_1", content="1. do the other thing")

    assert calls[0] == ("GET", f"/api/quests/{QUEST_ID}/context-entries", None, None)
    assert calls[1] == ("POST", f"/api/quests/{QUEST_ID}/context-entries", None,
                        {"name": "Next steps", "content": "1. do the thing"})
    # PUT, not POST: replacing the one entry is what keeps a refreshing artifact from piling up.
    assert calls[2] == ("PUT", f"/api/quests/{QUEST_ID}/context-entries/entry_1", None,
                        {"content": "1. do the other thing"})


# --- poller integration: opt-in pull-before-run / push-after-run hooks -------------

def _quest_folder_aware_mock_client(quest_folder_client):
    """A MockQuestClient (task surface) that also serves the account-wide quest surface."""
    from .test_runner import MockQuestClient

    client = MockQuestClient([
        {"id": "goal-task", "text": "do goal work", "status": "queued", "goal_id": QUEST_ID},
    ])
    client.get_my_quest = quest_folder_client.get_my_quest
    client.list_quest_notes = quest_folder_client.list_quest_notes
    client.add_quest_note = quest_folder_client.add_quest_note
    return client


def test_poller_pulls_quest_folder_before_running_when_mapped():
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval

    quest_folder_client = MockQuestFolderClient()
    client = _quest_folder_aware_mock_client(quest_folder_client)
    with tempfile.TemporaryDirectory() as d:
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=StubProvider(decisions=[{"action": "answer", "rationale": "ok"}]),
            quest_folder_map={QUEST_ID: d},
        )
        poller = Poller(cfg, state_path=None, client=client)
        handled = poller.run_once()
        content = Path(d, "QUEST_SYNC.md").read_text()
    assert handled == ["goal-task"]
    # Pulled twice: once by the scan-level periodic sync (fires every scan regardless of task
    # pickup), once by the task-scoped pre-run pull (this task's goal_id maps to the same quest).
    assert quest_folder_client.get_calls == [QUEST_ID, QUEST_ID]
    assert "100 paying clients" in content


def test_poller_no_map_does_not_sync_quest_folder():
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval

    quest_folder_client = MockQuestFolderClient()
    client = _quest_folder_aware_mock_client(quest_folder_client)
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=StubProvider(decisions=[{"action": "answer", "rationale": "ok"}]),
    )
    poller = Poller(cfg, state_path=None, client=client)
    assert poller.run_once() == ["goal-task"]
    assert quest_folder_client.get_calls == []            # no map -> no sync


def test_poller_quest_folder_sync_failure_never_breaks_the_task():
    """A sync that raises (bad folder, API error) must not stop the task from running."""
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval

    class BoomClient(MockQuestFolderClient):
        def get_my_quest(self, quest_id):
            raise RuntimeError("boom")

    quest_folder_client = BoomClient()
    client = _quest_folder_aware_mock_client(quest_folder_client)
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=StubProvider(decisions=[{"action": "answer", "rationale": "ok"}]),
        quest_folder_map={QUEST_ID: "/tmp/does-not-matter"},
    )
    poller = Poller(cfg, state_path=None, client=client)
    assert poller.run_once() == ["goal-task"]                # task still ran
    assert client.reports[0][:2] == ("goal-task", "done")


def test_poller_pushes_quest_folder_after_run_when_direction_is_push():
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval

    quest_folder_client = MockQuestFolderClient()
    client = _quest_folder_aware_mock_client(quest_folder_client)
    with tempfile.TemporaryDirectory() as d:
        pull_quest_to_folder(quest_folder_client, QUEST_ID, d)
        path = Path(d, "QUEST_SYNC.md")
        path.write_text(path.read_text().replace(
            "## Notes to push to Quest", "## Notes to push to Quest\n- queued while offline",
        ))
        quest_folder_client.get_calls.clear()  # forget the setup pull above; only the poller's calls matter
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=StubProvider(decisions=[{"action": "answer", "rationale": "ok"}]),
            quest_folder_map={QUEST_ID: d},
            quest_folder_sync_direction="push",
        )
        poller = Poller(cfg, state_path=None, client=client)
        poller.run_once()
    assert quest_folder_client.add_note_calls == [(QUEST_ID, "queued while offline")]
    assert quest_folder_client.get_calls == []   # push-only direction: no pull before the run


# --- periodic per-scan sync: independent of task pickup ----------------------------

def test_run_once_syncs_mapped_quest_folders_even_with_no_due_tasks():
    """The whole point of a per-scan sync: a mapped quest's folder must stay current even when
    no task ever fires for it (e.g. someone only edits the quest on Quest directly)."""
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval
    from .test_runner import MockQuestClient

    quest_folder_client = MockQuestFolderClient()
    client = MockQuestClient([])  # no due tasks at all
    client.get_my_quest = quest_folder_client.get_my_quest
    client.list_quest_notes = quest_folder_client.list_quest_notes
    client.add_quest_note = quest_folder_client.add_quest_note
    with tempfile.TemporaryDirectory() as d:
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=StubProvider(decisions=[]),
            quest_folder_map={QUEST_ID: d},
        )
        poller = Poller(cfg, state_path=None, client=client)
        handled = poller.run_once()
        content = Path(d, "QUEST_SYNC.md").read_text()
    assert handled == []                                  # no task ran
    assert quest_folder_client.get_calls == [QUEST_ID]     # but the folder was still synced
    assert "100 paying clients" in content


def test_run_once_periodic_sync_failure_never_breaks_the_scan():
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval
    from .test_runner import MockQuestClient

    class BoomClient(MockQuestFolderClient):
        def get_my_quest(self, quest_id):
            raise RuntimeError("boom")

    quest_folder_client = BoomClient()
    client = MockQuestClient([])
    client.get_my_quest = quest_folder_client.get_my_quest
    client.list_quest_notes = quest_folder_client.list_quest_notes
    client.add_quest_note = quest_folder_client.add_quest_note
    cfg = RunnerConfig(
        quest_base_url="http://x", quest_api_key="qsk_test",
        retrieval=StubRetrieval({"README.md": "fact"}),
        model_provider=StubProvider(decisions=[]),
        quest_folder_map={QUEST_ID: "/tmp/does-not-matter"},
    )
    poller = Poller(cfg, state_path=None, client=client)
    assert poller.run_once() == []   # scan completes normally despite the sync failure


def test_run_once_syncs_every_mapped_quest_folder():
    """More than one mapped quest: each gets synced on the same scan."""
    from quest_ai_runner.config import RunnerConfig
    from quest_ai_runner.runner.poller import Poller

    from .conftest import StubProvider, StubRetrieval
    from .test_runner import MockQuestClient

    other_quest_id = "quest_other000000000"
    clients = {
        QUEST_ID: MockQuestFolderClient(),
        other_quest_id: MockQuestFolderClient(
            state={"outcome": "A second quest", "completed": False}),
    }

    def _get_my_quest(quest_id):
        return clients[quest_id].get_my_quest(quest_id)

    def _list_quest_notes(quest_id):
        return clients[quest_id].list_quest_notes(quest_id)

    def _add_quest_note(quest_id, text):
        return clients[quest_id].add_quest_note(quest_id, text)

    client = MockQuestClient([])
    client.get_my_quest = _get_my_quest
    client.list_quest_notes = _list_quest_notes
    client.add_quest_note = _add_quest_note
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        cfg = RunnerConfig(
            quest_base_url="http://x", quest_api_key="qsk_test",
            retrieval=StubRetrieval({"README.md": "fact"}),
            model_provider=StubProvider(decisions=[]),
            quest_folder_map={QUEST_ID: d1, other_quest_id: d2},
        )
        poller = Poller(cfg, state_path=None, client=client)
        poller.run_once()
        content1 = Path(d1, "QUEST_SYNC.md").read_text()
        content2 = Path(d2, "QUEST_SYNC.md").read_text()
    assert "100 paying clients" in content1
    assert "A second quest" in content2


# --- env wiring for QAR_QUEST_FOLDER_MAP / QAR_QUEST_FOLDER_SYNC_DIRECTION ---------

def test_env_wiring_for_quest_folder_map(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    monkeypatch.delenv("QAR_QUEST_FOLDER_MAP", raising=False)
    monkeypatch.delenv("QAR_QUEST_FOLDER_SYNC_DIRECTION", raising=False)
    cfg = _config_from_env()
    assert cfg.quest_folder_map is None                    # library default: off
    assert cfg.quest_folder_sync_direction == "pull"        # library default

    monkeypatch.setenv(
        "QAR_QUEST_FOLDER_MAP",
        '{"quest_1625d9f47a06": "/srv/corpus/concept_ai_interface_quest"}',
    )
    monkeypatch.setenv("QAR_QUEST_FOLDER_SYNC_DIRECTION", "both")
    cfg = _config_from_env()
    assert cfg.quest_folder_map == {
        "quest_1625d9f47a06": "/srv/corpus/concept_ai_interface_quest",
    }
    assert cfg.quest_folder_sync_direction == "both"


def test_env_wiring_ignores_invalid_quest_folder_map_json(monkeypatch):
    from quest_ai_runner.cli import _config_from_env

    monkeypatch.setenv("QAR_QUEST_FOLDER_MAP", "not valid json")
    cfg = _config_from_env()
    assert cfg.quest_folder_map is None   # bad JSON ignored, default kept

    monkeypatch.setenv("QAR_QUEST_FOLDER_MAP", '["a", "list", "not", "an", "object"]')
    cfg = _config_from_env()
    assert cfg.quest_folder_map is None   # non-object JSON ignored, default kept


# --- pushing the quest STATE back (the goal block is editable, within limits) ---

class MockStateClient(MockQuestFolderClient):
    """Adds the role-scoped field write, and records what was sent."""

    def __init__(self, *a, ok=True, blocked=None, held="", **kw):
        super().__init__(*a, **kw)
        self.writes = []
        self._ok, self._blocked, self._held = ok, blocked or [], held

    def write_quest_fields(self, quest_id, fields, *, team_id=None):
        self.writes.append((quest_id, dict(fields)))
        return {"ok": self._ok, "blocked": self._blocked, "held": self._held}


def _pulled(client, folder):
    pull_quest_to_folder(client, QUEST_ID, folder)
    return Path(folder) / "QUEST_SYNC.md"


def test_editing_the_outcome_pushes_it():
    from quest_ai_runner.runner.quest_folder_sync import push_quest_state
    with tempfile.TemporaryDirectory() as d:
        client = MockStateClient()
        path = _pulled(client, d)
        path.write_text(path.read_text().replace(
            "**Goal:** The Super Psychic Academy reaches 100 paying clients",
            "**Goal:** The Super Psychic Academy reaches 250 paying clients"))
        result = push_quest_state(client, QUEST_ID, d)
        assert client.writes == [(QUEST_ID, {"outcome":
                                             "The Super Psychic Academy reaches 250 paying clients"})]
        assert result["pushed"]


def test_an_unedited_file_pushes_nothing():
    from quest_ai_runner.runner.quest_folder_sync import push_quest_state
    with tempfile.TemporaryDirectory() as d:
        client = MockStateClient()
        _pulled(client, d)
        assert push_quest_state(client, QUEST_ID, d)["pushed"] == {}
        assert client.writes == []


def test_ticking_the_status_pushes_completion():
    from quest_ai_runner.runner.quest_folder_sync import push_quest_state
    with tempfile.TemporaryDirectory() as d:
        client = MockStateClient()
        path = _pulled(client, d)
        path.write_text(path.read_text().replace("**Status:** In progress",
                                                 "**Status:** Completed"))
        push_quest_state(client, QUEST_ID, d)
        assert client.writes == [(QUEST_ID, {"completed": True})]


def test_editing_current_state_is_reported_not_silently_dropped():
    """No role may write current_state; a person who retypes it must be told it did not land."""
    from quest_ai_runner.runner.quest_folder_sync import push_quest_state
    with tempfile.TemporaryDirectory() as d:
        client = MockStateClient()
        path = _pulled(client, d)
        path.write_text(path.read_text().replace(
            "Website is live. A few friends trialed it for free.",
            "Totally different situation now."))
        result = push_quest_state(client, QUEST_ID, d)
        assert result["unwritable"] == ["current_state"]
        assert client.writes == []


def test_a_server_side_refusal_is_surfaced_not_reported_as_success():
    from quest_ai_runner.runner.quest_folder_sync import push_quest_state
    with tempfile.TemporaryDirectory() as d:
        client = MockStateClient(ok=False, blocked=["outcome"], held="")
        path = _pulled(client, d)
        path.write_text(path.read_text().replace("100 paying clients", "250 paying clients"))
        result = push_quest_state(client, QUEST_ID, d)
        assert result["pushed"] == {} and result["blocked"] == ["outcome"]


def test_push_state_without_a_file_raises():
    from quest_ai_runner.runner.quest_folder_sync import push_quest_state
    with tempfile.TemporaryDirectory() as d:
        with pytest.raises(QuestFolderSyncError):
            push_quest_state(MockStateClient(), QUEST_ID, d)
