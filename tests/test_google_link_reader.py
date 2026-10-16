"""Private Google link reading: URL parsing, who a file is read as, read-only enforcement, export
paths, paging, the server operation, the remote adapter and the web fallback wrapper.

A fake Drive stands in for Google: a transport that answers by file id and by WHO is asking (the
bearer token names the subject), so impersonation order and the absence of a service account
fallback are observable. No network, no real credentials.
"""
from __future__ import annotations

import json
import threading
import urllib.parse
import urllib.request

import pytest

from quest_ai_runner.adapters.files_server import build_server
from quest_ai_runner.adapters.google_link_reader import (
    KIND_DOC,
    KIND_FILE,
    KIND_FOLDER,
    KIND_FORM,
    KIND_SHEET,
    KIND_SLIDES,
    GoogleLinkConfig,
    GoogleLinkReader,
    GoogleReadError,
    ReadOnlyGoogleClient,
    is_allowed_read_url,
    parse_google_link,
)
from quest_ai_runner.adapters.private_link_web import PrivateLinkWebResearch
from quest_ai_runner.adapters.remote_files_adapter import RemoteFilesAdapter
from quest_ai_runner.core.adapters import Observation

DOC = "docAAAAAAAAAA1"
SHEET = "sheetBBBBBBB2"
SLIDES = "slideCCCCCCC3"
PDF = "pdfDDDDDDDDD4"
FOLDER = "folderEEEEEE5"
FORM = "formFFFFFFFF6"
OTHER = "otherGGGGGG7"
ALICE = "alice@example.org"
BOB = "bob@example.org"
TOKEN = "t" * 12 + "-test-token-0123456789"


