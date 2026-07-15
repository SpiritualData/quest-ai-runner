"""GoogleDriveAdapter -- RetrievalAdapter surface, mocked at the ONE HTTP seam (``_get_bytes``).

Mirrors the mocking approach used for GoogleChatAdapter (``test_google_chat_card_learning.py``):
a mutable in-memory fake stands in for the Drive REST API, no network, no real token, no real
file/folder ids. PDF extraction is exercised by injecting a fake ``pypdf`` module into
``sys.modules`` (the real package isn't a hard dependency of this repo), and the "pypdf not
installed" path is exercised for real since ``pypdf`` genuinely isn't installed here.
"""
import json
import sys
import types
import urllib.error
import urllib.parse

import pytest

from quest_ai_runner.adapters.google_drive_adapter import (
    GoogleDriveAdapter,
    parse_drive_url,
    DEFAULT_DRIVE_SCOPES,
)


class _FakeDrive:
    """In-memory stand-in for the Drive v3 REST API (no network)."""

    FOLDER_ID = "folderAAA"
    DOC_ID = "docBBB"
    SHEET_ID = "sheetCCC"
    PDF_ID = "pdfDDD"
    TEXT_ID = "textEEE"
    UNSUPPORTED_ID = "imgFFF"
    PDF_BYTES = b"%PDF-fake-bytes%"

    def __init__(self):
        self.metadata = {
            self.FOLDER_ID: {
                "id": self.FOLDER_ID, "name": "My Folder",
                "mimeType": "application/vnd.google-apps.folder",
            },
            self.DOC_ID: {
                "id": self.DOC_ID, "name": "My Doc",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-01-01T00:00:00Z",
                "webViewLink": "https://docs.google.com/document/d/docBBB/edit",
            },
            self.SHEET_ID: {
                "id": self.SHEET_ID, "name": "My Sheet",
                "mimeType": "application/vnd.google-apps.spreadsheet",
            },
            self.PDF_ID: {
                "id": self.PDF_ID, "name": "My PDF", "mimeType": "application/pdf", "size": "1234",
            },
            self.TEXT_ID: {
                "id": self.TEXT_ID, "name": "notes.txt", "mimeType": "text/plain",
            },
            self.UNSUPPORTED_ID: {
                "id": self.UNSUPPORTED_ID, "name": "image.png", "mimeType": "image/png",
            },
        }
        self.children = [
            {
                "id": "child1", "name": "child1.txt", "mimeType": "text/plain",
                "webViewLink": "https://drive.google.com/file/d/child1/view",
                "modifiedTime": "2026-02-01T00:00:00Z", "size": "10",
            },
        ]

    def get_bytes(self, url: str, token: str) -> bytes:
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path.endswith("/export"):
            mime = (qs.get("mimeType") or [""])[0]
            if mime == "text/plain":
                return b"Doc plain text content"
            if mime == "text/csv":
                return b"a,b,c\n1,2,3"
            return b""

        if qs.get("alt") == ["media"]:
            if self.PDF_ID in path:
                return self.PDF_BYTES
            if self.TEXT_ID in path:
                return b"plain text file body"
            return b""

        if path.endswith("/files"):
            return json.dumps({"files": self.children}).encode("utf-8")

        file_id = path.rsplit("/", 1)[-1]
        if file_id in self.metadata:
            return json.dumps(self.metadata[file_id]).encode("utf-8")

        raise urllib.error.HTTPError(url, 404, "not found", {}, None)


def _adapter(fake=None, **kwargs):
    a = GoogleDriveAdapter(token_provider=lambda: "fake-token", **kwargs)
    if fake is not None:
        a._get_bytes = fake.get_bytes
    return a


# ---------------------------------------------------------------------------
# parse_drive_url
# ---------------------------------------------------------------------------

def test_parse_drive_url_file():
    result = parse_drive_url("https://drive.google.com/file/d/abc123/view?usp=sharing")
    assert result == {"kind": "file", "id": "abc123"}


def test_parse_drive_url_folder():
    result = parse_drive_url("https://drive.google.com/drive/folders/xyz789")
    assert result == {"kind": "folder", "id": "xyz789"}


def test_parse_drive_url_doc():
    result = parse_drive_url("https://docs.google.com/document/d/docid456/edit")
    assert result == {"kind": "file", "id": "docid456"}


def test_parse_drive_url_sheet():
    result = parse_drive_url("https://docs.google.com/spreadsheets/d/sheetid789/edit#gid=0")
    assert result == {"kind": "file", "id": "sheetid789"}


def test_parse_drive_url_unrecognized_returns_none():
    assert parse_drive_url("https://example.com/not-a-drive-link") is None
    assert parse_drive_url("") is None
    assert parse_drive_url(None) is None
    # A bare id (not a URL) is also not a recognized URL -- callers use it directly.
    assert parse_drive_url("bareFileId123") is None


# ---------------------------------------------------------------------------
# Unconfigured adapter (no token_provider) -- every method degrades cleanly
# ---------------------------------------------------------------------------

