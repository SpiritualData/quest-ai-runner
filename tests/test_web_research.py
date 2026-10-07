"""Tests for WebResearchAdapter -- offline (fake backend / fake page_fetcher), no network."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from quest_ai_runner.adapters.web_cache import WebCache
from quest_ai_runner.adapters.web_research import (
    UnsupportedContentTypeError,
    WebResearchAdapter,
    build_web_research_from_env,
    canonicalize_url,
    estimate_tokens,
)
from quest_ai_runner.adapters.web_search_backends import SearchBackendError, SearchHit, SearchResponse


class FakeBackend:
    name = "fake-backend"

    def __init__(self, responses: Optional[Dict[str, SearchResponse]] = None, error_on: Optional[Dict[str, Exception]] = None):
        self.responses = responses or {}
        self.error_on = error_on or {}
        self.calls: List[str] = []

    def search(self, query, *, max_results=5):
        self.calls.append(query)
        if query in self.error_on:
            raise self.error_on[query]
        return self.responses.get(query, SearchResponse(hits=[], answer=""))


def _hit(title, url, snippet="snippet text", date=""):
    return SearchHit(title=title, url=url, snippet=snippet, date=date)


# ---------------------------------------------------------------------------
# search(): single query, basic shape
# ---------------------------------------------------------------------------


def test_search_single_query_formats_results_with_citations():
    backend = FakeBackend({"keyboards": SearchResponse(hits=[_hit("Keyboards 2024", "https://example.com/kb", "Top picks.")], answer="Buy the X1.")})
    adapter = WebResearchAdapter(backend, cache=WebCache())
    obs = adapter.search("keyboards")
    assert obs.kind == "query"
    assert obs.rel_path == "web_search:keyboards"
    assert "fake-backend" in obs.text
    assert "[title](url)" in obs.text
    assert "Keyboards 2024 | https://example.com/kb" in obs.text
    assert "Buy the X1." in obs.text
    assert obs.hits[0]["source"] == "fake-backend"
    assert obs.hits[0]["query"] == "keyboards"
    assert 'web_page' in obs.text  # the follow-up fetch hint


def test_search_no_query_is_an_error():
    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache())
    obs = adapter.search("")
    assert obs.kind == "error"


def test_search_all_queries_fail_returns_error_with_backend_message():
    backend = FakeBackend(error_on={"q": SearchBackendError("fake-backend: HTTP 429 rate limited")})
    adapter = WebResearchAdapter(backend, cache=WebCache())
    obs = adapter.search("q")
    assert obs.kind == "error"
    assert "fake-backend: HTTP 429 rate limited" in obs.error


# ---------------------------------------------------------------------------
# search(): multi-query concurrency, dedupe, partial failure
# ---------------------------------------------------------------------------


def test_search_multi_query_dedupes_by_normalized_url_and_runs_concurrently():
    shared_hit = _hit("Shared Page", "https://example.com/shared?utm_source=x")
    backend = FakeBackend(
        {
            "q1": SearchResponse(hits=[shared_hit, _hit("Only In Q1", "https://example.com/q1-only")]),
            "q2": SearchResponse(hits=[_hit("Shared Page Again", "https://example.com/shared"), _hit("Only In Q2", "https://example.com/q2-only")]),
        }
    )
    adapter = WebResearchAdapter(backend, cache=WebCache(), max_parallel=4)
    obs = adapter.search(["q1", "q2"])
    assert obs.kind == "query"
    urls = [h["url"] for h in obs.hits]
    # The utm_source-tagged duplicate of /shared should appear only once total.
    shared_count = sum(1 for u in urls if canonicalize_url(u) == canonicalize_url("https://example.com/shared"))
    assert shared_count == 1
    assert "https://example.com/q1-only" in urls
    assert "https://example.com/q2-only" in urls
    assert sorted(backend.calls) == ["q1", "q2"]


def test_search_partial_failure_reports_inline_and_keeps_good_results():
    backend = FakeBackend(
        responses={"good": SearchResponse(hits=[_hit("Good Hit", "https://example.com/good")])},
        error_on={"bad": SearchBackendError("fake-backend: HTTP 500")},
    )
    adapter = WebResearchAdapter(backend, cache=WebCache())
    obs = adapter.search(["good", "bad"])
    assert obs.kind == "query"  # at least one query succeeded
    assert "Good Hit" in obs.text
    assert 'WEB RESULTS for "bad": error' in obs.text
    assert "HTTP 500" in obs.text


# ---------------------------------------------------------------------------
# search(): output size stays small (snippet-first, token-estimated)
# ---------------------------------------------------------------------------


def test_search_output_stays_under_600_tokens_for_five_results():
    hits = [_hit(f"Result {i}", f"https://example.com/r{i}", "A reasonably short snippet describing result " + str(i) + ".") for i in range(5)]
    backend = FakeBackend({"q": SearchResponse(hits=hits, answer="A short synthesized answer summarizing the five results.")})
    adapter = WebResearchAdapter(backend, cache=WebCache(), max_results=5)
    obs = adapter.search("q")
    assert estimate_tokens(obs.text) < 600


# ---------------------------------------------------------------------------
# search(): caching -- hit avoids a second backend call, fresh bypasses the read
# ---------------------------------------------------------------------------


def test_search_cache_hit_avoids_second_backend_call():
    backend = FakeBackend({"q": SearchResponse(hits=[_hit("H", "https://example.com/h")])})
    cache = WebCache()
    adapter = WebResearchAdapter(backend, cache=cache)
    adapter.search("q")
    adapter.search("q")
    assert backend.calls == ["q"]  # second call served from cache


def test_search_fresh_bypasses_cache_read_but_still_writes():
    backend = FakeBackend({"q": SearchResponse(hits=[_hit("H", "https://example.com/h")])})
    cache = WebCache()
    adapter = WebResearchAdapter(backend, cache=cache)
    adapter.search("q")
    adapter.search("q", fresh=True)
    assert backend.calls == ["q", "q"]  # fresh forced a real call


def test_search_failed_lookup_is_never_cached():
    backend = FakeBackend(error_on={"q": SearchBackendError("down")})
    cache = WebCache()
    adapter = WebResearchAdapter(backend, cache=cache)
    adapter.search("q")
    assert cache.get("web_search", "fake-backend|5|q") is None


# ---------------------------------------------------------------------------
# search(): daily cost guard (requested addition)
# ---------------------------------------------------------------------------


def test_search_daily_limit_blocks_without_calling_backend():
    backend = FakeBackend({"q1": SearchResponse(hits=[_hit("H1", "https://example.com/1")]), "q2": SearchResponse(hits=[_hit("H2", "https://example.com/2")])})
    adapter = WebResearchAdapter(backend, cache=WebCache(), daily_limit=1)
    obs1 = adapter.search("q1")
    assert obs1.kind == "query"
    assert backend.calls == ["q1"]

    obs2 = adapter.search("q2")
    assert obs2.kind == "error"
    assert "daily limit" in obs2.error.lower()
    assert backend.calls == ["q1"]  # backend never called a second time


def test_search_daily_limit_does_not_count_cache_hits():
    backend = FakeBackend({"q1": SearchResponse(hits=[_hit("H1", "https://example.com/1")])})
    cache = WebCache()
    adapter = WebResearchAdapter(backend, cache=cache, daily_limit=1)
    adapter.search("q1")  # consumes the one allowed real call
    assert backend.calls == ["q1"]

    obs = adapter.search("q1")  # same query -> served from cache, no limiter check needed
    assert obs.kind == "query"
    assert backend.calls == ["q1"]  # still only one real call ever made


def test_no_daily_limit_by_default():
    backend = FakeBackend({f"q{i}": SearchResponse(hits=[_hit("H", f"https://example.com/{i}")]) for i in range(5)})
    adapter = WebResearchAdapter(backend, cache=WebCache())
    for i in range(5):
        obs = adapter.search(f"q{i}")
        assert obs.kind == "query"
    assert len(backend.calls) == 5


# ---------------------------------------------------------------------------
# fetch(): SSRF refusal
# ---------------------------------------------------------------------------


def test_fetch_refuses_private_address():
    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache())
    obs = adapter.fetch("http://127.0.0.1/secret")
    assert obs.kind == "error"
    assert "refused" in obs.error


def test_fetch_refuses_file_scheme():
    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache())
    obs = adapter.fetch("file:///etc/passwd")
    assert obs.kind == "error"


# ---------------------------------------------------------------------------
# fetch(): extraction, focus, budget, caching
# ---------------------------------------------------------------------------


_ARTICLE_HTML = """
<html><head><title>Great Article</title></head><body>
<nav>Home About</nav>
<article>
<p>This paragraph talks about quarterly earnings and revenue growth figures for the company,
with plenty of financial detail that a reader focused on finance would actually want to read.</p>
<p>This second paragraph is entirely about a completely different topic: the migration patterns
of arctic birds during the winter season, with no financial content whatsoever in it at all.</p>
</article>
</body></html>
"""


def test_fetch_extracts_and_scores_passages_for_focus():
    def fake_fetcher(url):
        return SimpleNamespace(html=_ARTICLE_HTML, content_type="text/html", status_code=200)

    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache(), page_fetcher=fake_fetcher)
    obs = adapter.fetch("https://example.com/article", focus="quarterly earnings revenue")
    assert obs.kind == "read"
    assert "PAGE: Great Article" in obs.text
    assert "quarterly earnings" in obs.text
    assert "Home About" not in obs.text


def test_fetch_caches_full_text_and_does_not_refetch():
    call_count = {"n": 0}

    def fake_fetcher(url):
        call_count["n"] += 1
        return SimpleNamespace(html=_ARTICLE_HTML, content_type="text/html", status_code=200)

    cache = WebCache()
    adapter = WebResearchAdapter(FakeBackend(), cache=cache, page_fetcher=fake_fetcher)
    adapter.fetch("https://example.com/article", focus="birds")
    adapter.fetch("https://example.com/article", focus="earnings")  # different focus, same page
    assert call_count["n"] == 1  # page fetched once; passage selection re-ran per focus


def test_fetch_pdf_content_type_returns_error_suggesting_deep_run():
    def fake_fetcher(url):
        raise UnsupportedContentTypeError(f"web fetch: {url} is a PDF; request a deep run to read PDFs")

    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache(), page_fetcher=fake_fetcher)
    obs = adapter.fetch("https://example.com/doc.pdf")
    assert obs.kind == "error"
    assert "deep run" in obs.error


def test_fetch_uses_url_fetch_fallback_when_extraction_is_thin():
    def thin_fetcher(url):
        return SimpleNamespace(html="<html><body><div id='app'></div></body></html>", content_type="text/html", status_code=200)

    fallback_calls = []

    def fallback(url):
        fallback_calls.append(url)
        return "This is the real page content retrieved by the fallback mechanism instead, " * 5

    adapter = WebResearchAdapter(
        FakeBackend(), cache=WebCache(), page_fetcher=thin_fetcher, url_fetch_fallback=fallback
    )
    obs = adapter.fetch("https://example.com/js-shell")
    assert obs.kind == "read"
    assert fallback_calls == ["https://example.com/js-shell"]
    assert "real page content" in obs.text


def test_fetch_fallback_not_used_when_primary_extraction_is_sufficient():
    def good_fetcher(url):
        return SimpleNamespace(html=_ARTICLE_HTML, content_type="text/html", status_code=200)

    fallback_calls = []

    def fallback(url):
        fallback_calls.append(url)
        return "should not be used"

    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache(), page_fetcher=good_fetcher, url_fetch_fallback=fallback)
    adapter.fetch("https://example.com/article")
    assert fallback_calls == []


def test_fetch_no_content_anywhere_is_an_error():
    def empty_fetcher(url):
        return SimpleNamespace(html="<html><body></body></html>", content_type="text/html", status_code=200)

    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache(), page_fetcher=empty_fetcher, url_fetch_fallback=None)
    obs = adapter.fetch("https://example.com/empty")
    assert obs.kind == "error"
    assert "no extractable content" in obs.error


# ---------------------------------------------------------------------------
# fetch(): the daily cost guard counts the PAID fallback, never the free direct fetch
# ---------------------------------------------------------------------------


def test_fetch_fallback_counted_against_the_daily_limit():
    def thin_fetcher(url):
        return SimpleNamespace(html="<html><body><div id='app'></div></body></html>",
                               content_type="text/html", status_code=200)

    fallback_calls = []

    def fallback(url):
        fallback_calls.append(url)
        return "This is the real page content retrieved by the fallback mechanism instead, " * 5

    adapter = WebResearchAdapter(
        FakeBackend(), cache=WebCache(), page_fetcher=thin_fetcher, url_fetch_fallback=fallback,
        daily_limit=1,
    )
    obs = adapter.fetch("https://example.com/js-shell")
    assert obs.kind == "read"
    assert fallback_calls == ["https://example.com/js-shell"]  # the one allowed paid call ran


def test_fetch_fallback_skipped_once_the_daily_limit_is_reached():
    def thin_fetcher(url):
        return SimpleNamespace(html="<html><body><div id='app'></div></body></html>",
                               content_type="text/html", status_code=200)

    fallback_calls = []

    def fallback(url):
        fallback_calls.append(url)
        return "This is the real page content retrieved by the fallback mechanism instead, " * 5

    adapter = WebResearchAdapter(
        FakeBackend(), cache=WebCache(), page_fetcher=thin_fetcher, url_fetch_fallback=fallback,
        daily_limit=1,
    )
    adapter.fetch("https://example.com/first")  # spends the one allowed fallback call
    assert fallback_calls == ["https://example.com/first"]

    obs = adapter.fetch("https://example.com/second")  # different URL, no cache hit either
    assert fallback_calls == ["https://example.com/first"]  # never called a second time
    assert obs.kind == "error"
    assert "daily limit" in obs.error.lower()


def test_fetch_direct_fetch_never_counted_against_the_daily_limit():
    def good_fetcher(url):
        return SimpleNamespace(html=_ARTICLE_HTML, content_type="text/html", status_code=200)

    fallback_calls = []

    def fallback(url):
        fallback_calls.append(url)
        return "should not be used"

    adapter = WebResearchAdapter(
        FakeBackend(), cache=WebCache(), page_fetcher=good_fetcher, url_fetch_fallback=fallback,
        daily_limit=1,
    )
    for i in range(5):
        obs = adapter.fetch(f"https://example.com/article-{i}")
        assert obs.kind == "read"
    assert fallback_calls == []  # direct fetches are sufficient and free; never counted


def test_fetch_empty_focus_keeps_leading_passages():
    def fake_fetcher(url):
        return SimpleNamespace(html=_ARTICLE_HTML, content_type="text/html", status_code=200)

    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache(), page_fetcher=fake_fetcher)
    obs = adapter.fetch("https://example.com/article")
    assert obs.kind == "read"
    assert "quarterly earnings" in obs.text  # the leading paragraph is kept


# ---------------------------------------------------------------------------
# describe() / backend_name
# ---------------------------------------------------------------------------


def test_describe_and_backend_name():
    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache())
    assert adapter.backend_name == "fake-backend"
    assert adapter.describe() == "web search via fake-backend"


# ---------------------------------------------------------------------------
# build_web_research_from_env
# ---------------------------------------------------------------------------


def test_build_from_env_returns_none_when_disabled():
    env = {"WEB_SEARCH_ENABLED": "false", "SERPER_API_KEY": "k"}
    assert build_web_research_from_env(env) is None


def test_build_from_env_returns_none_without_any_backend_config():
    assert build_web_research_from_env({}) is None


def test_build_from_env_wires_backend_and_tuning(tmp_path):
    env = {
        "SERPER_API_KEY": "sk-1",
        "WEB_SEARCH_MAX_RESULTS": "3",
        "QAR_WEB_PAGE_TOKEN_BUDGET": "500",
        "QAR_WEB_CACHE_DIR": str(tmp_path),
        "QAR_WEB_SEARCH_DAILY_LIMIT": "10",
    }
    adapter = build_web_research_from_env(env)
    assert adapter is not None
    assert adapter.backend_name == "serper"
    assert adapter._max_results == 3
    assert adapter._page_token_budget == 500
    assert adapter._cache.directory is not None


def test_build_from_env_wires_gemini_url_fallback_when_gemini_key_present():
    env = {"SERPER_API_KEY": "sk-1", "GOOGLE_API_KEY": "g-key"}
    adapter = build_web_research_from_env(env)
    assert adapter is not None
    assert adapter._url_fetch_fallback is not None


def test_build_from_env_no_fallback_without_gemini_key():
    env = {"SERPER_API_KEY": "sk-1"}
    adapter = build_web_research_from_env(env)
    assert adapter is not None
    assert adapter._url_fetch_fallback is None


# ---------------------------------------------------------------------------
# _default_page_fetcher: redirects are re-checked by the SSRF guard, and the page's
# declared charset is honored
# ---------------------------------------------------------------------------


class _FakeStream:
    """A stand-in for httpx.stream's context manager."""

    def __init__(self, status_code: int, headers: Dict[str, str], body: bytes = b"") -> None:
        self.status_code = status_code
        self.headers = headers
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self):
        yield self._body


