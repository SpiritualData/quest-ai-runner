"""Per-consumer watermarks, staged advances, quest collection discovery, folder files and CONTEXT_UPDATES.md."""
from datetime import datetime, timedelta, timezone
import os

from quest_ai_runner.body.intake_sources.context_sources_collections import (
    QuestCollectionsSource, entry_has_category, render_entry)
from quest_ai_runner.body.intake_sources.context_sources_folder import FolderFilesSource
from quest_ai_runner.body.intake_sources.context_update_state import CollectRequest, PendingMarks, Watermarks
from quest_ai_runner.body.intake_sources.context_updates import UpdateEngine
from quest_ai_runner.body.intake_sources.context_updates_file import (
    FILE_NAME, index_block, render_section, write_context_file)

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


class Client:
    def __init__(self):
        self.calls = []
        self.colls = [
            {"id": "c_link", "name": "Linked log", "type": "tracker", "linkedQuestIds": ["q1"], "enabled": True,
             "lastEntryAt": iso(0.5), "customFields": [{"id": "f1", "name": "What happened"}, {"id": "f2", "name": "Mood"}]},
            {"id": "c_old", "name": "Old", "type": "tracker", "linkedQuestIds": ["q1"], "enabled": True, "lastEntryAt": iso(30), "customFields": []},
            {"id": "c_cat", "name": "Category journal", "type": "custom", "categoryId": "cat_x", "enabled": True,
             "lastEntryAt": iso(1), "customFields": []},
            {"id": "c_other", "name": "Other area", "type": "custom", "categoryId": "cat_y", "enabled": True, "lastEntryAt": iso(1), "customFields": []},
            {"id": "h1", "name": "Walk", "type": "habit", "enabled": True, "lastEntryAt": iso(0.2), "customFields": []},
            {"id": "ins", "name": "Insights list", "type": None, "enabled": True, "lastEntryAt": iso(0.1),
             "customFields": [{"id": "insight", "name": "Insight"}, {"id": "categories", "name": "Categories"}]},
        ]
        self.entries = {
            "c_link": [{"createdAt": iso(0.5), "updatedAt": iso(0.5), "fieldValues": {"f1": "Wrote ch. 2", "f2": "", "entry_date": "2026-11-20"}},
                       {"createdAt": iso(9), "updatedAt": iso(0.3), "fieldValues": {"f1": "edited old"}},
                       {"createdAt": iso(10), "updatedAt": iso(10), "fieldValues": {"f1": "ancient"}}],
            "c_cat": [{"createdAt": iso(1), "updatedAt": iso(1), "fieldValues": {"x": "journal text"}}],
            "h1": [{"createdAt": iso(0.2), "updatedAt": iso(0.2), "fieldValues": {"completionType": "yes", "entry_date": "2026-11-20"}}],
            "ins": [{"createdAt": iso(0.1), "updatedAt": iso(0.1), "fieldValues": {"insight": "A", "categories": ["PhD"]}},
                    {"createdAt": iso(0.1), "updatedAt": iso(0.1), "fieldValues": {"insight": "B", "categories": ["Music"]}}],
        }

    def list_collections(self):
        self.calls.append("list_collections")
        return self.colls

    def list_collection_entries(self, cid, page=0, limit=50):
        self.calls.append(("entries", cid))
        rows = self.entries.get(cid, [])
        return {"items": rows[page * limit:(page + 1) * limit], "pagination": {"has_next": False}}

    def get_quest(self, quest_id):
        return {"quest_id": quest_id, "category_ids": ["cat_x"], "category_id": "cat_x"}


def request(client, **spec):
    return CollectRequest(card={"quest_id": "q1"}, card_id="q1", spec={"source": "quest_collections", **spec},
                          since=NOW - timedelta(days=2), now=NOW, client=client)