def test_unconfigured_adapter_never_raises():
    a = GoogleDriveAdapter(token_provider=None)

    obs = a.read_section("some-id")
    assert obs.kind == "error" and "not configured" in obs.error

    obs = a.query({"action": "list", "folder_id": "x"})
    assert obs.kind == "error" and "not configured" in obs.error

    obs = a.describe_source("some-id")
    assert obs.kind == "error" and "not configured" in obs.error

    obs = a.list_sources()
    assert obs.kind == "query" and "not configured" in obs.text


def test_token_provider_returning_none_is_also_unconfigured():
    a = GoogleDriveAdapter(token_provider=lambda: None)
    obs = a.query({"action": "list", "folder_id": "x"})
    assert obs.kind == "error" and "not configured" in obs.error


# ---------------------------------------------------------------------------
# query({"action": "list", ...})
# ---------------------------------------------------------------------------

def test_query_list_returns_file_metadata():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "list", "folder_id": fake.FOLDER_ID})
    assert obs.kind == "query"
    payload = json.loads(obs.text)
    assert "truncated" not in payload
    assert payload["files"] == [
        {
            "id": "child1", "name": "child1.txt", "mimeType": "text/plain",
            "webViewLink": "https://drive.google.com/file/d/child1/view",
            "modifiedTime": "2026-02-01T00:00:00Z", "size": "10",
        }
    ]


def test_query_list_requires_folder_id():
    a = _adapter(_FakeDrive())
    obs = a.query({"action": "list"})
    assert obs.kind == "error" and "folder_id" in obs.error


def test_query_list_truncates_huge_folder():
    """A folder with more children than _MAX_LIST_FILES is capped, not dumped unbounded into the
    LLM's grounding context (regression test for an unbounded-listing prompt-blowup risk)."""
    from quest_ai_runner.adapters import google_drive_adapter as mod

    fake = _FakeDrive()
    fake.children = [
        {
            "id": f"child{i}", "name": f"child{i}.txt", "mimeType": "text/plain",
            "webViewLink": f"https://drive.google.com/file/d/child{i}/view",
            "modifiedTime": "2026-02-01T00:00:00Z", "size": "10",
        }
        for i in range(mod._MAX_LIST_FILES + 50)
    ]
    a = _adapter(fake)
    obs = a.query({"action": "list", "folder_id": fake.FOLDER_ID})
    assert obs.kind == "query"
    payload = json.loads(obs.text)
    assert len(payload["files"]) == mod._MAX_LIST_FILES
    assert payload["truncated"] is True


# ---------------------------------------------------------------------------
# query({"action": "read", ...}) -- content extraction by mimeType
# ---------------------------------------------------------------------------

def test_query_read_google_doc_exports_plain_text():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": fake.DOC_ID})
    assert obs.kind == "read"
    assert obs.text == "Doc plain text content"


def test_query_read_google_sheet_exports_csv():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": fake.SHEET_ID})
    assert obs.kind == "read"
    assert obs.text == "a,b,c\n1,2,3"


def test_query_read_plain_text_file():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": fake.TEXT_ID})
    assert obs.kind == "read"
    assert obs.text == "plain text file body"


def test_query_read_unsupported_mimetype_returns_error_not_raw_binary():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": fake.UNSUPPORTED_ID})
    assert obs.kind == "error"
    assert "image/png" in obs.error
    assert "not supported" in obs.error


def test_query_read_missing_file_returns_error():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": "does-not-exist"})
    assert obs.kind == "error"


def test_query_read_requires_file_id():
    a = _adapter(_FakeDrive())
    obs = a.query({"action": "read"})
    assert obs.kind == "error" and "file_id" in obs.error


def test_query_unknown_action_returns_error():
    a = _adapter(_FakeDrive())
    obs = a.query({"action": "delete", "file_id": "x"})
    assert obs.kind == "error" and "unknown Drive query action" in obs.error


def test_query_folder_id_via_read_action_lists_instead():
    """A folder id passed to action='read' is auto-detected via mimeType and listed."""
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": fake.FOLDER_ID})
    assert obs.kind == "query"
    payload = json.loads(obs.text)
    assert payload["files"][0]["id"] == "child1"


def test_query_read_by_url_resolves_to_file_id():
    """A pasted Docs/Sheets/Drive URL works as file_id, same as read_section already did."""
    fake = _FakeDrive()
    a = _adapter(fake)
    url = f"https://docs.google.com/document/d/{fake.DOC_ID}/edit"
    obs = a.query({"action": "read", "file_id": url})
    assert obs.kind == "read"
    assert obs.text == "Doc plain text content"


def test_query_list_by_url_resolves_to_folder_id():
    """A pasted Drive folder URL works as folder_id, same as read_section already did."""
    fake = _FakeDrive()
    a = _adapter(fake)
    url = f"https://drive.google.com/drive/folders/{fake.FOLDER_ID}"
    obs = a.query({"action": "list", "folder_id": url})
    assert obs.kind == "query"
    payload = json.loads(obs.text)
    assert payload["files"][0]["id"] == "child1"


