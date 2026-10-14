"""Offline tests for reading shared Google files / PDFs and for honest, typed link failures."""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Dict, List

import pytest

from quest_ai_runner.adapters import web_link_access as wla
from quest_ai_runner.adapters import web_research as wr
from quest_ai_runner.adapters.web_cache import WebCache
from quest_ai_runner.adapters.web_link_access import (
    ALL_REASONS,
    REASON_MESSAGES,
    extract_pdf_text,
    link_not_read_error,
    rewrite_for_export,
)
from quest_ai_runner.adapters.web_research import (
    FetchedPage,
    PageFetchError,
    UnsupportedContentTypeError,
    WebResearchAdapter,
    _default_page_fetcher,
)
from quest_ai_runner.core import orchestrator as orch
from quest_ai_runner.core.link_detect import describe_link, extract_urls

ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Fake hosts resolve to a public address, so the real SSRF logic runs without a DNS query."""
    from quest_ai_runner.adapters import web_page_extract

    monkeypatch.setattr(web_page_extract, "default_resolver", lambda host: ["93.184.216.34"])


class NoBackend:
    name = "none"

    def search(self, query, *, max_results=5):  # pragma: no cover - never called
        raise AssertionError("search not expected")


def adapter(fetcher, **kw):
    return WebResearchAdapter(NoBackend(), cache=WebCache(), page_fetcher=fetcher, **kw)


def text_page(body, content_type="text/plain", **kw):
    return FetchedPage(html=body, content_type=content_type, status_code=200, **kw)


# ---------------------------------------------------------------------------
# URL classification and export rewriting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    (f"https://docs.google.com/document/d/{ID}/edit", f"https://docs.google.com/document/d/{ID}/export?format=txt"),
    (f"https://docs.google.com/document/u/0/d/{ID}/edit?usp=sharing", f"https://docs.google.com/document/d/{ID}/export?format=txt"),
    (f"https://docs.google.com/a/example.org/document/d/{ID}/edit", f"https://docs.google.com/document/d/{ID}/export?format=txt"),
    (f"https://docs.google.com/spreadsheets/d/{ID}/edit", f"https://docs.google.com/spreadsheets/d/{ID}/export?format=csv"),
    (f"https://docs.google.com/spreadsheets/d/{ID}/edit#gid=1234", f"https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&gid=1234"),
    (f"https://docs.google.com/spreadsheets/d/{ID}/edit?gid=77&usp=sharing", f"https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&gid=77"),
    (f"https://docs.google.com/presentation/d/{ID}/edit", f"https://docs.google.com/presentation/d/{ID}/export?format=txt"),
    (f"https://drive.google.com/file/d/{ID}/view?usp=sharing", f"https://drive.google.com/uc?export=download&id={ID}"),
    (f"https://drive.google.com/open?id={ID}", f"https://drive.google.com/uc?export=download&id={ID}"),
])
def test_rewrite_for_export(url, expected):
    target = rewrite_for_export(url)
    assert target is not None
    assert target.export_url == expected


def test_rewrite_kinds_and_formats():
    assert rewrite_for_export(f"https://docs.google.com/document/d/{ID}/edit").text_format == "text"
    assert rewrite_for_export(f"https://docs.google.com/spreadsheets/d/{ID}/edit").text_format == "csv"
    assert rewrite_for_export(f"https://drive.google.com/file/d/{ID}/view").kind == "drive_file"


@pytest.mark.parametrize("url", [
    "https://example.com/page",
    f"https://docs.google.com/document/d/e/2PACX-{ID}/pub",   # published page, ordinary HTML
    "https://docs.google.com/document/d/short/edit",            # not a real id
    f"https://evil.example/document/d/{ID}/edit",
    f"https://docs.google.com.evil.example/document/d/{ID}/edit",
    "ftp://docs.google.com/document/d/" + ID,
    "",
])
def test_rewrite_ignores_everything_else(url):
    assert rewrite_for_export(url) is None


def test_google_form_is_recognised_but_has_no_export():
    target = rewrite_for_export("https://docs.google.com/forms/d/e/abc/viewform")
    assert target is not None and target.kind == "google_form" and target.export_url == ""


def test_drive_folder_is_recognised_but_has_no_export():
    target = rewrite_for_export("https://drive.google.com/drive/folders/" + ID)
    assert target is not None and target.kind == "drive_folder" and target.export_url == ""


def test_folder_link_is_reported_not_fetched():
    def fetcher(url):  # pragma: no cover
        raise AssertionError("a folder must not be fetched")

    obs = adapter(fetcher).fetch("https://drive.google.com/drive/folders/" + ID)
    assert obs.kind == "error"
    assert "LINK NOT READ [unsupported_type]" in obs.error
    assert "folder" in obs.error


# ---------------------------------------------------------------------------
# Adapter: reads through the export URL
# ---------------------------------------------------------------------------


def test_google_doc_is_read_through_the_export_url():
    seen: List[str] = []

    def fetcher(url):
        seen.append(url)
        return text_page("\ufeffQuarterly plan\n" + "Goal one is to grow the list. " * 8)

    obs = adapter(fetcher).fetch(f"https://docs.google.com/document/d/{ID}/edit")
    assert obs.kind == "read"
    assert seen == [f"https://docs.google.com/document/d/{ID}/export?format=txt"]
    assert "Quarterly plan" in obs.text and "\ufeff" not in obs.text
    assert f"/document/d/{ID}/edit" in obs.text  # cited under the link the user gave


def test_sheet_tab_gid_is_kept_and_tabs_do_not_share_a_cache_entry():
    seen: List[str] = []

    def fetcher(url):
        seen.append(url)
        return text_page("name,score\n" + "ann,10\n" * 60, "text/csv")

    a = adapter(fetcher)
    a.fetch(f"https://docs.google.com/spreadsheets/d/{ID}/edit#gid=11")
    a.fetch(f"https://docs.google.com/spreadsheets/d/{ID}/edit#gid=22")
    assert seen == [
        f"https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&gid=11",
        f"https://docs.google.com/spreadsheets/d/{ID}/export?format=csv&gid=22",
    ]


def test_slides_and_drive_file_use_their_export_urls():
    seen: List[str] = []

    def fetcher(url):
        seen.append(url)
        return text_page("Slide one: the plan. " * 30)

    a = adapter(fetcher)
    assert a.fetch(f"https://docs.google.com/presentation/d/{ID}/edit").kind == "read"
    assert a.fetch(f"https://drive.google.com/file/d/{ID}/view").kind == "read"
    assert seen[0].endswith("/export?format=txt")
    assert seen[1] == f"https://drive.google.com/uc?export=download&id={ID}"


def test_the_ssrf_guard_runs_on_the_rewritten_url(monkeypatch):
    calls: List[str] = []
    real = wr.check_url_is_safe

    def guard(url):
        calls.append(url)
        if "export?format=txt" in url:
            return "refused: rewritten url is not allowed"
        return real(url) if False else None

    monkeypatch.setattr(wr, "check_url_is_safe", guard)

    def fetcher(url):  # pragma: no cover
        raise AssertionError("must not fetch a refused rewritten url")

    obs = adapter(fetcher).fetch(f"https://docs.google.com/document/d/{ID}/edit")
    assert obs.kind == "error" and "refused" in obs.error
    assert any("export?format=txt" in c for c in calls)


# ---------------------------------------------------------------------------
# Default fetcher: typed failures, redirects, size cap
# ---------------------------------------------------------------------------


class FakeStream:
    def __init__(self, status_code, headers=None, body=b""):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        for i in range(0, len(self._body), 65536):
            yield self._body[i:i + 65536]
        if not self._body:
            yield b""


def patch_stream(monkeypatch, script: Dict[str, FakeStream]):
    import httpx

    from quest_ai_runner.adapters import web_page_extract

    monkeypatch.setattr(web_page_extract, "default_resolver", lambda host: ["93.184.216.34"])
    seen: List[str] = []

    def fake_stream(method, url, **kwargs):
        seen.append(url)
        return script[url]

    monkeypatch.setattr(httpx, "stream", fake_stream)
    return seen


EXPORT = f"https://docs.google.com/document/d/{ID}/export?format=txt"


def reason_of(monkeypatch, script, url=EXPORT):
    patch_stream(monkeypatch, script)
    with pytest.raises(PageFetchError) as exc:
        _default_page_fetcher(url)
    return exc.value.reason


def test_signin_redirect_is_not_shared_and_is_not_followed(monkeypatch):
    script = {EXPORT: FakeStream(307, {"location": "https://accounts.google.com/ServiceLogin?continue=x"})}
    seen = patch_stream(monkeypatch, script)
    with pytest.raises(PageFetchError) as exc:
        _default_page_fetcher(EXPORT)
    assert exc.value.reason == wla.REASON_NOT_SHARED
    assert seen == [EXPORT]  # the sign-in page was never opened


def test_google_403_is_not_shared(monkeypatch):
    assert reason_of(monkeypatch, {EXPORT: FakeStream(403)}) == wla.REASON_NOT_SHARED


def test_google_401_is_not_shared(monkeypatch):
    assert reason_of(monkeypatch, {EXPORT: FakeStream(401)}) == wla.REASON_NOT_SHARED


def test_404_is_not_found(monkeypatch):
    assert reason_of(monkeypatch, {EXPORT: FakeStream(404)}) == wla.REASON_NOT_FOUND


def test_other_site_403_and_429_are_blocked(monkeypatch):
    url = "https://news.example/story"
    assert reason_of(monkeypatch, {url: FakeStream(403)}, url) == wla.REASON_BLOCKED
    assert reason_of(monkeypatch, {url: FakeStream(429)}, url) == wla.REASON_BLOCKED


def test_server_error_is_unreachable(monkeypatch):
    assert reason_of(monkeypatch, {EXPORT: FakeStream(503)}) == wla.REASON_UNREACHABLE


@pytest.mark.parametrize("ctype", ["video/mp4", "image/png", "audio/mpeg"])
def test_media_is_unsupported(monkeypatch, ctype):
    assert reason_of(monkeypatch, {EXPORT: FakeStream(200, {"content-type": ctype}, b"x")}) == wla.REASON_UNSUPPORTED_TYPE


def test_office_binary_is_unsupported(monkeypatch):
    ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert reason_of(monkeypatch, {EXPORT: FakeStream(200, {"content-type": ctype}, b"PK")}) == wla.REASON_UNSUPPORTED_TYPE


def test_octet_stream_binary_is_unsupported(monkeypatch):
    body = b"\x00\x01\x02binary"
    assert reason_of(monkeypatch, {EXPORT: FakeStream(200, {"content-type": "application/octet-stream"}, body)}) == wla.REASON_UNSUPPORTED_TYPE


def test_pdf_over_the_size_cap_is_too_large(monkeypatch):
    body = b"%PDF-1.4 " + b"a" * (wr._MAX_PAGE_BYTES + 10)
    assert reason_of(monkeypatch, {EXPORT: FakeStream(200, {"content-type": "application/pdf"}, body)}) == wla.REASON_TOO_LARGE


def test_declared_length_over_the_cap_is_too_large_without_reading(monkeypatch):
    headers = {"content-type": "application/pdf", "content-length": str(wr._MAX_PAGE_BYTES + 1)}
    assert reason_of(monkeypatch, {EXPORT: FakeStream(200, headers, b"%PDF")}) == wla.REASON_TOO_LARGE


def test_text_over_the_cap_is_truncated_with_a_note(monkeypatch):
    body = (b"word " * (wr._MAX_PAGE_BYTES // 5 + 100))
    patch_stream(monkeypatch, {EXPORT: FakeStream(200, {"content-type": "text/plain"}, body)})
    page = _default_page_fetcher(EXPORT)
    assert page.truncated is True
    assert wr._with_truncation_note("hello", page).endswith("only the start was read.]")


def test_redirect_from_the_export_url_into_a_private_address_is_refused(monkeypatch):
    script = {EXPORT: FakeStream(302, {"location": "http://169.254.169.254/latest/meta-data/"})}
    seen = patch_stream(monkeypatch, script)
    with pytest.raises(PageFetchError) as exc:
        _default_page_fetcher(EXPORT)
    assert "169.254.169.254" in str(exc.value)
    assert seen == [EXPORT]


def test_a_googleusercontent_download_redirect_is_followed(monkeypatch):
    dl = "https://doc-0c.googleusercontent.com/download/abc"
    script = {
        EXPORT: FakeStream(307, {"location": dl}),
        dl: FakeStream(200, {"content-type": "text/plain; charset=utf-8"}, "hello doc".encode()),
    }
    patch_stream(monkeypatch, script)
    assert _default_page_fetcher(EXPORT).html == "hello doc"


# ---------------------------------------------------------------------------
# Adapter: every reason becomes the right user-facing error
# ---------------------------------------------------------------------------


def failing(reason, message="x"):
    def fetcher(url):
        raise PageFetchError(message, reason)
    return fetcher


@pytest.mark.parametrize("reason", [
    wla.REASON_NOT_SHARED, wla.REASON_NOT_FOUND, wla.REASON_TOO_LARGE, wla.REASON_UNSUPPORTED_TYPE,
])
def test_hard_reasons_are_reported_without_a_paid_fallback(reason):
    calls = []
    obs = adapter(failing(reason), url_fetch_fallback=lambda u: calls.append(u) or "paid text " * 100).fetch(
        f"https://docs.google.com/document/d/{ID}/edit")
    assert obs.kind == "error"
    assert obs.error.startswith(f"LINK NOT READ [{reason}]")
    assert REASON_MESSAGES[reason] in obs.error
    assert calls == []


@pytest.mark.parametrize("reason", [wla.REASON_BLOCKED, wla.REASON_UNREACHABLE])
def test_soft_reasons_try_the_fallback_then_report(reason):
    calls = []
    obs = adapter(failing(reason), url_fetch_fallback=lambda u: calls.append(u) or "").fetch("https://news.example/a")
    assert calls == ["https://news.example/a"]
    assert obs.error.startswith(f"LINK NOT READ [{reason}]")
    assert REASON_MESSAGES[reason] in obs.error


def test_thin_js_page_is_js_shell():
    shell = "<html><head><script src='app.js'></script></head><body><div id='root'></div></body></html>"
    obs = adapter(lambda u: text_page(shell, "text/html")).fetch("https://app.example/")
    assert obs.error.startswith("LINK NOT READ [js_shell]")
    assert REASON_MESSAGES[wla.REASON_JS_SHELL] in obs.error


def test_login_wall_page_is_not_shared():
    wall = "<html><body><h1>Sign in</h1><p>Please log in to continue.</p></body></html>"
    obs = adapter(lambda u: text_page(wall, "text/html")).fetch("https://portal.example/doc/1")
    assert obs.error.startswith("LINK NOT READ [not_shared]")


def test_bot_wall_page_is_blocked():
    wall = "<html><body><p>Verify you are human to continue</p></body></html>"
    obs = adapter(lambda u: text_page(wall, "text/html")).fetch("https://shop.example/x")
    assert obs.error.startswith("LINK NOT READ [blocked]")


def test_google_html_instead_of_an_export_is_not_shared():
    html = "<!DOCTYPE html><html><body>Sign in to Google</body></html>"
    obs = adapter(lambda u: text_page(html, "text/html")).fetch(f"https://docs.google.com/document/d/{ID}/edit")
    assert obs.error.startswith("LINK NOT READ [not_shared]")


def test_drive_too_large_to_scan_page_is_too_large():
    html = "<html><body>Google Drive can't scan this file for viruses. report.zip is too large</body></html>"
    obs = adapter(lambda u: text_page(html, "text/html")).fetch(f"https://drive.google.com/file/d/{ID}/view")
    assert obs.error.startswith("LINK NOT READ [too_large]")


def test_none_of_the_user_messages_blame_google_links_in_general_or_use_em_dashes():
    for reason, message in REASON_MESSAGES.items():
        assert "\u2014" not in message and "\u2013" not in message
        assert "cannot open Google" not in message and "can't open Google" not in message
    assert set(REASON_MESSAGES) == set(ALL_REASONS)
    not_shared = REASON_MESSAGES[wla.REASON_NOT_SHARED].lower()
    assert "anyone with the link" in not_shared and "paste" in not_shared
    for text in (wla.PDF_NO_READER_MESSAGE, wla.PDF_NO_TEXT_MESSAGE, wla.FOLDER_MESSAGE):
        assert "\u2014" not in text


# ---------------------------------------------------------------------------
# PDFs
# ---------------------------------------------------------------------------


def install_fake_pypdf(monkeypatch, pages, encrypted=False):
    class Page:
        def __init__(self, t):
            self.t = t

        def extract_text(self):
            return self.t

    class Reader:
        def __init__(self, stream):
            self.pages = [Page(t) for t in pages]
            self.is_encrypted = encrypted

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=Reader))


def test_pdf_text_is_extracted(monkeypatch):
    install_fake_pypdf(monkeypatch, ["Annual report. Revenue grew twelve percent.", "Appendix details here."])
    assert "Revenue grew" in extract_pdf_text(b"%PDF-1.7")


def test_pdf_without_pypdf_degrades_honestly(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)  # makes `import pypdf` raise ImportError
    with pytest.raises(wla.LinkReadError) as exc:
        extract_pdf_text(b"%PDF-1.7")
    assert exc.value.reason == wla.REASON_UNSUPPORTED_TYPE
    assert exc.value.user_message == wla.PDF_NO_READER_MESSAGE


def test_scanned_pdf_with_no_text_is_reported(monkeypatch):
    install_fake_pypdf(monkeypatch, ["", ""])
    with pytest.raises(wla.LinkReadError) as exc:
        extract_pdf_text(b"%PDF-1.7")
    assert exc.value.user_message == wla.PDF_NO_TEXT_MESSAGE


def test_encrypted_pdf_is_reported(monkeypatch):
    install_fake_pypdf(monkeypatch, ["x"], encrypted=True)
    with pytest.raises(wla.LinkReadError):
        extract_pdf_text(b"%PDF-1.7")


def test_pdf_link_end_to_end(monkeypatch):
    install_fake_pypdf(monkeypatch, ["Grant narrative. We will train two hundred mentors this year. " * 5])
    pdf = FetchedPage(html="", content_type="application/pdf", status_code=200, data=b"%PDF-1.7")
    obs = adapter(lambda u: pdf).fetch("https://example.org/files/narrative.pdf", focus="mentors")
    assert obs.kind == "read" and "two hundred mentors" in obs.text


def test_pdf_link_without_pypdf_end_to_end(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdf", None)
    pdf = FetchedPage(html="", content_type="application/pdf", status_code=200, data=b"%PDF-1.7")
    obs = adapter(lambda u: pdf).fetch(f"https://drive.google.com/file/d/{ID}/view")
    assert obs.error.startswith("LINK NOT READ [unsupported_type]")
    assert wla.PDF_NO_READER_MESSAGE in obs.error


def test_unsupported_content_type_error_still_surfaces_its_own_message():
    def fetcher(url):
        raise UnsupportedContentTypeError("web fetch: request a deep run to read PDFs")

    obs = adapter(fetcher).fetch("https://example.com/doc.bin")
    assert "deep run" in obs.error and obs.error.startswith("LINK NOT READ [unsupported_type]")


# ---------------------------------------------------------------------------
# Deterministic URL detection and the one links rule
# ---------------------------------------------------------------------------


def test_extract_urls_handles_prose_markdown_and_duplicates():
    msg = (f"Please read https://docs.google.com/document/d/{ID}/edit, and also "
           "(https://example.org/a.pdf). See [the site](https://example.org/x?y=1). "
           "https://example.org/a.pdf again. Wiki: https://en.wikipedia.org/wiki/Foo_(bar).")
    assert extract_urls(msg) == [
        f"https://docs.google.com/document/d/{ID}/edit",
        "https://example.org/a.pdf",
        "https://example.org/x?y=1",
        "https://en.wikipedia.org/wiki/Foo_(bar)",
    ]


def test_extract_urls_none_and_junk():
    assert extract_urls("") == [] and extract_urls(None) == [] and extract_urls("no links here") == []
    assert extract_urls("see http:// nothing") == []


def test_describe_link():
    assert describe_link(f"https://docs.google.com/spreadsheets/d/{ID}/edit") == "Google Sheet"
    assert describe_link(f"https://docs.google.com/document/d/{ID}/edit") == "Google Doc"
    assert describe_link(f"https://docs.google.com/presentation/d/{ID}/edit") == "Google Slides"
    assert describe_link(f"https://drive.google.com/file/d/{ID}/view") == "Google Drive file"
    assert describe_link("https://x.org/a.PDF") == "PDF"
    assert describe_link("https://x.org/") == "web page"


def test_links_block_lists_the_links_for_the_planner():
    block = orch.planner_links_block(f"summarize https://docs.google.com/document/d/{ID}/edit", object())
    assert "LINKS IN THIS MESSAGE" in block and f"1. https://docs.google.com/document/d/{ID}/edit (Google Doc)" in block
    assert "READ it first" in block and "web_page" in block


def test_links_block_absent_without_links_and_honest_without_a_reader():
    assert orch.planner_links_block("no link here", object()) == ""
    no_reader = orch.planner_links_block("see https://example.org/a", None)
    assert "have NOT read these" in no_reader and "paste the text" in no_reader


def test_links_block_caps_the_list():
    msg = " ".join(f"https://example.org/{i}" for i in range(8))
    block = orch.planner_links_block(msg, object())
    assert "5. https://example.org/4" in block and "6. " not in block and "+3 more" in block


def test_one_consistent_link_rule_across_the_prompt():
    block = orch.planner_links_block("https://example.org/a", object())
    # The ONE rule: read with or without a question; on failure, say why using the typed reason.
    assert "with or without a question" in block
    assert "LINK NOT READ [reason]" in block
    assert "Never say you cannot open Google links" in block
    # The two older rules now defer to it instead of contradicting it.
    import inspect
    src = inspect.getsource(orch)
    assert "A bare pasted file or quoted text with no" in src
    assert "A bare pasted link, file" not in src
    assert "this hand-off rule never overrides it" in src.lower().replace("\n", " ").replace("  ", " ")
    assert "LINKS IN THIS MESSAGE" in orch.PLANNER_WEB_HEAD


def test_prompt_constants_have_no_em_dashes():
    for text in (orch.PLANNER_LINKS_HEAD, orch.PLANNER_LINKS_NO_READER, orch.LINK_NOT_READ_ANSWER_RULE,
                 orch.PLANNER_WEB_HEAD):
        assert "\u2014" not in text


def test_marker_matches_the_error_prefix_and_the_answer_rule_is_added():
    err = link_not_read_error("https://x.org", wla.REASON_NOT_SHARED)
    assert err.startswith(orch.LINK_NOT_READ_MARKER)
    tail = orch.grounding_answer_tail([{"kind": "error", "error": err}], False)
    assert orch.LINK_NOT_READ_ANSWER_RULE in tail
    assert orch.LINK_NOT_READ_ANSWER_RULE not in orch.grounding_answer_tail(
        [{"kind": "read", "text": "fine"}], False)