def test_discovery_is_incremental_and_renders_fields_by_name():
    client = Client()
    updates = QuestCollectionsSource().collect(request(client))
    titles = " | ".join(u.title for u in updates)
    assert "Linked log" in titles and "Category journal" in titles
    assert "Other area" not in titles and "Old" not in titles and "Walk" not in titles
    assert ("entries", "c_old") not in client.calls          # unchanged since the watermark: never downloaded
    linked = next(u for u in updates if u.item_id == "c_link")
    assert "- What happened: Wrote ch. 2" in linked.body and "Mood" not in linked.body
    assert "edited old" in linked.body and "(edited)" in linked.body and "ancient" not in linked.body
    assert linked.verbatim


def test_all_habits_and_named_include_with_category_filter():
    client = Client()
    updates = QuestCollectionsSource().collect(request(client, discover=False, all_habits=True, include=[
        {"name": "Insights list", "categories": ["phd"], "category_field": "categories"}]))
    by = {u.item_id: u for u in updates}
    assert set(by) == {"habits", "ins"}
    assert "- Insight: A" in by["ins"].body and "B" not in by["ins"].body.replace("Insight", "")
    assert "Walk" in by["habits"].body


def test_helpers():
    assert entry_has_category({"fieldValues": {"categories": ["PhD"]}}, {}, ["phd"], "categories")
    assert not entry_has_category({"fieldValues": {}}, {}, ["phd"], "categories")
    assert render_entry({"createdAt": iso(1), "fieldValues": {"a": "", "b": [], "c": {}, "d": "kept"}}, {"d": "Dee"}).endswith("- Dee: kept")


def test_folder_files_since_watermark_excluding_from_quest(tmp_path):
    hc = tmp_path / "human_context"
    (hc / "from_quest").mkdir(parents=True)
    (hc / "from_quest" / "note.md").write_text("synced")
    (hc / "meeting.md").write_text("Transcript excerpt\nmore")
    old = hc / "old.md"
    old.write_text("old")
    os.utime(old, (NOW.timestamp() - 86400 * 20, NOW.timestamp() - 86400 * 20))
    req = CollectRequest(card_id="q1", spec={"source": "folder_files"}, since=datetime.now(timezone.utc) - timedelta(days=1),
                         folder=str(tmp_path))
    found = FolderFilesSource().collect(req)
    assert [u.item_id for u in found] == ["human_context/meeting.md"]
    assert "Transcript excerpt" in found[0].body


def test_per_consumer_watermarks_seed_from_legacy(tmp_path):
    marks = Watermarks(str(tmp_path / "w.json"))
    marks.set("q1", "quest_notes", NOW - timedelta(days=3))
    assert marks.get("q1", "quest_notes", "pass_a") == NOW - timedelta(days=3)       # seeded from the per-quest stamp
    marks.set("q1", "quest_notes", NOW, "pass_a")
    assert marks.get("q1", "quest_notes", "pass_a") == NOW
    assert marks.get("q1", "quest_notes", "pass_b") == NOW - timedelta(days=3)       # another consumer is unaffected
    assert marks.get("q1", "quest_notes") == NOW - timedelta(days=3)


def engine(tmp_path, client):
    return UpdateEngine(client, watermarks=Watermarks(str(tmp_path / "w.json")), pending=PendingMarks(str(tmp_path / "p.json")),
                        always=("quest_collections",), now_fn=lambda: NOW, folder_resolver=lambda q: str(tmp_path))


def test_advance_only_after_success(tmp_path):
    client = Client()
    eng = engine(tmp_path, client)
    bundle = eng.collect({"quest_id": "q1"}, card_id="q1", consumer="ap1", since=None)
    assert bundle.updates
    bundle.mark_seen(task_id="t1")
    assert eng._watermarks.get("q1", "quest_collections", "ap1") is None            # staged only
    assert eng.settle("t1", succeeded=False) is False
    assert eng._watermarks.get("q1", "quest_collections", "ap1") is None            # failed run: the news is kept
    bundle.mark_seen(task_id="t2")
    assert eng.settle("t2", succeeded=True) is True
    assert eng._watermarks.get("q1", "quest_collections", "ap1") == NOW
    assert eng._watermarks.get("q1", "quest_collections", "ap2") is None           # another consumer still sees it


