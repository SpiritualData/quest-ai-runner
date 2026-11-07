"""Offline tests of the production chat arm's guards, stream parsing and fingerprint (no network, no model)."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prodchat as PC  # noqa: E402

BASE, EMAIL, UID = "https://api.prod.example.org", "ai@example.org", "u123"


@pytest.fixture(autouse=True)
def target(monkeypatch):
    for key, value in {"QAR_PROD_BASE_URL": BASE, "QAR_PROD_EXPECT_ACCOUNT": EMAIL, "QAR_PROD_EXPECT_USER_ID": UID, "QAR_PROD_QUEST_ID": "quest_x", "QAR_PROD_TEAM_ID": "team_x"}.items():
        monkeypatch.setenv(key, value)


def test_stream_parsing_keeps_order_skips_done_marker_and_counts_calls():
    lines = ['data: {"event": "start"}', "", 'data: {"event": "interim", "text": "Looking now"}', 'data: {"event": "tokens", "data": {"tokens_in": 10, "tokens_out": 2}}',
             "data: not json", 'data: {"event": "tokens", "data": {"tokens_in": 30, "tokens_out": 9}}', 'data: {"event": "tokens", "data": {"tokens_in": 30, "tokens_out": 9, "final": true}}',
             'data: {"event": "done", "content": "The answer"}', "data: [DONE]"]
    events = list(PC.parse_sse(lines))
    assert [PC.event_name(e) for e in events] == ["start", "interim", "tokens", "_unparsed", "tokens", "tokens", "done"]
    assert PC.usage_of(events) == {"model_calls": 2, "tokens_in": 30, "tokens_out": 9}
    assert [f["type"] for f in PC.frames_of(events)][1] == "interim"
    assert PC.route_of(events) == "inline"


def test_account_guard_refuses_any_other_account_or_base_url():
    assert PC.check_account({"user_id": UID, "email": EMAIL})["email"] == EMAIL
    for who in ({"user_id": "other", "email": EMAIL}, {"user_id": UID, "email": "someone@example.org"}, {}, {"error": "boom"}):
        with pytest.raises(PC.GuardRefusal):
            PC.check_account(who)
    PC.check_base_url(BASE + "/")
    for url in ("https://api.dev.example.org", "", BASE + ".evil.org"):
        with pytest.raises(PC.GuardRefusal):
            PC.check_base_url(url)


def test_the_client_makes_no_write_but_its_own_conversations():
    client = PC.ProdClient(BASE, "k")
    client.allow_write("POST", "/api/quest-ai/conversations")
    for method, path in (("POST", "/api/quest-ai/conversations/qaconv_1/messages/stream"), ("DELETE", "/api/quests/quest_x/notes/n1"), ("POST", "/api/quests/quest_x/notes")):
        with pytest.raises(PC.GuardRefusal):
            client.allow_write(method, path)
    client.own_conversations.add("qaconv_1")
    client.allow_write("POST", "/api/quest-ai/conversations/qaconv_1/messages/stream")
    with pytest.raises(PC.GuardRefusal):
        client.allow_write("POST", "/api/quest-ai/conversations/qaconv_2/stop")


def test_id_whitelist_refuses_other_ids_and_other_text(tmp_path):
    f = tmp_path / "p.jsonl"
    f.write_text(json.dumps({"case_id": "BAD-02", "input_text": "approved text"}) + "\n")
    allowed = PC.load_allowed_cases(f)
    assert PC.check_case({"id": "BAD-02", "message": " approved text "}, allowed) == "approved text"
    for case in ({"id": "BAD-99", "message": "approved text"}, {"id": "BAD-02", "message": "edited text"}):
        with pytest.raises(PC.GuardRefusal):
            PC.check_case(case, allowed)


def reads(**over):
    base = {"notes": [{"note_id": "n1", "text": "a", "updated_at": "1"}], "goals": [{"id": "g1", "name": "G", "completed": False}], "workflows": [], "spec_records": [{"recordId": "r1", "revision": 1, "fields": {"a": 1}}],
            "tasks": [{"task_id": "t1", "title": "old"}], "decisions": [], "conversations": [{"id": "c1", "title": "old chat"}]}
    base.update(over)
    return base


def test_fingerprint_detects_an_unexpected_write_and_allows_an_expected_one():
    before = PC.fingerprint_from(reads())
    after = PC.fingerprint_from(reads(notes=[{"note_id": "n1", "text": "a", "updated_at": "1"}, {"note_id": "n2", "text": "new"}],
                                      conversations=[{"id": "c1"}, {"id": "own"}]))
    changes = PC.diff_fingerprints(before, after, own_conv="own")
    assert [(c["category"], c["change"], c["id"]) for c in changes] == [("notes", "added", "n2")]      # the run's own conversation is not a change
    lines = PC.unexpected_writes(changes, {"allow": []}, zero_writes=True)
    assert len(lines) == 1 and lines[0].startswith("UNEXPECTED WRITE: notes added n2")
    assert PC.unexpected_writes(changes, {"allow": [{"tool": "add_quest_note", "args": {}}]}) == []             # explained by meaning: a note tool allows a note
    assert PC.unexpected_writes(changes, {"allow": [{"tool": "set_goal_strategy_link"}]})                       # a goal tool does not explain a note
    assert PC.unexpected_writes(changes, {"allow": [{"tool": "add_quest_note"}]}, zero_writes=True)            # a zero-writes case explains nothing


def test_changed_removed_and_queued_items_are_all_seen():
    before = PC.fingerprint_from(reads())
    after = PC.fingerprint_from(reads(goals=[], spec_records=[{"recordId": "r1", "revision": 2, "fields": {"a": 2}}], tasks=[{"task_id": "t1"}, {"task_id": "t2", "conv_id": "own"}],
                                      decisions=[{"decision_id": "d1", "conv_id": "own"}]))
    kinds = {(c["category"], c["change"]) for c in PC.diff_fingerprints(before, after, own_conv="own")}
    assert kinds == {("goals", "removed"), ("spec_records", "changed"), ("tasks", "added"), ("decisions", "added")}
    assert PC.diff_fingerprints(before, before) == []
