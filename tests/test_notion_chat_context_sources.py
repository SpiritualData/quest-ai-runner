"""The ``notion_database`` and ``google_chat`` context sources, and the engine behaviour they need.

What this file pins down:

  * THE WATERMARK IS HONEST. A source only advances when a run was handed what it found, a failed
    read never advances it, and a read-only store never writes anything (including snapshots).
  * WHICH PROPERTIES CHANGED, NOT JUST THAT A ROW DID: a Status change and a new Deadline Date arrive
    as ``Status: Not started -> In progress`` / ``Deadline Date: (empty) -> 2026-10-01``, worked out
    against the snapshot stored beside the watermark, which survives a restart. Where it cannot
    know, it says so.
  * NOTION'S MINUTE-ROUNDED EDIT TIMES DO NOT LOSE AN EDIT made inside the watermark's own minute.
  * CHAT SPACES ARE READ ONLY IF NAMED BY THE CARD AND ALLOWLISTED. A space that is not, is refused
    before any request is made. The assistant's own (and any Chat app's) messages are skipped.
  * UNCONFIGURED IS AN HONEST GAP, never an exception, and never hides the rest of the bundle.

Offline: Notion is ``tests/notion_fake.py``; Google Chat is a small fake below.
"""
import json
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from quest_ai_runner.adapters.google_chat_adapter import (
    ChatReadError,
    GoogleChatAdapter,
    static_token_provider as chat_token,
)
from quest_ai_runner.adapters.notion_adapter import NotionAdapter, static_token_provider
from quest_ai_runner.runner.context_updates import (
    CollectRequest,
    GoogleChatSource,
    NotionDatabaseSource,
    SourceGap,
    UpdateEngine,
    Watermarks,
)

from .notion_fake import DB_ID, FakeNotion, Resp, make_page, minute_rounded

PAGE_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PAGE_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
PAGE_C = "cccccccccccccccccccccccccccccccc"


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Clock:
    def __init__(self, now):
        self.now = at(now)

    def __call__(self):
        return self.now

    def set(self, now):
        self.now = at(now)


def card(*specs):
    return {"quest_id": "quest_1", "name": "The study", "context_sources": list(specs)}


# ================================================================================================
# Notion
# ================================================================================================

@pytest.fixture
def notion(monkeypatch):
    fake = FakeNotion()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def notion_adapter():
    return NotionAdapter(token_provider=static_token_provider("test-token"),
                         database_ids={"tasks": DB_ID})


def engine_for(tmp_path, clock, **kw):
    kw.setdefault("notion", notion_adapter())
    return UpdateEngine(None, watermarks=Watermarks(str(tmp_path / "wm.json")), now_fn=clock,
                        always=(), **kw)


NOTION_CARD = card({"source": "notion_database", "database": "tasks"})


def edit(fake, page_id, edited, **changes):
    """Edit a row the way Notion does: new property values and a new (minute-rounded) edit time."""
    page = fake.pages[page_id]
    page["last_edited_time"] = minute_rounded(edited)
    for name, value in changes.items():
        name = name.replace("_", " ")
        prop = page["properties"][name]
        if prop["type"] == "status":
            prop["status"] = {"name": value} if value else None
        elif prop["type"] == "date":
            prop["date"] = {"start": value} if value else None
        elif prop["type"] == "title":
            prop["title"] = [{"type": "text", "plain_text": value}]


