"""Opt-in unrestricted Google Chat (``all_spaces = true``): config, adapter and context source.

Fake HTTP only, no network. The safe default (an explicit allowlist) stays exactly as it was; these
tests cover the explicit opt-in and prove the restricted mode still refuses "all".
"""
import logging
import urllib.parse
from datetime import datetime

import pytest

from quest_ai_runner.adapters.google_chat_adapter import (
    ChatReadError, GoogleChatAdapter, space_label, static_token_provider as chat_token)
from quest_ai_runner.config import RunnerConfig, resolve_config_objects
from quest_ai_runner.runner.context_updates import UpdateEngine, Watermarks
from .notion_fake import Resp

S_TEAM = "spaces/AAAA1111"
S_DM = "spaces/DMDM2222"
S_GROUP = "spaces/GRGR3333"
S_OLD = "spaces/OLDO4444"


def at(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class Clock:
    def __init__(self, now):
        self.now = at(now)

    def __call__(self):
        return self.now


class FakeChat:
    def __init__(self, page_size=2):
        self.spaces = []              # dicts as the API returns them
        self.messages = {}
        self.requests = []
        self.page_size = page_size

    def add_space(self, name, display=None, kind="SPACE", active=None):
        sp = {"name": name, "spaceType": kind}
        if display:
            sp["displayName"] = display
        if active:
            sp["lastActiveTime"] = active
        self.spaces.append(sp)

    def say(self, space, sender, text, created, *, sender_type="HUMAN"):
        n = len(self.messages.setdefault(space, [])) + 1
        self.messages[space].append({
            "name": f"{space}/messages/m{n}", "createTime": created, "text": text,
            "thread": {"name": f"{space}/threads/t{n}"},
            "sender": {"name": f"users/{sender}", "displayName": sender, "type": sender_type}})

    def __call__(self, req, timeout=None):
        self.requests.append((req.get_method(), req.full_url))
        parsed = urllib.parse.urlparse(req.full_url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        path = parsed.path.replace("/v1/", "", 1)
        if path == "spaces":
            start = int(params.get("pageToken") or 0)
            chunk = self.spaces[start:start + self.page_size]
            body = {"spaces": chunk}
            if start + self.page_size < len(self.spaces):
                body["nextPageToken"] = str(start + self.page_size)
            return Resp(body)
        if path.endswith("/messages"):
            space = path[:-len("/messages")]
            rows = list(self.messages.get(space, []))
            flt = params.get("filter")
            if flt:
                cutoff = at(flt[len('createTime > "'):-1])
                rows = [m for m in rows if at(m["createTime"]) > cutoff]
            rows.sort(key=lambda m: m["createTime"], reverse=True)
            return Resp({"messages": rows[:int(params["pageSize"])]})
        info = next((s for s in self.spaces if s["name"] == path), {"name": path})
        return Resp(info)


@pytest.fixture
def chat(monkeypatch):
    fake = FakeChat()
    fake.add_space(S_TEAM, "Study team", active="2026-09-20T09:00:00Z")
    fake.add_space(S_DM, None, "DIRECT_MESSAGE", active="2026-09-20T09:30:00Z")
    fake.add_space(S_GROUP, None, "GROUP_CHAT", active="2026-09-20T10:00:00Z")
    fake.add_space(S_OLD, "Quiet room", active="2026-09-01T09:00:00Z")
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def adapter(**kw):
    return GoogleChatAdapter(token_provider=chat_token("t"), assistant_senders=["users/assistant1"], **kw)


def engine(tmp_path, ad):
    return UpdateEngine(None, watermarks=Watermarks(str(tmp_path / "wm.json")),
                        now_fn=Clock("2026-09-20T12:00:00Z"), always=(), google_chat=ad)


def card(*specs):
    return {"quest_id": "quest_1", "name": "The study", "context_sources": list(specs)}


ALL = card({"source": "google_chat"})


# --- config -------------------------------------------------------------------------------------

@pytest.fixture
def sa_file(tmp_path):
    path = tmp_path / "sa.json"
    path.write_text("{}")
    return str(path)


def test_all_spaces_wires_without_space_names_and_logs_it(sa_file, monkeypatch, caplog):
    monkeypatch.setattr("quest_ai_runner.adapters.google_chat_adapter.service_account_token_provider",
                        lambda **kw: (lambda: "tok"))
    with caplog.at_level(logging.INFO):
        cfg = resolve_config_objects(RunnerConfig(google_chat={
            "service_account_file": sa_file, "all_spaces": True}))
    ad = cfg.google_chat_adapter
    assert isinstance(ad, GoogleChatAdapter) and ad.all_spaces
    assert "Google Chat wired (read-only, ALL spaces the subject belongs to)" in caplog.text


def test_default_still_fails_closed_and_all_spaces_false_does_not_opt_in(sa_file, caplog):
    for block in ({"service_account_file": sa_file},
                  {"service_account_file": sa_file, "all_spaces": False}):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            cfg = resolve_config_objects(RunnerConfig(google_chat=block))
        assert cfg.google_chat_adapter is None and "space_names" in caplog.text


# --- adapter ------------------------------------------------------------------------------------

def test_space_allowed_unrestricted_accepts_well_formed_names_only():
    ad = adapter(all_spaces=True)
    assert ad.space_allowed(S_DM) and ad.space_allowed("spaces/abc_-9")
    for bad in ("", "AAAA", "spaces/", "spaces/a/b", "users/AAAA", "spaces/a b", None):
        assert not ad.space_allowed(bad)


def test_space_allowed_default_is_the_allowlist():
    ad = adapter(space_names=[S_TEAM])
    assert ad.space_allowed(S_TEAM) and not ad.space_allowed(S_DM)
    assert not adapter().space_allowed(S_TEAM)


def test_enumeration_paginates_and_labels_dms_and_group_chats(chat):
    result = adapter(all_spaces=True).list_member_spaces(max_spaces=10)
    assert result.error is None and not result.truncated
    by = {s["name"]: s for s in result.spaces}
    assert len(by) == 4 and len([r for r in chat.requests if "pageToken" in r[1]]) >= 1
    assert by[S_TEAM]["displayName"] == "Study team" and by[S_TEAM]["label"] == "Study team"
    assert by[S_DM]["displayName"] is None and by[S_DM]["label"] == "direct message DMDM22"
    assert by[S_GROUP]["label"] == "group chat GRGR33"
    assert by[S_TEAM]["lastActiveTime"] == "2026-09-20T09:00:00Z"
    assert all(s["label"].strip() for s in result.spaces)


def test_enumeration_respects_the_cap_and_says_so(chat):
    result = adapter(all_spaces=True).list_member_spaces(max_spaces=3)
    assert len(result.spaces) == 3 and result.truncated


def test_enumeration_never_raises_and_is_refused_when_restricted(chat, monkeypatch):
    assert "all_spaces" in adapter(space_names=[S_TEAM]).list_member_spaces().error
    assert chat.requests == []

    def boom(req, timeout=None):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    result = adapter(all_spaces=True).list_member_spaces()
    assert result.error and "OSError" in result.error and result.spaces == []
    assert "no token_provider" in GoogleChatAdapter(all_spaces=True).list_member_spaces().error


def test_label_is_never_blank():
    assert space_label("spaces/XY", None, None) == "space XY"
    assert space_label("", "", "DIRECT_MESSAGE").startswith("direct message")


def test_typed_read_of_a_dm_uses_its_label(chat):
    chat.say(S_DM, "Ana", "Hi", "2026-09-20T09:40:00Z")
    got = adapter(all_spaces=True).fetch_messages_since(S_DM, None)
    assert got.display_name == "direct message DMDM22" and len(got.messages) == 1


def test_restricted_typed_read_still_refuses_unlisted_space(chat):
    with pytest.raises(ChatReadError, match="refused"):
        adapter(space_names=[S_TEAM]).fetch_messages_since(S_DM, None)
    assert chat.requests == []


def test_adapter_has_no_write_path(chat):
    ad = adapter(all_spaces=True)
    assert not [n for n in dir(ad) if n.lower().lstrip("_") in ("post", "send", "create_message", "reply", "delete")]
    ad.list_member_spaces()
    ad.fetch_messages_since(S_TEAM, None)
    assert {m for m, _u in chat.requests} == {"GET"}


# --- source -------------------------------------------------------------------------------------

def test_source_reads_every_active_space_skips_stale_and_labels_each(chat, tmp_path):
    chat.say(S_TEAM, "Ana", "Plan the study.", "2026-09-20T09:00:00Z")
    chat.say(S_DM, "Ben", "Private note.", "2026-09-20T09:30:00Z")
    chat.say(S_GROUP, "Cy", "Lunch?", "2026-09-20T10:00:00Z")
    chat.say(S_GROUP, "assistant1", "Own words.", "2026-09-20T10:05:00Z")
    chat.say(S_OLD, "Di", "Ancient.", "2026-09-01T09:00:00Z")
    clock = Clock("2026-09-20T12:00:00Z")
    eng = UpdateEngine(None, watermarks=Watermarks(str(tmp_path / "wm.json")), now_fn=clock,
                       always=(), google_chat=adapter(all_spaces=True))
    bundle = eng.collect(card({"source": "google_chat", "spaces": "all"}), card_id="quest_1")
    (report,) = bundle.reports
    assert report.ok
    text = "\n".join(u.title + " " + u.body + " " + str(u.location) for u in bundle.updates)
    assert "Study team" in text and "direct message DMDM22" in text and "group chat GRGR33" in text
    assert "Ancient" not in text and "Own words" not in text
    assert not [r for r in chat.requests if f"{S_OLD}/messages" in r[1]]
    assert "1 with no activity" in report.explanation


def test_source_honors_max_spaces_and_reports_truncation(chat, tmp_path):
    for sp in (S_TEAM, S_DM, S_GROUP):
        chat.say(sp, "Ana", "hello", "2026-09-20T09:00:00Z")
    bundle = engine(tmp_path, adapter(all_spaces=True)).collect(
        card({"source": "google_chat", "max_spaces": 2}), card_id="quest_1")
    (report,) = bundle.reports
    assert report.ok and "first 2" in report.explanation
    read = {u.raw["space"] for u in bundle.updates}
    assert len(read) <= 2 and not (read & {S_OLD})


def test_source_explicit_space_list_still_works_in_unrestricted_mode(chat, tmp_path):
    chat.say(S_TEAM, "Ana", "one", "2026-09-20T09:00:00Z")
    chat.say(S_DM, "Ben", "two", "2026-09-20T09:30:00Z")
    bundle = engine(tmp_path, adapter(all_spaces=True)).collect(
        card({"source": "google_chat", "spaces": [S_TEAM]}), card_id="quest_1")
    assert {u.raw["space"] for u in bundle.updates} == {S_TEAM}
    assert not [r for r in chat.requests if r[1].endswith("/v1/spaces") or "/v1/spaces?" in r[1]]


def test_source_listing_failure_is_reported_and_holds_the_watermark(chat, tmp_path, monkeypatch):
    def boom(req, timeout=None):
        raise OSError("down")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    bundle = engine(tmp_path, adapter(all_spaces=True)).collect(ALL, card_id="quest_1")
    assert not bundle.reports[0].ok
    bundle.mark_seen()
    assert Watermarks(str(tmp_path / "wm.json")).get("quest_1", "google_chat") is None


@pytest.mark.parametrize("spec", [{"source": "google_chat"}, {"source": "google_chat", "spaces": "all"},
                                  {"source": "google_chat", "spaces": ["all"]}])
def test_restricted_mode_refuses_all_with_the_opt_in_hint_and_reads_nothing(chat, tmp_path, spec):
    bundle = engine(tmp_path, adapter(space_names=[S_TEAM])).collect(card(spec), card_id="quest_1")
    (report,) = bundle.reports
    assert not report.ok and "all_spaces = true" in report.error
    assert chat.requests == []