def _patch_stream(monkeypatch, script):
    """Make httpx.stream return the scripted response for each URL, recording the order.

    Also stubs the SSRF guard's DNS resolver so the per-hop re-check runs its real logic against
    a fake public address instead of making a real DNS query from the test host.
    """
    import httpx

    from quest_ai_runner.adapters import web_page_extract

    monkeypatch.setattr(web_page_extract, "default_resolver", lambda host: ["93.184.216.34"])

    seen: List[str] = []

    def fake_stream(method, url, **kwargs):
        seen.append(url)
        assert kwargs.get("follow_redirects") is False, "the fetcher must follow redirects by hand"
        return script[url]

    monkeypatch.setattr(httpx, "stream", fake_stream)
    return seen


def test_default_fetcher_refuses_a_redirect_into_a_private_address(monkeypatch):
    """follow_redirects=True would have walked straight past check_url_is_safe: the guard only
    ever saw the URL the planner asked for, so a public URL that 302s to localhost or a cloud
    metadata endpoint was fetched anyway."""
    from quest_ai_runner.adapters.web_research import PageFetchError, _default_page_fetcher

    script = {
        "https://public.example/go": _FakeStream(
            302, {"location": "http://169.254.169.254/latest/meta-data/"}
        ),
    }
    seen = _patch_stream(monkeypatch, script)
    with pytest.raises(PageFetchError) as excinfo:
        _default_page_fetcher("https://public.example/go")
    assert "169.254.169.254" in str(excinfo.value)
    assert "private/loopback/link-local" in str(excinfo.value)
    assert seen == ["https://public.example/go"]  # the metadata endpoint was never opened