# ---------------------------------------------------------------------------
# PDF extraction (optional pypdf dependency)
# ---------------------------------------------------------------------------

def test_query_read_pdf_without_pypdf_installed_returns_clean_error():
    # pypdf is genuinely not installed in this repo's dependency set (optional [drive] extra),
    # so this exercises the real ImportError path, not a simulation.
    sys.modules.pop("pypdf", None)
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": fake.PDF_ID})
    assert obs.kind == "error"
    assert "pypdf" in obs.error
    assert "pip install" in obs.error


def test_query_read_pdf_with_pypdf_installed_extracts_text(monkeypatch):
    class _FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _FakePdfReader:
        def __init__(self, stream):
            self._data = stream.read()

        @property
        def pages(self):
            return [_FakePage("Page one text"), _FakePage("Page two text")]

    fake_pypdf = types.ModuleType("pypdf")
    fake_pypdf.PdfReader = _FakePdfReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.query({"action": "read", "file_id": fake.PDF_ID})
    assert obs.kind == "read"
    assert obs.text == "Page one text\nPage two text"


# ---------------------------------------------------------------------------
# grep -- not supported
# ---------------------------------------------------------------------------

def test_grep_not_supported():
    a = _adapter(_FakeDrive())
    obs = a.grep("anything")
    assert obs.kind == "error"
    assert "not supported" in obs.error
    assert "query(" in obs.error


# ---------------------------------------------------------------------------
# read_section -- delegates to the same read/list logic, by id or by URL
# ---------------------------------------------------------------------------

def test_read_section_by_bare_id_reads_a_file():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.read_section(fake.DOC_ID)
    assert obs.kind == "read"
    assert obs.text == "Doc plain text content"
    assert obs.rel_path == fake.DOC_ID


def test_read_section_by_url_reads_a_file():
    fake = _FakeDrive()
    a = _adapter(fake)
    url = f"https://docs.google.com/document/d/{fake.DOC_ID}/edit"
    obs = a.read_section(url)
    assert obs.kind == "read"
    assert obs.text == "Doc plain text content"
    assert obs.rel_path == url  # original rel_path preserved, not rewritten to the bare id


def test_read_section_folder_id_lists():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.read_section(fake.FOLDER_ID)
    assert obs.kind == "query"
    payload = json.loads(obs.text)
    assert payload["files"][0]["id"] == "child1"


def test_read_section_applies_line_range():
    fake = _FakeDrive()
    a = _adapter(fake)

    def multi_line_bytes(url, token):
        if "alt=media" in url and fake.TEXT_ID in url:
            return b"line one\nline two\nline three"
        return fake.get_bytes(url, token)

    a._get_bytes = multi_line_bytes
    obs = a.read_section(fake.TEXT_ID, start_line=2, end_line=2)
    assert obs.kind == "read"
    assert obs.text == "line two"


def test_read_section_empty_id_is_an_error():
    a = _adapter(_FakeDrive())
    obs = a.read_section("")
    assert obs.kind == "error"


# ---------------------------------------------------------------------------
# Discovery: list_sources / describe_source / list_operations / describe_operation
# ---------------------------------------------------------------------------

def test_list_sources_without_root_folder_gives_guidance():
    a = _adapter(_FakeDrive())
    obs = a.list_sources()
    assert obs.kind == "query"
    assert "no configured root folder" in obs.text.lower()


def test_list_sources_with_root_folder_lists_it():
    fake = _FakeDrive()
    a = _adapter(fake, root_folder_id=fake.FOLDER_ID)
    obs = a.list_sources()
    assert obs.kind == "query"
    payload = json.loads(obs.text)
    assert payload["files"][0]["id"] == "child1"


def test_describe_source_file():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.describe_source(fake.DOC_ID)
    assert obs.kind == "query"
    assert "My Doc" in obs.text
    assert "application/vnd.google-apps.document" in obs.text
    assert "readable via" in obs.text


def test_describe_source_unsupported_mimetype_says_so():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.describe_source(fake.UNSUPPORTED_ID)
    assert obs.kind == "query"
    assert "not supported" in obs.text


def test_describe_source_folder():
    fake = _FakeDrive()
    a = _adapter(fake)
    obs = a.describe_source(fake.FOLDER_ID)
    assert obs.kind == "query"
    assert "item(s) directly inside this folder" in obs.text


def test_list_operations_mentions_drive_list_and_drive_read():
    a = _adapter(_FakeDrive())
    obs = a.list_operations()
    assert obs.kind == "query"
    assert "drive_list" in obs.text
    assert "drive_read" in obs.text


def test_describe_operation_known_and_unknown():
    a = _adapter(_FakeDrive())
    obs = a.describe_operation("drive_read")
    assert obs.kind == "query"
    assert "file_id" in obs.text

    obs = a.describe_operation("nonexistent_op")
    assert obs.kind == "error"


# ---------------------------------------------------------------------------
# Default scopes sanity
# ---------------------------------------------------------------------------

def test_default_drive_scopes_is_readonly():
    assert DEFAULT_DRIVE_SCOPES == ("https://www.googleapis.com/auth/drive.readonly",)