class FakeDrive:
    """A tiny Drive. ``visible`` maps file id to the subjects that may read it ("sa" is the
    service account). Records every call as (method, url, subject)."""

    def __init__(self):
        self.calls = []
        self.files = {
            DOC: {"name": "Plan", "mimeType": "application/vnd.google-apps.document"},
            SHEET: {"name": "Numbers", "mimeType": "application/vnd.google-apps.spreadsheet"},
            SLIDES: {"name": "Deck", "mimeType": "application/vnd.google-apps.presentation"},
            PDF: {"name": "a.pdf", "mimeType": "application/pdf", "size": "100"},
            FOLDER: {"name": "Shared folder", "mimeType": "application/vnd.google-apps.folder"},
            FORM: {"name": "Survey", "mimeType": "application/vnd.google-apps.form"},
            OTHER: {"name": "movie.mp4", "mimeType": "video/mp4", "size": "100"},
        }
        self.visible = {fid: {ALICE} for fid in self.files}
        self.visible[DOC] = {ALICE, "sa"}
        self.doc_text = "Plan heading\n\nThe launch date is the first of March."
        self.sheet_csv = "name,value\nrent,1200\n"
        self.exports = {}

    def token(self, subject, scope):
        return f"{subject or 'sa'}|{scope}"

    def transport(self, url, headers, timeout, max_bytes):
        subject, scope = headers["Authorization"][len("Bearer "):].split("|", 1)
        self.calls.append(("GET", url, subject, scope))
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        segments = parts.path.strip("/").split("/")
        if parts.hostname == "docs.google.com":
            fid = segments[2]
            if subject not in self.visible.get(fid, set()):
                return 404, b"{}", False
            return 200, self.sheet_csv.encode(), False
        if segments[-1] == "files" and len(segments) == 3:
            kids = [{"id": "k1", "name": "notes.txt", "mimeType": "text/plain", "modifiedTime": "2026-01-01"},
                    {"id": "k2", "name": "sub", "mimeType": "application/vnd.google-apps.folder"}]
            return 200, json.dumps({"files": kids}).encode(), False
        fid = segments[3]
        if subject not in self.visible.get(fid, set()):
            return 404, json.dumps({"error": {"errors": [{"reason": "notFound"}]}}).encode(), False
        meta = {"id": fid, **self.files[fid]}
        if segments[-1] == "export":
            mime = query["mimeType"][0]
            if fid in self.exports and mime in self.exports[fid]:
                return 200, self.exports[fid][mime], False
            if mime == "text/plain":
                return 200, self.doc_text.encode(), False
            if mime == "text/csv":
                return 200, self.sheet_csv.encode(), False
            return 403, json.dumps({"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}}).encode(), False
        if query.get("alt") == ["media"]:
            return 200, b"plain bytes", False
        return 200, json.dumps(meta).encode(), False


def make_reader(drive, *, domains=("example.org",), fallback=False):
    config = GoogleLinkConfig(credentials_path="unused", impersonate_domains=domains,
                              service_fallback=fallback)
    client = ReadOnlyGoogleClient(drive.token, transport=drive.transport)
    return GoogleLinkReader(config, client=client)


def subjects(drive):
    return [c[2] for c in drive.calls]


# --------------------------------------------------------------------------- URL parsing


@pytest.mark.parametrize("url,kind,file_id,gid", [
    (f"https://docs.google.com/document/d/{DOC}/edit", KIND_DOC, DOC, None),
    (f"https://docs.google.com/document/u/0/d/{DOC}/edit?tab=t.0", KIND_DOC, DOC, None),
    (f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid=0", KIND_SHEET, SHEET, "0"),
    (f"https://docs.google.com/spreadsheets/d/{SHEET}/edit?gid=1234&range=A1", KIND_SHEET, SHEET, "1234"),
    (f"https://docs.google.com/spreadsheets/d/{SHEET}/edit", KIND_SHEET, SHEET, None),
    (f"https://docs.google.com/presentation/d/{SLIDES}/edit#slide=id.p", KIND_SLIDES, SLIDES, None),
    (f"https://docs.google.com/forms/d/{FORM}/edit", KIND_FORM, FORM, None),
    (f"https://drive.google.com/file/d/{PDF}/view?usp=sharing", KIND_FILE, PDF, None),
    (f"https://drive.google.com/open?id={PDF}", KIND_FILE, PDF, None),
    (f"https://drive.google.com/uc?export=download&id={PDF}", KIND_FILE, PDF, None),
    (f"https://drive.google.com/drive/folders/{FOLDER}?usp=sharing", KIND_FOLDER, FOLDER, None),
    (f"https://drive.google.com/drive/u/1/folders/{FOLDER}", KIND_FOLDER, FOLDER, None),
])
def test_parse_google_links(url, kind, file_id, gid):
    ref = parse_google_link(url)
    assert (ref.kind, ref.file_id, ref.gid) == (kind, file_id, gid)


@pytest.mark.parametrize("url", [
    "https://example.com/document/d/" + DOC, "https://docs.google.com/", "not a url",
    "ftp://docs.google.com/document/d/" + DOC, "https://drive.google.com/drive/my-drive", ""])
def test_parse_rejects_non_google_links(url):
    assert parse_google_link(url) is None


# --------------------------------------------------------------------------- who reads


def test_impersonation_reads_as_the_person_and_never_touches_the_service_account():
    drive = FakeDrive()
    answer = make_reader(drive).read(f"https://docs.google.com/document/d/{DOC}/edit", as_user=ALICE)
    assert answer["ok"] and "first of March" in answer["text"]
    assert set(subjects(drive)) == {ALICE}


def test_person_without_access_is_told_so_and_service_account_is_not_tried():
    drive = FakeDrive()
    answer = make_reader(drive).read(f"https://docs.google.com/document/d/{DOC}/edit", as_user=BOB)
    assert not answer["ok"] and answer["reason"] == "not_shared"
    assert "not shared with your account" in answer["error"]
    assert "sa" not in subjects(drive)       # the DOC is visible to the service account; still not used


def test_service_account_fallback_only_when_the_operator_enables_it():
    drive = FakeDrive()
    answer = make_reader(drive, fallback=True).read(
        f"https://docs.google.com/document/d/{DOC}/edit", as_user=BOB)
    assert answer["ok"]
    assert subjects(drive)[0] == BOB and subjects(drive)[-1] == "sa"      # the person first, then the account


def test_no_user_or_foreign_domain_is_refused_without_a_google_call():
    drive = FakeDrive()
    reader = make_reader(drive)
    for who in (None, "", "not-an-email", "mallory@evil.test", "x@sub.example.org"):
        answer = reader.read(f"https://docs.google.com/document/d/{DOC}/edit", as_user=who)
        assert not answer["ok"] and answer["reason"] == "not_allowed"
    assert drive.calls == []


def test_no_user_with_fallback_uses_the_service_account():
    drive = FakeDrive()
    answer = make_reader(drive, fallback=True).read(f"https://docs.google.com/document/d/{DOC}/edit")
    assert answer["ok"] and set(subjects(drive)) == {"sa"}


def test_impersonation_uses_the_configured_scope_and_service_account_the_readonly_one():
    drive = FakeDrive()
    make_reader(drive, fallback=True).read(f"https://docs.google.com/document/d/{DOC}/edit", as_user=BOB)
    scopes = {(c[2], c[3]) for c in drive.calls}
    assert (BOB, "https://www.googleapis.com/auth/drive") in scopes
    assert ("sa", "https://www.googleapis.com/auth/drive.readonly") in scopes


# --------------------------------------------------------------------------- read-only


@pytest.mark.parametrize("url", [
    "https://www.googleapis.com/drive/v3/files/abc123456789/permissions",
    "https://www.googleapis.com/drive/v3/files/abc123456789/copy",
    "https://www.googleapis.com/drive/v3/files/abc123456789/watch",
    "https://www.googleapis.com/upload/drive/v3/files",
    "https://www.googleapis.com/drive/v3/files?alt=media",
    "https://www.googleapis.com/drive/v3/files/abc123456789/export?alt=media",
    "http://www.googleapis.com/drive/v3/files/abc123456789",
    "https://evil.test/drive/v3/files/abc123456789",
    "https://docs.google.com/document/d/abc123456789/edit",
    "https://www.googleapis.com@evil.test/drive/v3/files/abc123456789",
])
def test_requests_outside_the_read_allowlist_are_refused_before_any_network_call(url):
    drive = FakeDrive()
    assert not is_allowed_read_url(url)
    client = ReadOnlyGoogleClient(drive.token, transport=drive.transport)
    with pytest.raises(GoogleReadError):
        client.get(url, subject=ALICE, scope="s")
    assert drive.calls == []


def test_every_call_a_full_read_makes_is_an_allowed_get():
    drive = FakeDrive()
    reader = make_reader(drive)
    for url in (f"https://docs.google.com/document/d/{DOC}/edit",
                f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid=7",
                f"https://docs.google.com/spreadsheets/d/{SHEET}/edit",
                f"https://docs.google.com/presentation/d/{SLIDES}/edit",
                f"https://drive.google.com/drive/folders/{FOLDER}",
                f"https://drive.google.com/file/d/{PDF}/view"):
        reader.read(url, as_user=ALICE)
    assert drive.calls
    assert all(method == "GET" and is_allowed_read_url(url) for method, url, *_ in drive.calls)


# --------------------------------------------------------------------------- export paths


def test_doc_and_slides_export_as_plain_text():
    drive = FakeDrive()
    reader = make_reader(drive)
    for fid, kind, url in ((DOC, KIND_DOC, f"https://docs.google.com/document/d/{DOC}/edit"),
                           (SLIDES, KIND_SLIDES, f"https://docs.google.com/presentation/d/{SLIDES}/edit")):
        answer = reader.read(url, as_user=ALICE)
        assert answer["ok"] and answer["kind"] == kind and "launch date" in answer["text"]
    assert any("mimeType=text%2Fplain" in c[1] for c in drive.calls)


def test_sheet_with_gid_exports_that_tab_as_csv():
    drive = FakeDrive()
    answer = make_reader(drive).read(
        f"https://docs.google.com/spreadsheets/d/{SHEET}/edit#gid=42", as_user=ALICE)
    assert answer["ok"] and "rent,1200" in answer["text"]
    csv_calls = [c[1] for c in drive.calls if "docs.google.com" in c[1]]
    assert csv_calls and "gid=42" in csv_calls[0] and "format=csv" in csv_calls[0]


def test_sheet_without_gid_reads_every_tab_via_the_workbook_export():
    openpyxl = pytest.importorskip("openpyxl")
    import io
    book = openpyxl.Workbook()
    book.active.title = "First"
    book.active.append(["a", "1"])
    book.create_sheet("Second").append(["b", "2"])
    buffer = io.BytesIO()
    book.save(buffer)
    drive = FakeDrive()
    drive.exports[SHEET] = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": buffer.getvalue()}
    answer = make_reader(drive).read(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit", as_user=ALICE)
    assert answer["ok"] and "Sheet: First" in answer["text"] and "Sheet: Second" in answer["text"]


def test_sheet_without_gid_falls_back_to_first_tab_and_says_so():
    drive = FakeDrive()          # the fake refuses the workbook export as too large
    answer = make_reader(drive).read(f"https://docs.google.com/spreadsheets/d/{SHEET}/edit", as_user=ALICE)
    assert answer["ok"] and "first tab" in answer["text"] and "rent,1200" in answer["text"]


def test_folder_lists_children_names():
    drive = FakeDrive()
    answer = make_reader(drive).read(f"https://drive.google.com/drive/folders/{FOLDER}", as_user=ALICE)
    assert answer["ok"] and answer["kind"] == KIND_FOLDER
    assert "notes.txt" in answer["text"] and "sub/" in answer["text"]


def test_binary_pdf_and_text_files_go_through_extraction():
    drive = FakeDrive()
    drive.files["txtHHHHHHHH8"] = {"name": "n.txt", "mimeType": "text/plain", "size": "11"}
    drive.visible["txtHHHHHHHH8"] = {ALICE}
    answer = make_reader(drive).read("https://drive.google.com/file/d/txtHHHHHHHH8/view", as_user=ALICE)
    assert answer["ok"] and answer["text"] == "plain bytes"


def test_unsupported_file_type_and_forms_are_refused_honestly():
    drive = FakeDrive()
    reader = make_reader(drive)
    video = reader.read(f"https://drive.google.com/file/d/{OTHER}/view", as_user=ALICE)
    assert not video["ok"] and video["reason"] == "unsupported_type"
    form = reader.read(f"https://docs.google.com/forms/d/{FORM}/edit", as_user=ALICE)
    assert not form["ok"] and form["reason"] == "unsupported_type"


def test_secret_shapes_are_redacted_in_the_text():
    drive = FakeDrive()
    drive.doc_text = "notes\npassword = " + "x9" * 12 + "\nend"
    answer = make_reader(drive).read(f"https://docs.google.com/document/d/{DOC}/edit", as_user=ALICE)
    assert answer["ok"] and ("x9" * 12) not in answer["text"] and "[redacted]" in answer["text"]


# --------------------------------------------------------------------------- paging and focus


def test_full_read_pages_and_names_the_part_count():
    drive = FakeDrive()
    drive.doc_text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(400))
    reader = make_reader(drive)
    url = f"https://docs.google.com/document/d/{DOC}/edit"
    first = reader.read(url, as_user=ALICE, full=True)
    second = reader.read(url, as_user=ALICE, part=2)
    assert first["mode"] == "full" and first["part"] == 1 and first["parts"] > 1
    assert second["part"] == 2 and second["text"] != first["text"]
    beyond = reader.read(url, as_user=ALICE, part=999)
    assert beyond["ok"] and beyond["text"] == ""


def test_focused_read_returns_the_matching_passages_and_flags_excerpts():
    drive = FakeDrive()
    filler = "\n\n".join(f"Filler paragraph {i} " + "lorem " * 80 for i in range(200))
    drive.doc_text = filler + "\n\nThe pricing table says the annual plan costs ninety dollars."
    answer = make_reader(drive).read(
        f"https://docs.google.com/document/d/{DOC}/edit", as_user=ALICE, focus="annual plan pricing")
    assert answer["mode"] == "focus" and "ninety dollars" in answer["text"] and answer["excerpt_only"]


def test_a_paged_read_does_not_refetch_within_the_cache_window():
    drive = FakeDrive()
    reader = make_reader(drive)
    url = f"https://docs.google.com/document/d/{DOC}/edit"
    reader.read(url, as_user=ALICE, full=True)
    count = len(drive.calls)
    reader.read(url, as_user=ALICE, part=1)
    assert len(drive.calls) == count


def test_cache_is_per_person():
    drive = FakeDrive()
    reader = make_reader(drive)
    url = f"https://docs.google.com/document/d/{DOC}/edit"
    assert reader.read(url, as_user=ALICE)["ok"]
    assert not reader.read(url, as_user=BOB)["ok"]       # Alice's cached copy is not Bob's


# --------------------------------------------------------------------------- config


def test_config_from_env(tmp_path):
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"type": "service_account"}))
    config = GoogleLinkConfig.from_env({
        "QAR_FILES_GOOGLE_CREDENTIALS": str(key),
        "QAR_FILES_GOOGLE_IMPERSONATE_DOMAINS": "Example.org, other.test",
        "QAR_FILES_GOOGLE_SA_FALLBACK": "1"})
    assert config.impersonate_domains == ("example.org", "other.test")
    assert config.service_fallback and config.impersonate_scope.endswith("/auth/drive")
    assert GoogleLinkConfig.from_env({}) is None
    assert GoogleLinkConfig.from_env({"QAR_FILES_GOOGLE_CREDENTIALS": str(tmp_path / "missing.json")}) is None
    key.write_text(json.dumps({"type": "authorized_user"}))
    assert GoogleLinkConfig.from_env({"QAR_FILES_GOOGLE_CREDENTIALS": str(key)}) is None