def test_default_fetcher_follows_a_safe_relative_redirect(monkeypatch):
    from quest_ai_runner.adapters.web_research import _default_page_fetcher

    script = {
        "https://public.example/a": _FakeStream(301, {"location": "/b"}),
        "https://public.example/b": _FakeStream(
            200, {"content-type": "text/html; charset=utf-8"}, b"<html><p>hi</p></html>"
        ),
    }
    seen = _patch_stream(monkeypatch, script)
    page = _default_page_fetcher("https://public.example/a")
    assert page.status_code == 200
    assert "hi" in page.html
    assert seen == ["https://public.example/a", "https://public.example/b"]


def test_default_fetcher_stops_after_too_many_redirects(monkeypatch):
    from quest_ai_runner.adapters.web_research import PageFetchError, _default_page_fetcher

    url = "https://public.example/loop"
    script = {url: _FakeStream(302, {"location": url})}
    _patch_stream(monkeypatch, script)
    with pytest.raises(PageFetchError) as excinfo:
        _default_page_fetcher(url)
    assert "too many redirects" in str(excinfo.value)


def test_default_fetcher_decodes_the_declared_charset_not_assumed_utf8(monkeypatch):
    """A windows-1252 page decoded as UTF-8 turns every accented character into U+FFFD, which
    then poisons the focus scoring as well as the text the model reads."""
    from quest_ai_runner.adapters.web_research import _default_page_fetcher

    body = "<html><body><p>café résumé naïve</p></body></html>".encode("windows-1252")
    script = {
        "https://public.example/p": _FakeStream(
            200, {"content-type": "text/html; charset=windows-1252"}, body
        ),
    }
    _patch_stream(monkeypatch, script)
    page = _default_page_fetcher("https://public.example/p")
    assert "café résumé naïve" in page.html
    assert "�" not in page.html