def test_context_file_rolls_newest_first_and_trims(tmp_path):
    client = Client()
    eng = engine(tmp_path, client)
    bundle = eng.collect({"quest_id": "q1"}, card_id="q1", consumer="ap1")
    bundle.collected_at = NOW - timedelta(days=40)
    path = write_context_file(bundle, str(tmp_path), consumer="ap1", now=NOW - timedelta(days=40))
    bundle.collected_at = NOW
    path = write_context_file(bundle, str(tmp_path), consumer="ap1", now=NOW)
    text = (tmp_path / FILE_NAME).read_text()
    assert path == tmp_path / FILE_NAME
    assert text.count("<!-- context-updates-section") == 1                       # the 40-day-old section was trimmed
    assert "## ap1:" in text and "### quest_collections" in text and "Wrote ch. 2" in text
    block = index_block(bundle, path)
    assert str(path) in block and "[U1]" in block and "quest_collections" in block


def test_empty_bundle_writes_nothing(tmp_path):
    eng = UpdateEngine(None, always=(), now_fn=lambda: NOW)
    assert write_context_file(eng.collect({"quest_id": "q1"}, card_id="q1"), str(tmp_path)) is None
    assert not (tmp_path / FILE_NAME).exists()


def test_schedule_counts_activity():
    from quest_ai_runner.intelligence.proactivity.quest_schedule import activity_rows, new_activity_since
    last = iso(1)
    assert new_activity_since([], [{"at": iso(0.1), "kind": "quest_collections"}], last, NOW)
    assert not new_activity_since([], [{"at": iso(3), "kind": "quest_collections"}], last, NOW)


def test_named_includes_survive_an_empty_card_and_file_is_complete(tmp_path):
    """A quest record that failed to read is still the quest: its mapped includes apply, and the file holds what the judges drop."""
    from quest_ai_runner.body.intake_sources.context_updates import consumer_spec_resolver
    client = Client()
    client.get_quest = lambda quest_id, team_id=None: {}
    spec_map = {"q1": [{"source": "quest_collections", "discover": False,
                        "include": [{"id": "ins", "categories": ["PhD"]}]}]}
    eng = UpdateEngine(client, watermarks=Watermarks(None), always=("quest_collections",), now_fn=lambda: NOW,
                       spec_resolver=consumer_spec_resolver(spec_map), relevance_judge=lambda *a, **k: set())
    bundle = eng.collect({}, card_id="q1", consumer="c")
    assert [u.item_id for u in bundle.updates] == ["ins"]
    long_text = "x" * 900
    client.entries["ins"][0]["fieldValues"]["insight"] = long_text
    bundle.complete[0].raw["full_body"] = long_text           # an insight carries its whole text for the file
    path = write_context_file(bundle, str(tmp_path), consumer="c", now=NOW)
    assert long_text in path.read_text()


def test_judged_out_items_still_reach_the_file(tmp_path):
    from quest_ai_runner.body.intake_sources.context_update_types import ContextUpdate
    from quest_ai_runner.body.intake_sources.context_update_bundle import ContextUpdates
    kept = ContextUpdate(source="a", title="kept", body="short", occurred_at=NOW, ref="U1")
    dropped = ContextUpdate(source="insights", title="dropped by a judge", body="clipped [...truncated]",
                            raw={"full_body": "the whole insight"}, occurred_at=NOW)
    bundle = ContextUpdates(card_id="q1", collected_at=NOW, updates=[kept], complete=[kept, dropped])
    text = write_context_file(bundle, str(tmp_path), consumer="c", now=NOW).read_text()
    assert "the whole insight" in text and "[...truncated]" not in text and "2 item(s)" in text


def test_extra_fields_render_by_name():
    entry = {"createdAt": iso(1), "fieldValues": {"extra_fields": {"field_001": "Preparing for a church meal", "field_9": ""}}}
    assert "- Title: Preparing for a church meal" in render_entry(entry, {"field_001": "Title"})
