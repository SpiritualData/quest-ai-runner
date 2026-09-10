"""Comments on a document are a channel, not a corpus: they can be read AND answered.

A comment is the cheapest thing a person writes and the easiest for an assistant never to see.
They leave it anchored to the sentence it is about and then wait. What this file pins down:

  * THE QUOTE IS THE SUBJECT. Comments are written as deixis ("this is unclear", "cite here"), so a
    comment parsed without the passage it is anchored to is unanswerable.
  * ANSWERED MEANS THE LAST WORD IS OURS. A person who replies after our reply has reopened the
    thread, and it needs an answer again.
  * A READ FAILS QUIETLY, A WRITE DOES NOT. A person who has commented on nothing is the normal
    case; an assistant that believes it answered somebody when it did not is a real problem.

Offline: ``urlopen`` is replaced with a recorder, so no network and no credentials are involved.
"""
import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from quest_ai_runner.adapters.drive_comments import (
    COMMENT_WRITE_SCOPES,
    DriveComments,
    MAX_COMMENT_CHARS,
    render_comments,
    unanswered,
)

SINCE = datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc)


class FakeHTTP:
    """Stands in for ``urllib.request.urlopen``: records requests, replays queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []                     # (method, url, body)

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.requests.append((req.get_method(), req.full_url, body))
        nxt = self.responses.pop(0) if self.responses else {}
        if isinstance(nxt, Exception):
            raise nxt
        return _Resp(nxt)

    @property
    def urls(self):
        return [u for _m, u, _b in self.requests]


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, message=""):
    body = io.BytesIO(json.dumps({"error": {"message": message}}).encode("utf-8"))
    return urllib.error.HTTPError("http://drive", code, message, {}, body)


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP([])
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def _client(token="tok"):
    return DriveComments(token_provider=(lambda: token) if token else None)


def _comment(comment_id="c1", *, content="This needs a citation", quoted="the claim in para three",
             author="A reader", me=False, resolved=False, replies=()):
    return {
        "id": comment_id,
        "content": content,
        "createdTime": "2026-09-08T10:00:00.000Z",
        "modifiedTime": "2026-09-08T10:00:00.000Z",
        "resolved": resolved,
        "author": {"displayName": author, "me": me},
        "quotedFileContent": {"value": quoted, "mimeType": "text/html"},
        "replies": list(replies),
    }


def _reply(content, *, me=False, author="A reader"):
    return {"id": "r1", "content": content, "createdTime": "2026-09-08T11:00:00.000Z",
            "author": {"displayName": author, "me": me}, "action": ""}


# --- reading -----------------------------------------------------------------------------------

def test_a_comment_arrives_with_the_passage_it_was_anchored_to(http):
    http.responses.append({"comments": [_comment()]})
    got = _client().comments_for_file("file1", file_name="Chapter two")
    assert len(got) == 1
    c = got[0]
    assert c.quoted_text == "the claim in para three"
    assert c.content == "This needs a citation"
    assert c.file_id == "file1" and c.file_name == "Chapter two"
    assert "reply to comment c1 on file file1" in c.as_text()


def test_since_is_pushed_down_to_drive_so_a_reopened_thread_still_comes_back(http):
    http.responses.append({"comments": []})
    _client().comments_for_file("file1", since=SINCE)
    assert "startModifiedTime=2026-09-01T00%3A00%3A00.000Z" in http.urls[0]


def test_resolved_threads_are_left_out_unless_they_are_asked_for(http):
    http.responses.append({"comments": [_comment("c1"), _comment("c2", resolved=True)]})
    assert [c.comment_id for c in _client().comments_for_file("f")] == ["c1"]
    http.responses.append({"comments": [_comment("c1"), _comment("c2", resolved=True)]})
    assert len(_client().comments_for_file("f", include_resolved=True)) == 2


def test_a_thread_whose_last_word_is_ours_is_answered_and_one_they_replied_to_is_not(http):
    http.responses.append({"comments": [
        _comment("answered", replies=[_reply("Added the citation.", me=True)]),
        _comment("reopened", replies=[_reply("Cited.", me=True),
                                      _reply("Not that one, the other paper.")]),
        _comment("ours", me=True),
        _comment("open"),
    ]})
    got = {c.comment_id: c for c in _client().comments_for_file("f")}
    assert got["answered"].needs_answer is False
    assert got["reopened"].needs_answer is True         # they had the last word
    assert got["ours"].needs_answer is False            # we wrote it
    assert got["open"].needs_answer is True
    assert [c.comment_id for c in unanswered(got.values())] == ["reopened", "open"]


def test_an_essay_length_comment_cannot_crowd_out_the_work_it_is_about(http):
    http.responses.append({"comments": [_comment(content="x" * (MAX_COMMENT_CHARS + 500))]})
    body = _client().comments_for_file("f")[0].content
    assert len(body) <= MAX_COMMENT_CHARS + len(" [...truncated]")
    assert body.endswith("[...truncated]")


def test_a_folder_is_the_unit_a_person_thinks_in(http):
    http.responses.append({"files": [
        {"id": "f1", "name": "Chapter one", "modifiedTime": "2026-09-08T09:00:00.000Z"},
        {"id": "f2", "name": "Chapter two", "modifiedTime": "2026-09-07T09:00:00.000Z"},
    ]})
    http.responses.append({"comments": [_comment("c1")]})
    http.responses.append({"comments": [_comment("c2")]})
    got = _client().comments_for_folder("folder1")
    assert sorted(c.comment_id for c in got) == ["c1", "c2"]
    assert sorted(c.file_name for c in got) == ["Chapter one", "Chapter two"]


def test_a_folder_read_without_a_since_still_finds_a_comment_on_an_old_file(http):
    """One HTTP call per file, and the FILE listing is deliberately unfiltered by time: a document
    untouched for a month can still have a comment added today."""
    http.responses.append({"files": [{"id": "f1", "name": "Old chapter",
                                      "modifiedTime": "2026-01-01T09:00:00.000Z"}]})
    http.responses.append({"comments": [_comment()]})
    assert len(_client().comments_for_folder("folder1", since=SINCE)) == 1
    assert "modifiedTime+%3E" not in http.urls[0]   # no time filter on the FILE listing


def test_files_in_folder_reports_what_the_person_edited_since_we_looked(http):
    http.responses.append({"files": [{"id": "f1", "name": "Chapter two", "mimeType": "doc",
                                      "modifiedTime": "2026-09-08T09:00:00.000Z",
                                      "webViewLink": "http://doc"}]})
    changed = _client().files_in_folder("folder1", since=SINCE)
    assert [f.file_name for f in changed] == ["Chapter two"]
    assert changed[0].modified_at == datetime(2026, 9, 8, 9, 0, tzinfo=timezone.utc)
    assert "modifiedTime+%3E" in http.urls[0]


# --- a read never raises ---------------------------------------------------------------------

def test_no_token_means_no_access_rather_than_an_exception(http):
    assert DriveComments(token_provider=None).comments_for_file("f") == []
    assert DriveComments(token_provider=lambda: None).files_in_folder("folder") == []
    assert http.requests == []


def test_a_broken_token_provider_degrades_to_no_access():
    def boom():
        raise RuntimeError("no credentials on this machine")

    assert DriveComments(token_provider=boom).comments_for_file("f") == []


def test_an_api_failure_reads_as_nothing_new_not_as_a_crash(http):
    http.responses.append(_http_error(403, "Insufficient permission"))
    assert _client().comments_for_file("f") == []
    http.responses.append(_http_error(500))
    assert _client().files_in_folder("folder") == []


# --- writing, which is NOT best-effort ---------------------------------------------------------

def test_a_reply_lands_on_the_thread_it_answers(http):
    http.responses.append({"id": "r99"})
    out = _client().reply("file1", "c1", "Added the citation.")
    assert out.ok and out.reply_id == "r99"
    method, url, body = http.requests[0]
    assert method == "POST"
    assert url.startswith("https://www.googleapis.com/drive/v3/files/file1/comments/c1/replies")
    assert body == {"content": "Added the citation."}


def test_resolving_says_what_was_done_rather_than_silently_closing_the_thread(http):
    http.responses.append({"id": "r99"})
    assert _client().resolve("file1", "c1", "Rewrote the paragraph.").ok
    assert http.requests[0][2] == {"content": "Rewrote the paragraph.", "action": "resolve"}


def test_a_read_scoped_token_gets_the_apis_own_reason_not_a_silent_no_op(http):
    http.responses.append(_http_error(403, "Insufficient permission for this file"))
    out = _client().reply("file1", "c1", "Added the citation.")
    assert out.ok is False
    assert "403" in out.error and "Insufficient permission" in out.error
    assert "drive" in COMMENT_WRITE_SCOPES[0]


def test_an_empty_reply_is_refused_before_it_reaches_the_api(http):
    out = _client().reply("file1", "c1", "   ")
    assert out.ok is False and out.error == "empty reply text"
    assert http.requests == []


def test_no_token_is_reported_plainly_rather_than_reported_as_sent(http):
    out = DriveComments(token_provider=lambda: None).reply("file1", "c1", "Answered.")
    assert out.ok is False and "no Drive token" in out.error


# --- rendering ---------------------------------------------------------------------------------

def test_rendered_comments_always_carry_the_address_to_answer_them_at(http):
    http.responses.append({"comments": [_comment()]})
    block = render_comments(_client().comments_for_file("file1", file_name="Chapter two"))
    assert "the claim in para three" in block
    assert "reply to comment c1 on file file1" in block


def test_nothing_to_show_renders_nothing(http):
    assert render_comments([]) == ""


def test_html_entities_in_comment_and_quote_are_decoded():
    """Drive serves comment text and the quoted passage HTML-escaped.

    Live case (2026-09-10): a comment on the phrase "ITPP's corpus is positive-only by design"
    came back quoting ``ITPP&#39;s corpus``. Left encoded, the entity reaches a model as the
    person's own words, and a run trying to find the quoted passage in the document searches for
    a string that is not in it.
    """
    from quest_ai_runner.adapters.drive_comments import DriveComments

    dc = DriveComments(token_provider=lambda: "t")
    comment = dc._parse_comment(
        {
            "id": "c1",
            "content": "You said &quot;by design&quot; &amp; I never did",
            "quotedFileContent": {"value": "ITPP&#39;s corpus is positive-only"},
            "author": {"displayName": "Joshua Mathias"},
        },
        "f1", "A doc", "")

    assert comment.content == 'You said "by design" & I never did'
    assert comment.quoted_text == "ITPP's corpus is positive-only"