def test_default_fetcher_sniffs_a_meta_charset_when_the_header_omits_one(monkeypatch):
    from quest_ai_runner.adapters.web_research import _default_page_fetcher

    body = (
        "<html><head><meta charset='iso-8859-1'></head><body><p>naïve</p></body></html>"
    ).encode("iso-8859-1")
    script = {
        "https://public.example/m": _FakeStream(200, {"content-type": "text/html"}, body),
    }
    _patch_stream(monkeypatch, script)
    page = _default_page_fetcher("https://public.example/m")
    assert "naïve" in page.html


# ---------------------------------------------------------------------------
# search(): a snippet that is just a span of the backend's own summary is not billed twice
# ---------------------------------------------------------------------------


def test_search_drops_a_snippet_already_contained_in_the_summary():
    """Gemini grounding builds each hit's snippet out of the answer's grounding supports, so the
    summary and the snippets are the same sentences. Measured live: ~40% of a 406-token search
    observation was the summary repeated back."""
    answer = "Widgets cost twelve dollars as of October 2026, up from ten dollars in June."
    hits = [
        _hit("Widget News", "https://example.com/1", "Widgets cost twelve dollars as of October 2026"),
        _hit("Widget Blog", "https://example.com/2", "An independent snippet with its own wording."),
    ]
    backend = FakeBackend({"widgets": SearchResponse(hits=hits, answer=answer)})
    adapter = WebResearchAdapter(backend, cache=WebCache())
    text = adapter.search("widgets").text
    assert text.count("Widgets cost twelve dollars as of October 2026") == 1
    assert "An independent snippet with its own wording." in text
    assert "https://example.com/1" in text  # the hit itself is still listed and citable