# --------------------------------------------------------------------------- server + client


@pytest.fixture()
def served(tmp_path):
    drive = FakeDrive()
    root = tmp_path / "root"
    root.mkdir()
    server = build_server(str(root), TOKEN, port=0, links=make_reader(drive))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}", drive
    server.shutdown()
    server.server_close()


@pytest.fixture()
def served_without_links(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    server = build_server(str(root), TOKEN, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def post(base, op, body, token=TOKEN):
    req = urllib.request.Request(f"{base}/v1/{op}", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_server_advertises_link_capability(served, served_without_links):
    base, _ = served
    _, answer = post(base, "capabilities", {})
    assert answer["links"]["google"] is True and answer["links"]["service_account_fallback"] is False
    _, none = post(served_without_links, "capabilities", {})
    assert none["links"]["google"] is False


def test_server_link_op_needs_the_token_and_reads_as_the_person(served):
    base, drive = served
    url = f"https://docs.google.com/document/d/{DOC}/edit"
    assert post(base, "link", {"url": url, "as_user": ALICE}, token="wrong-token-wrong-token")[0] == 401
    _, ok = post(base, "link", {"url": url, "as_user": ALICE})
    assert ok["ok"] and "first of March" in ok["text"]
    _, denied = post(base, "link", {"url": url, "as_user": BOB})
    assert not denied["ok"] and denied["reason"] == "not_shared"
    _, refused = post(base, "link", {"url": url, "as_user": "mallory@evil.test"})
    assert not refused["ok"] and refused["reason"] == "not_allowed"


def test_server_without_links_says_unavailable(served_without_links):
    _, answer = post(served_without_links, "link", {"url": "https://docs.google.com/document/d/" + DOC})
    assert not answer["ok"] and answer["reason"] == "unavailable"


def test_remote_adapter_reads_a_link_framed_as_untrusted(served):
    base, _ = served
    adapter = RemoteFilesAdapter(alias="box", base_url=base, token=TOKEN)
    assert adapter.reads_links()
    obs = adapter.read_link(f"https://docs.google.com/document/d/{DOC}/edit", as_user=ALICE)
    assert obs.kind == "read" and "UNTRUSTED FILE CONTENT" in obs.text and "first of March" in obs.text


def test_remote_adapter_error_is_the_typed_link_not_read_sentence(served):
    base, _ = served
    adapter = RemoteFilesAdapter(alias="box", base_url=base, token=TOKEN)
    obs = adapter.read_link(f"https://docs.google.com/document/d/{DOC}/edit", as_user=BOB)
    assert obs.kind == "error" and obs.error.startswith("LINK NOT READ [not_shared]")
    assert "not shared with your account" in obs.error


def test_remote_adapter_returns_none_when_the_server_reads_no_links(served_without_links):
    adapter = RemoteFilesAdapter(alias="box", base_url=served_without_links, token=TOKEN)
    assert not adapter.reads_links()
    assert adapter.read_link("https://docs.google.com/document/d/" + DOC) is None
    assert adapter.link_reader(ALICE)("https://docs.google.com/document/d/" + DOC) is None


# --------------------------------------------------------------------------- web fallback


class FakeWeb:
    def __init__(self, result):
        self.result = result
        self.fetches = []
        self.backend_name = "fake"

    def fetch(self, url, **kwargs):
        self.fetches.append((url, kwargs))
        return self.result

    def search(self, queries, **kwargs):
        return Observation(kind="query", text="searched")


PUBLIC_FAIL = Observation(kind="error", rel_path="u", error="LINK NOT READ [not_shared] u : sign in")


def test_wrapper_falls_back_to_the_private_reader_for_google_links():
    seen = []

    def reader(url, **kwargs):
        seen.append((url, kwargs))
        return Observation(kind="read", text="private text")

    wrapped = PrivateLinkWebResearch(FakeWeb(PUBLIC_FAIL), [reader])
    out = wrapped.fetch(f"https://docs.google.com/document/d/{DOC}/edit", focus="x", full=True, part=2)
    assert out.text == "private text"
    assert seen[0][1] == {"full": True, "part": 2, "focus": "x"}
    assert wrapped.backend_name == "fake" and wrapped.search("q").text == "searched"


def test_wrapper_leaves_public_successes_and_non_google_links_alone():
    called = []

    def reader(url, **kwargs):
        called.append(url)
        return Observation(kind="read", text="private")

    ok = Observation(kind="read", text="public")
    assert PrivateLinkWebResearch(FakeWeb(ok), [reader]).fetch(
        f"https://docs.google.com/document/d/{DOC}/edit").text == "public"
    assert PrivateLinkWebResearch(FakeWeb(PUBLIC_FAIL), [reader]).fetch("https://example.org/page").kind == "error"
    assert called == []


def test_wrapper_keeps_the_public_error_when_no_reader_can_help():
    wrapped = PrivateLinkWebResearch(FakeWeb(PUBLIC_FAIL), [lambda url, **k: None, lambda url, **k: 1 / 0])
    assert wrapped.fetch(f"https://docs.google.com/document/d/{DOC}/edit") is PUBLIC_FAIL


def test_wrapper_prefers_the_readers_specific_error():
    specific = Observation(kind="error", error="LINK NOT READ [not_shared] u : not shared with your account")
    wrapped = PrivateLinkWebResearch(FakeWeb(PUBLIC_FAIL), [lambda url, **k: specific])
    assert wrapped.fetch(f"https://docs.google.com/document/d/{DOC}/edit") is specific