def test_first_look_separates_created_rows_from_old_rows_edited_in_the_window(notion, tmp_path):
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-19T09:00:00Z", edited="2026-09-19T09:00:00Z"))
    notion.add(make_page(PAGE_B, "Recruit participants", status="In progress",
                         created="2026-01-01T00:00:00Z", edited="2026-09-18T08:00:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    bundle = engine_for(tmp_path, clock).collect(NOTION_CARD, card_id="quest_1")

    by_id = {u.item_id: u for u in bundle.updates}
    assert by_id[PAGE_A].kind == "created"
    assert by_id[PAGE_A].title == '"Write the methods section" was added in tasks'
    assert "Status: Not started" in by_id[PAGE_A].body
    assert by_id[PAGE_B].kind == "edited"
    assert "no earlier snapshot" in by_id[PAGE_B].body and "unknown" in by_id[PAGE_B].body
    assert by_id[PAGE_A].url.endswith(PAGE_A) and by_id[PAGE_A].location == "tasks"
    assert not any(u.needs_response for u in bundle.updates)


def test_a_status_change_and_a_new_deadline_arrive_as_named_property_changes(notion, tmp_path):
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-19T09:00:00Z", edited="2026-09-19T09:00:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    engine = engine_for(tmp_path, clock)
    first = engine.collect(NOTION_CARD, card_id="quest_1")
    first.mark_seen()

    edit(notion, PAGE_A, "2026-09-21T09:10:00Z", Status="In progress", Deadline_Date="2026-10-01")
    notion.add(make_page(PAGE_C, "Book the lab", created="2026-09-21T10:00:00Z",
                         edited="2026-09-21T10:00:00Z"))
    clock.set("2026-09-21T12:00:00Z")
    second = engine.collect(NOTION_CARD, card_id="quest_1")

    by_id = {u.item_id: u for u in second.updates}
    assert set(by_id) == {PAGE_A, PAGE_C}
    changed = by_id[PAGE_A]
    assert changed.kind == "changed"
    assert "Status: Not started -> In progress" in changed.body
    assert "Deadline Date: (empty) -> 2026-10-01" in changed.body
    assert changed.title == '"Write the methods section" changed in tasks'
    assert by_id[PAGE_C].kind == "created"


def test_nothing_new_is_reported_as_nothing_new_and_a_repeat_delivers_nothing_twice(notion, tmp_path):
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-19T09:00:00Z", edited="2026-09-19T09:00:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    engine = engine_for(tmp_path, clock)
    engine.collect(NOTION_CARD, card_id="quest_1").mark_seen()

    clock.set("2026-09-22T12:00:00Z")
    quiet = engine.collect(NOTION_CARD, card_id="quest_1")
    assert quiet.updates == []
    assert "no rows were created or edited" in quiet.reports[0].explanation
    assert "no rows were created or edited" in quiet.checked_line()


def test_an_edit_that_changed_no_listed_property_says_so_instead_of_inventing_a_diff(notion, tmp_path):
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-19T09:00:00Z", edited="2026-09-19T09:00:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    engine = engine_for(tmp_path, clock)
    engine.collect(NOTION_CARD, card_id="quest_1").mark_seen()

    edit(notion, PAGE_A, "2026-09-21T09:10:00Z")              # body edit: properties untouched
    clock.set("2026-09-21T12:00:00Z")
    (update,) = engine.collect(NOTION_CARD, card_id="quest_1").updates
    assert update.kind == "edited" and "none of the properties shown here changed" in update.body


def test_an_edit_inside_the_watermarks_own_minute_is_not_lost(notion, tmp_path):
    """Notion stores 10:05:45 as 10:05:00; a filter at the watermark 10:05:30 must still find it."""
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-01T00:00:00Z", edited="2026-09-19T09:00:00Z"))
    clock = Clock("2026-09-20T10:05:30Z")
    engine = engine_for(tmp_path, clock)
    engine.collect(NOTION_CARD, card_id="quest_1").mark_seen()

    edit(notion, PAGE_A, "2026-09-20T10:05:45Z", Status="Done")   # stored as 10:05:00
    clock.set("2026-09-20T10:30:00Z")
    (update,) = engine.collect(NOTION_CARD, card_id="quest_1").updates
    assert "Status: Not started -> Done" in update.body


def test_the_snapshot_survives_a_restart_and_a_read_only_store_writes_nothing(notion, tmp_path):
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-19T09:00:00Z", edited="2026-09-19T09:00:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    path = str(tmp_path / "wm.json")

    # a read-only engine (the inspection path) collects and "marks seen": nothing persists
    looker = UpdateEngine(None, watermarks=Watermarks(path, read_only=True), now_fn=clock,
                          always=(), notion=notion_adapter())
    looker.collect(NOTION_CARD, card_id="quest_1").mark_seen()
    assert Watermarks(path).get_snapshot("quest_1", "notion_database", DB_ID) is None

    real = UpdateEngine(None, watermarks=Watermarks(path), now_fn=clock, always=(),
                        notion=notion_adapter())
    real.collect(NOTION_CARD, card_id="quest_1").mark_seen()
    reloaded = Watermarks(path)
    stored = reloaded.get_snapshot("quest_1", "notion_database", DB_ID)["rows"][PAGE_A]
    assert stored["props"]["Status"] == "Not started" and stored["title"] == "Write the methods section"

    edit(notion, PAGE_A, "2026-09-21T09:10:00Z", Status="Done")
    clock.set("2026-09-21T12:00:00Z")
    restarted = UpdateEngine(None, watermarks=Watermarks(path), now_fn=clock, always=(),
                             notion=notion_adapter())
    (update,) = restarted.collect(NOTION_CARD, card_id="quest_1").updates
    assert "Status: Not started -> Done" in update.body


def test_a_failed_read_is_reported_and_advances_neither_the_watermark_nor_the_snapshot(notion, tmp_path):
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-19T09:00:00Z", edited="2026-09-19T09:00:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    engine = engine_for(tmp_path, clock)
    notion.fail_with = urllib.error.HTTPError("u", 500, "boom", {}, None)
    bundle = engine.collect(NOTION_CARD, card_id="quest_1")
    assert bundle.updates == [] and not bundle.reports[0].ok
    assert "NotionError" in bundle.reports[0].error and "500" in bundle.reports[0].error
    bundle.mark_seen()
    wm = Watermarks(str(tmp_path / "wm.json"))
    assert wm.get("quest_1", "notion_database") is None
    assert wm.get_snapshot("quest_1", "notion_database", DB_ID) is None


def test_more_rows_than_the_limit_is_stated_not_hidden(notion, tmp_path):
    for i in range(8):
        notion.add(make_page(f"{i:032x}", f"Row {i}", created="2026-09-19T09:00:00Z",
                             edited=f"2026-09-19T09:{i:02d}:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    bundle = engine_for(tmp_path, clock).collect(
        card({"source": "notion_database", "database": "tasks", "max_rows": 3}), card_id="quest_1")
    assert len(bundle.updates) == 3
    assert "only the 3 most recently edited" in bundle.reports[0].explanation


def test_unconfigured_notion_is_an_honest_gap_and_does_not_cost_the_other_sources(tmp_path):
    clock = Clock("2026-09-20T12:00:00Z")
    engine = UpdateEngine(None, watermarks=Watermarks(None), now_fn=clock, notion=None)
    bundle = engine.collect(NOTION_CARD, card_id="quest_1")
    (report,) = [r for r in bundle.reports if r.source == "notion_database"]
    assert not report.ok and report.error.startswith("notion is not configured")
    assert "could not read: notion is not configured" in bundle.checked_line()
    assert len(bundle.reports) > 1                       # the always-on sources still ran


def test_a_database_that_is_not_configured_is_refused_and_names_what_is(notion, tmp_path):
    clock = Clock("2026-09-20T12:00:00Z")
    bundle = engine_for(tmp_path, clock).collect(
        card({"source": "notion_database", "database": "someone-elses"}), card_id="quest_1")
    assert "not one of the configured databases" in bundle.reports[0].error
    assert "configured: tasks" in bundle.reports[0].error
    assert notion.requests == []


def test_a_spec_naming_no_database_is_a_gap(notion, tmp_path):
    bundle = engine_for(tmp_path, Clock("2026-09-20T12:00:00Z")).collect(
        card({"source": "notion_database"}), card_id="quest_1")
    assert "names no database" in bundle.reports[0].error


def test_notion_source_is_read_only_on_the_wire(notion, tmp_path):
    notion.add(make_page(PAGE_A, "Write the methods section", status="Not started",
                         created="2026-09-19T09:00:00Z", edited="2026-09-19T09:00:00Z"))
    clock = Clock("2026-09-20T12:00:00Z")
    engine = engine_for(tmp_path, clock)
    engine.collect(NOTION_CARD, card_id="quest_1").mark_seen()
    assert notion.writes == [] and all(
        m == "GET" or p.endswith("/query") for m, p, _b in notion.requests)


# ================================================================================================
# Google Chat
# ================================================================================================

class FakeChat:
    """Google Chat's REST surface for spaces and messages, in memory. Records every request."""

    def __init__(self):
        self.messages = {}            # space -> [message dict]
        self.display = {}             # space -> displayName
        self.requests = []            # (method, url)
        self.fail_with = None

    def say(self, space, thread, sender, text, created, *, sender_type="HUMAN", name=None):
        n = len(self.messages.setdefault(space, [])) + 1
        self.messages[space].append({
            "name": f"{space}/messages/m{n}", "createTime": created, "text": text,
            "thread": {"name": f"{space}/threads/{thread}"},
            "sender": {"name": name or f"users/{sender}", "displayName": sender, "type": sender_type},
        })

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.requests.append((req.get_method(), url))
        if self.fail_with is not None:
            raise self.fail_with
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path.replace("/v1/", "", 1)
        if path.endswith("/messages"):
            space = path[:-len("/messages")]
            rows = list(self.messages.get(space, []))
            flt = params.get("filter")
            if flt:
                assert flt.startswith('createTime > "') and flt.endswith('"'), flt
                cutoff = at(flt[len('createTime > "'):-1])
                rows = [m for m in rows if at(m["createTime"]) > cutoff]
            assert params.get("orderBy") == "createTime desc"
            rows.sort(key=lambda m: m["createTime"], reverse=True)
            size = int(params["pageSize"])
            start = int(params.get("pageToken") or 0)
            chunk = rows[start:start + size]
            body = {"messages": chunk}
            if start + size < len(rows):
                body["nextPageToken"] = str(start + size)
            return Resp(body)
        return Resp({"name": path, "displayName": self.display.get(path, "")})


SPACE = "spaces/AAAA1111"
OTHER_SPACE = "spaces/ZZZZ9999"


@pytest.fixture
def chat(monkeypatch):
    fake = FakeChat()
    fake.display[SPACE] = "Study team"
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def chat_adapter(**kw):
    kw.setdefault("space_names", [SPACE])
    kw.setdefault("assistant_senders", ["users/assistant1"])
    return GoogleChatAdapter(token_provider=chat_token("test-token"), **kw)


def chat_engine(tmp_path, clock, adapter=None):
    return UpdateEngine(None, watermarks=Watermarks(str(tmp_path / "wm.json")), now_fn=clock,
                        always=(), google_chat=adapter if adapter is not None else chat_adapter())


CHAT_CARD = card({"source": "google_chat", "spaces": [SPACE]})


def test_new_messages_are_grouped_by_thread_with_the_assistants_own_skipped(chat, tmp_path):
    chat.say(SPACE, "t1", "Ana", "Should we move the survey to Friday?", "2026-09-20T09:00:00Z")
    chat.say(SPACE, "t1", "Ben", "Friday works for me.", "2026-09-20T09:05:00Z")
    chat.say(SPACE, "t2", "Cho", "Lab booked for Monday.", "2026-09-20T10:00:00Z")
    chat.say(SPACE, "t2", "Summary Bot", "Daily digest posted.", "2026-09-20T10:01:00Z", sender_type="BOT")
    chat.say(SPACE, "t2", "Quest AI", "I moved it.", "2026-09-20T10:02:00Z", name="users/assistant1")
    chat.say(SPACE, "t3", "Dee", "", "2026-09-20T10:03:00Z")
    clock = Clock("2026-09-20T12:00:00Z")
    bundle = chat_engine(tmp_path, clock).collect(CHAT_CARD, card_id="quest_1")

    rows = {u.item_id: u for u in bundle.updates}
    assert set(rows) == {f"{SPACE}/threads/t1", f"{SPACE}/threads/t2"}
    t1 = rows[f"{SPACE}/threads/t1"]
    assert t1.title == "2 new message(s) in Study team" and t1.location == "Study team"
    assert "Ana: Should we move the survey to Friday? | Ben: Friday works for me." in t1.body
    t2 = rows[f"{SPACE}/threads/t2"]
    assert "Cho: Lab booked for Monday." in t2.body
    assert "Summary Bot" not in t2.body and "I moved it" not in t2.body
    assert t1.verbatim and not t1.needs_response and "read-only" in t1.how_to_respond
    assert bundle.reports[0].considered == 6
    assert bundle.reports[0].explanation == (
        "6 message(s) across 1 space(s), 2 written by this assistant or a Chat app, skipped, "
        "1 with no text, skipped")


def test_the_watermark_moves_only_when_delivered_and_a_late_message_waits_for_the_next_look(chat, tmp_path):
    chat.say(SPACE, "t1", "Ana", "First.", "2026-09-20T09:00:00Z")
    chat.say(SPACE, "t1", "Ben", "Arrived after the look began.", "2026-09-20T12:00:30Z")
    clock = Clock("2026-09-20T12:00:00Z")
    engine = chat_engine(tmp_path, clock)

    undelivered = engine.collect(CHAT_CARD, card_id="quest_1")
    assert "First." in undelivered.updates[0].body and "after the look" not in undelivered.updates[0].body
    # not marked seen: the same message is offered again
    again = engine.collect(CHAT_CARD, card_id="quest_1")
    assert len(again.updates) == 1 and "First." in again.updates[0].body
    again.mark_seen()

    clock.set("2026-09-20T13:00:00Z")
    chat.say(SPACE, "t2", "Cho", "Newer still.", "2026-09-20T12:30:00Z")
    later = engine.collect(CHAT_CARD, card_id="quest_1")
    text = " ".join(u.body for u in later.updates)
    assert "Arrived after the look began." in text and "Newer still." in text and "First." not in text
    later.mark_seen()
    clock.set("2026-09-20T14:00:00Z")
    assert engine.collect(CHAT_CARD, card_id="quest_1").updates == []


def test_a_space_not_on_the_allowlist_is_refused_before_any_request(chat, tmp_path):
    chat.say(OTHER_SPACE, "t1", "Eve", "Private.", "2026-09-20T09:00:00Z")
    clock = Clock("2026-09-20T12:00:00Z")
    bundle = chat_engine(tmp_path, clock).collect(
        card({"source": "google_chat", "spaces": [OTHER_SPACE]}), card_id="quest_1")
    assert bundle.updates == [] and not bundle.reports[0].ok
    assert bundle.reports[0].error.startswith("refused: " + OTHER_SPACE)
    assert chat.requests == []


def test_a_mixed_spec_reads_only_the_allowlisted_space_and_says_what_it_refused(chat, tmp_path):
    chat.say(SPACE, "t1", "Ana", "Hello.", "2026-09-20T09:00:00Z")
    chat.say(OTHER_SPACE, "t1", "Eve", "Private.", "2026-09-20T09:00:00Z")
    clock = Clock("2026-09-20T12:00:00Z")
    bundle = chat_engine(tmp_path, clock).collect(
        card({"source": "google_chat", "spaces": [SPACE, OTHER_SPACE]}), card_id="quest_1")
    assert [u.body for u in bundle.updates] == ["Ana: Hello."]
    assert all(OTHER_SPACE not in url for _m, url in chat.requests)
    assert "1 named space(s) refused" in bundle.reports[0].explanation


def test_a_space_the_card_does_not_name_is_never_read_even_though_allowlisted(chat, tmp_path):
    chat.say(SPACE, "t1", "Ana", "Hello.", "2026-09-20T09:00:00Z")
    chat.say(OTHER_SPACE, "t1", "Eve", "Also allowed, not named.", "2026-09-20T09:00:00Z")
    clock = Clock("2026-09-20T12:00:00Z")
    adapter = chat_adapter(space_names=[SPACE, OTHER_SPACE])
    bundle = chat_engine(tmp_path, clock, adapter).collect(CHAT_CARD, card_id="quest_1")
    assert [u.body for u in bundle.updates] == ["Ana: Hello."]
    assert all(OTHER_SPACE not in url for _m, url in chat.requests)


def test_with_no_allowlist_nothing_is_read_by_the_typed_path(chat, tmp_path):
    adapter = GoogleChatAdapter(token_provider=chat_token("t"))
    assert adapter.space_allowed(SPACE) is False
    with pytest.raises(ChatReadError, match="refused"):
        adapter.fetch_messages_since(SPACE, None)
    bundle = chat_engine(tmp_path, Clock("2026-09-20T12:00:00Z"), adapter).collect(
        CHAT_CARD, card_id="quest_1")
    assert "refused" in bundle.reports[0].error and chat.requests == []


def test_a_failed_read_is_reported_and_the_watermark_stays(chat, tmp_path):
    chat.say(SPACE, "t1", "Ana", "Hello.", "2026-09-20T09:00:00Z")
    chat.fail_with = urllib.error.HTTPError("u", 403, "no", {}, None)
    clock = Clock("2026-09-20T12:00:00Z")
    engine = chat_engine(tmp_path, clock)
    bundle = engine.collect(CHAT_CARD, card_id="quest_1")
    assert not bundle.reports[0].ok and "403" in bundle.reports[0].error
    bundle.mark_seen()
    assert Watermarks(str(tmp_path / "wm.json")).get("quest_1", "google_chat") is None


def test_unconfigured_chat_is_an_honest_gap(tmp_path):
    engine = UpdateEngine(None, watermarks=Watermarks(None),
                          now_fn=Clock("2026-09-20T12:00:00Z"), google_chat=None)
    bundle = engine.collect(CHAT_CARD, card_id="quest_1")
    (report,) = [r for r in bundle.reports if r.source == "google_chat"]
    assert not report.ok and report.error.startswith("google chat is not configured")


def test_a_spec_with_no_spaces_is_a_gap(chat, tmp_path):
    bundle = chat_engine(tmp_path, Clock("2026-09-20T12:00:00Z")).collect(
        card({"source": "google_chat"}), card_id="quest_1")
    assert "names no spaces" in bundle.reports[0].error


def test_chat_output_is_bounded_and_says_what_was_left_out(chat, tmp_path):
    for i in range(13):
        chat.say(SPACE, f"t{i}", "Ana", f"Thread {i}", f"2026-09-20T09:{i:02d}:00Z")
    for i in range(40):
        chat.say(SPACE, "busy", "Ben", "x" * 900, f"2026-09-20T10:{i:02d}:00Z")
    clock = Clock("2026-09-20T12:00:00Z")
    bundle = chat_engine(tmp_path, clock).collect(CHAT_CARD, card_id="quest_1")
    assert len(bundle.updates) == 10
    assert "older thread(s) not shown" in bundle.reports[0].explanation
    busy = next(u for u in bundle.updates if u.item_id.endswith("/busy"))
    assert busy.body.startswith("[10 earlier message(s) in this thread not shown]")
    assert busy.raw["messages"] == 40


def test_chat_truncation_at_the_cap_is_stated(chat, tmp_path):
    for i in range(12):
        chat.say(SPACE, "t1", "Ana", f"m{i}", f"2026-09-20T09:{i:02d}:00Z")
    bundle = chat_engine(tmp_path, Clock("2026-09-20T12:00:00Z")).collect(
        card({"source": "google_chat", "spaces": [SPACE], "max_messages": 5}), card_id="quest_1")
    assert "oldest in the window were left out" in bundle.reports[0].explanation
    assert "m11" in bundle.updates[0].body and "m0" not in bundle.updates[0].body


def test_the_chat_read_path_only_ever_issues_gets(chat, tmp_path):
    chat.say(SPACE, "t1", "Ana", "Hello.", "2026-09-20T09:00:00Z")
    engine = chat_engine(tmp_path, Clock("2026-09-20T12:00:00Z"))
    engine.collect(CHAT_CARD, card_id="quest_1").mark_seen()
    adapter = chat_adapter()
    adapter.fetch_messages_since(SPACE, None, max_messages=10)
    assert {m for m, _u in chat.requests} == {"GET"}


def test_fetch_messages_since_asks_the_api_for_only_newer_messages(chat):
    chat.say(SPACE, "t1", "Ana", "Old.", "2026-09-10T09:00:00Z")
    chat.say(SPACE, "t1", "Ana", "New.", "2026-09-20T09:00:00Z")
    result = chat_adapter().fetch_messages_since(SPACE, at("2026-09-15T00:00:00Z"))
    assert [m["text"] for m in result.messages] == ["New."]
    assert result.display_name == "Study team" and not result.truncated


# ================================================================================================
# Registration
# ================================================================================================

def test_both_sources_are_registered_built_ins_with_discoverable_descriptions():
    described = UpdateEngine().describe_sources()
    assert "rows created or edited in a Notion database" in described["notion_database"]
    assert "Google Chat spaces" in described["google_chat"]


def test_neither_source_is_always_on_or_relevance_judged_or_ask_tracking():
    engine = UpdateEngine()
    assert "notion_database" not in engine.specs_for({}) and "google_chat" not in str(engine.specs_for({}))
    for src in (NotionDatabaseSource(), GoogleChatSource()):
        assert not src.judge_relevance and not src.tracks_asks and not src.judge_admission
        assert not src.relay_to_quest


def test_a_source_gap_carries_its_message_unprefixed_and_is_not_an_exception_to_the_bundle():
    request = CollectRequest(spec={"source": "google_chat"})
    with pytest.raises(SourceGap):
        GoogleChatSource(None).collect(request)