def test_search_keeps_a_snippet_whose_text_falls_past_the_summary_trim():
    long_answer = ("a" * 420) + " the tail sentence only the snippet shows."
    hits = [_hit("T", "https://example.com/1", "the tail sentence only the snippet shows.")]
    backend = FakeBackend({"q": SearchResponse(hits=hits, answer=long_answer)})
    adapter = WebResearchAdapter(backend, cache=WebCache())
    text = adapter.search("q").text
    assert "the tail sentence only the snippet shows." in text


# ---------------------------------------------------------------------------
# fetch(): the page token budget holds end to end, and a failure names its cause
# ---------------------------------------------------------------------------


def test_fetch_body_honors_the_token_budget_on_a_long_page():
    paragraphs = "".join(
        f"<p>{'alpha beta gamma delta epsilon ' * 25}</p>" for _ in range(60)
    )
    html = f"<html><head><title>Long</title></head><body><article>{paragraphs}</article></body></html>"

    def fake_fetcher(url):
        return SimpleNamespace(html=html, content_type="text/html", status_code=200)

    adapter = WebResearchAdapter(
        FakeBackend(), cache=WebCache(), page_fetcher=fake_fetcher, page_token_budget=800
    )
    obs = adapter.fetch("https://example.com/long", focus="gamma delta")
    assert obs.kind == "read"
    # One passage may land the total just over the budget; it must not be a multiple of it.
    assert estimate_tokens(obs.text) < 1100


def test_fetch_error_names_the_underlying_cause():
    def failing_fetcher(url):
        raise RuntimeError("web fetch: HTTP 403 for https://example.com/blocked")

    adapter = WebResearchAdapter(FakeBackend(), cache=WebCache(), page_fetcher=failing_fetcher)
    obs = adapter.fetch("https://example.com/blocked")
    assert obs.kind == "error"
    assert "403" in obs.error


def test_fetch_error_names_a_failing_fallback_fetcher():
    def failing_fetcher(url):
        raise RuntimeError("connect timeout")

    def failing_fallback(url):
        raise RuntimeError("url_context could not retrieve the page")

    adapter = WebResearchAdapter(
        FakeBackend(), cache=WebCache(), page_fetcher=failing_fetcher,
        url_fetch_fallback=failing_fallback,
    )
    obs = adapter.fetch("https://example.com/blocked")
    assert obs.kind == "error"
    assert "connect timeout" in obs.error
