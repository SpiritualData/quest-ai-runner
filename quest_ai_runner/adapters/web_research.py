"""web_research -- WebResearchAdapter: fast, snippet-first web search + focused page fetch.

This is the piece the orchestrator's planner actually calls. Two operations, deliberately
asymmetric in cost, mirroring how ChatGPT/Perplexity-style assistants stay fast and cheap:

  * ``search(queries)``  -- ALWAYS cheap: runs one or more queries concurrently, dedupes hits by
                             URL, and returns ONE compact Observation (title + url + ~240-char
                             snippet per hit, every hit citable as a markdown link). This is what
                             rides in every planner/answer call that sees it, so it stays terse.
  * ``fetch(url, focus)`` -- used only when the planner explicitly wants to read one page in full.
                              Extracts the page's main text, keeps only the passages relevant to
                              ``focus`` (BM25-lite; see ``web_page_extract``), and caps the result
                              to a token budget (default ~800 tokens) instead of dumping the whole
                              page into context.

Everything is cached (``WebCache``): identical searches and page fetches are free on a repeat.
Neither method ever raises; every failure comes back as ``Observation(kind="error", ...)``.

``build_web_research_from_env`` is the one-call constructor a consumer's ``config.py`` wires in:
it picks a backend (see ``web_search_backends.select_search_backend``), reads the tuning env vars,
and wires a Gemini ``url_context`` fallback for pages that resist direct extraction (JS shells,
403s) when a Gemini key happens to be configured.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os

from typing import Any, Callable, Dict, List, Mapping, Optional, Union

from esoi import implementation
from ..core.adapters import Observation
from .web_cache import normalize_query, WebCache
from .web_link_access import (
    classify_thin_page,
    drive_download_is_too_big,
    extract_pdf_text,
    FOLDER_MESSAGE,
    FORM_MESSAGE,
    KIND_DRIVE_FOLDER,
    KIND_FORM,
    link_not_read_error,
    LinkReadError,
    LinkTarget,
    looks_like_pdf,
    REASON_BLOCKED,
    REASON_JS_SHELL,
    REASON_MESSAGES,
    REASON_NOT_SHARED,
    REASON_TOO_LARGE,
    REASON_UNREACHABLE,
    REASON_UNSUPPORTED_TYPE,
    rewrite_for_export,
)
from .web_page_extract import (
    check_url_is_safe,
    estimate_tokens,
    extract_main_text,
    select_passages,
    split_passages,
)
from .web_research_fetch import (
    canonicalize_url,
    collapse_ws,
    default_page_fetcher,
    HARD_FAILURE_REASONS,
    MAX_PAGE_BYTES,
    PageFetchError,
    trim_chars,
)
from .web_research_limiter import DailyLimiter
from .web_search_backends import (
    SearchBackendError,
    SearchHit,
    SearchResponse,
    select_search_backend,
)

logger = logging.getLogger("quest-ai-runner.web-research")

_DEFAULT_MAX_RESULTS = 5
#: Focused read ({"web_page": url, "focus": ...}): the passages most relevant to the focus.
_DEFAULT_PAGE_TOKEN_BUDGET = 3000
#: Whole-document read ({"web_page": url, "full": true}): the text in order, one part at a time.
_DEFAULT_FULL_PAGE_TOKEN_BUDGET = 12000
_DEFAULT_SEARCH_TTL = 86400
_DEFAULT_PAGE_TTL = 604800
_SNIPPET_MAX_CHARS = 240
_SUMMARY_MAX_CHARS = 400
_MIN_EXTRACTED_CHARS = 200

_DAILY_LIMIT_MESSAGE = (
    "Web search daily limit reached for this deployment; answer from what you know and say "
    "the information may be out of date."
)
#: Shown (as the fetch's ``cause``) when a thin direct fetch has nothing else to fall back on
#: because the daily limit is reached: the paid ``url_fetch_fallback`` call is skipped outright.
_DAILY_LIMIT_FALLBACK_MESSAGE = (
    "Page needs a rendering fetch, but the web daily limit is reached for this deployment."
)


# WebResearchAdapter
# ---------------------------------------------------------------------------


@implementation("search-the-web", "web_research")
class WebResearchAdapter:
    """Snippet-first web search + focus-scored page fetch, for the orchestrator's planner.

    Not a general ``RetrievalAdapter``; a deliberately small two-method surface the orchestrator
    wires in and calls directly from its read-spec handling (``{"web": "<query>"}`` and
    ``{"web_page": "<url>", "focus": "..."}``).
    """

    def __init__(
        self,
        backend: "SearchBackend",
        *,
        max_results: int = 5,
        page_token_budget: int = _DEFAULT_PAGE_TOKEN_BUDGET,
        full_page_token_budget: int = _DEFAULT_FULL_PAGE_TOKEN_BUDGET,
        cache: Optional["WebCache"] = None,
        search_ttl_seconds: int = 86400,
        page_ttl_seconds: int = 604800,
        page_fetcher: Optional[Callable[[str], Any]] = None,
        url_fetch_fallback: Optional[Callable[[str], str]] = None,
        max_parallel: int = 4,
        daily_limit: Optional[int] = None,
    ) -> None:
        self._backend = backend
        self._max_results = max_results
        self._page_token_budget = page_token_budget
        self._full_page_token_budget = max(page_token_budget, full_page_token_budget)
        self._cache = cache if cache is not None else WebCache()
        self._search_ttl = search_ttl_seconds
        self._page_ttl = page_ttl_seconds
        self._page_fetcher = page_fetcher or default_page_fetcher
        self._url_fetch_fallback = url_fetch_fallback
        self._max_parallel = max(1, max_parallel)
        self._limiter = DailyLimiter(daily_limit, state_dir=self._cache.directory)

    @property
    def backend_name(self) -> str:
        return getattr(self._backend, "name", "unknown")

    def describe(self) -> str:
        return f"web search via {self.backend_name}"

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def search(
        self,
        queries: Union[str, List[str]],
        *,
        max_results: Optional[int] = None,
        fresh: bool = False,
    ) -> Observation:
        try:
            return self._search_inner(queries, max_results=max_results, fresh=fresh)
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebResearchAdapter.search failed", exc_info=True)
            return Observation(kind="error", error=f"web search failed: {exc}")

    def _search_inner(
        self, queries: Union[str, List[str]], *, max_results: Optional[int], fresh: bool
    ) -> Observation:
        if isinstance(queries, str):
            raw_list = [queries]
        else:
            raw_list = list(queries or [])
        query_list = [q.strip() for q in raw_list if q and q.strip()]
        if not query_list:
            return Observation(kind="error", error="web search: no query given")

        n = max_results or self._max_results
        results: Dict[str, SearchResponse] = {}
        errors: Dict[str, str] = {}

        n_workers = min(self._max_parallel, len(query_list))
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(self._search_one, q, n, fresh): q for q in query_list}
            for fut in concurrent.futures.as_completed(futures):
                q = futures[fut]
                try:
                    results[q] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    errors[q] = str(exc)

        ordered_results = [(q, results[q]) for q in query_list if q in results]

        if not ordered_results:
            if errors and all(e == _DAILY_LIMIT_MESSAGE for e in errors.values()):
                return Observation(kind="error", error=_DAILY_LIMIT_MESSAGE)
            msg = "; ".join(f"{q}: {errors.get(q, 'unknown error')}" for q in query_list)
            return Observation(kind="error", error=f"web search failed: {msg}")

        seen_urls: set = set()
        hits: List[Dict[str, Any]] = []
        blocks: List[str] = []

        for q, resp in ordered_results:
            lines = [
                f'WEB RESULTS for "{q}" (via {self.backend_name}; cite facts inline as [title](url)):'
            ]
            if resp.answer:
                lines.append(f"Summary: {trim_chars(resp.answer, _SUMMARY_MAX_CHARS)}")
            # A backend whose per-hit "snippet" is a SPAN OF ITS OWN SUMMARY bills the same words
            # twice. Gemini grounding does exactly that (its snippets come from the answer's
            # grounding supports): measured live, the summary and the first two snippets were the
            # same sentences, roughly 40% of a 406-token observation. A snippet already contained
            # in the summary is therefore dropped, and the title+URL stay so the hit is still
            # citable. Backends with independent snippets (Serper, Brave, Tavily, SearXNG) never
            # match this and are unaffected.
            # Compared against the summary AS RENDERED (trimmed), never the full answer, so a
            # snippet whose text falls past the trim point is kept rather than dropped as a
            # duplicate of something the planner never sees.
            shown_answer = trim_chars(resp.answer, _SUMMARY_MAX_CHARS) if resp.answer else ""

            shown = 0
            for hit in resp.hits:
                norm_url = canonicalize_url(hit.url) if hit.url else ""
                if norm_url and norm_url in seen_urls:
                    continue
                if norm_url:
                    seen_urls.add(norm_url)
                shown += 1
                date_part = f" | {hit.date}" if hit.date else ""
                lines.append(f"{shown}. {hit.title} | {hit.url}{date_part}")
                snippet = trim_chars(hit.snippet, _SNIPPET_MAX_CHARS)
                # An already-truncated snippet carries a trailing ellipsis that the summary does
                # not, so the containment check compares the part before it.
                collapsed_snippet = collapse_ws(hit.snippet).rstrip(".… ")
                if snippet and not (
                    shown_answer and collapsed_snippet and collapsed_snippet in shown_answer
                ):
                    lines.append(f"   {snippet}")
                hits.append(
                    {
                        "title": hit.title,
                        "url": hit.url,
                        "snippet": hit.snippet,
                        "query": q,
                        "source": self.backend_name,
                    }
                )
            lines.append(
                '(Snippets are partial. To read one page in full, request '
                '{"web_page": "<url>", "focus": "<what you need>"}.)'
            )
            blocks.append("\n".join(lines))

        for q in query_list:
            if q in errors:
                blocks.append(f'WEB RESULTS for "{q}": error -- {errors[q]}')

        first_q = query_list[0]
        return Observation(
            kind="query",
            rel_path=f"web_search:{first_q[:80]}",
            text="\n\n".join(blocks),
            hits=hits,
        )

    def _search_one(self, query: str, max_results: int, fresh: bool) -> SearchResponse:
        cache_key = f"{self.backend_name}|{max_results}|{normalize_query(query)}"

        if not fresh:
            cached = self._cache.get("web_search", cache_key)
            if cached is not None:
                return SearchResponse(
                    hits=[SearchHit(**h) for h in cached.get("hits", [])],
                    answer=cached.get("answer", ""),
                    queries_issued=list(cached.get("queries_issued", [])),
                )

        if self._limiter.exhausted():
            raise SearchBackendError(_DAILY_LIMIT_MESSAGE)
        self._limiter.record()

        resp = self._backend.search(query, max_results=max_results)

        self._cache.set(
            "web_search",
            cache_key,
            {
                "hits": [
                    {"title": h.title, "url": h.url, "snippet": h.snippet, "date": h.date}
                    for h in resp.hits
                ],
                "answer": resp.answer,
                "queries_issued": resp.queries_issued,
            },
            ttl_seconds=self._search_ttl,
        )
        return resp

    # ------------------------------------------------------------------
    # fetch
    # ------------------------------------------------------------------

    def fetch(self, url: str, *, focus: Optional[str] = None, fresh: bool = False,
              full: bool = False, part: Optional[int] = None) -> Observation:
        """Read one page. Default: the passages most relevant to ``focus`` (about
        ``page_token_budget`` tokens). ``full=True`` returns the document's text in order, one
        part of about ``full_page_token_budget`` tokens at a time; ``part=N`` (1-based, implies
        ``full``) reads part N. A long document says how many parts there are and how to ask for
        the next."""
        try:
            return self._fetch_inner(url, focus=focus, fresh=fresh, full=full, part=part)
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebResearchAdapter.fetch failed for %r", url, exc_info=True)
            return Observation(kind="error", rel_path=url, error=f"web fetch failed: {exc}")

    def _fetch_inner(self, url: str, *, focus: Optional[str], fresh: bool,
                     full: bool = False, part: Optional[int] = None) -> Observation:
        url = (url or "").strip()
        if not url:
            return Observation(kind="error", error="web fetch: no URL given")

        safety_error = check_url_is_safe(url)
        if safety_error:
            return Observation(kind="error", rel_path=url, error=safety_error)

        # A Google Docs / Sheets / Slides / Drive link is read through its public export URL (it
        # works when the file is shared with a link anyone can open). The rewritten URL is a
        # different address than the one the planner asked for, so it gets its own SSRF check and
        # its own cache key (the export URL carries the sheet tab's gid).
        target = rewrite_for_export(url)
        fetch_url = url
        if target is not None:
            if target.kind == KIND_DRIVE_FOLDER:
                return Observation(
                    kind="error", rel_path=url,
                    error=link_not_read_error(url, REASON_UNSUPPORTED_TYPE, FOLDER_MESSAGE))
            if target.kind == KIND_FORM:
                return Observation(
                    kind="error", rel_path=url,
                    error=link_not_read_error(url, REASON_UNSUPPORTED_TYPE, FORM_MESSAGE))
            fetch_url = target.export_url
            safety_error = check_url_is_safe(fetch_url)
            if safety_error:
                return Observation(kind="error", rel_path=url, error=safety_error)

        canonical = canonicalize_url(fetch_url)
        cached_page = None if fresh else self._cache.get("web_page", canonical)

        if cached_page is not None:
            title = cached_page.get("title", "")
            full_text = cached_page.get("text", "")
        else:
            title, full_text, hard_error, cause, reason = self._fetch_and_extract(fetch_url, target, shown_url=url)
            if hard_error:
                return Observation(kind="error", rel_path=url, error=hard_error)

            if len(full_text.strip()) < _MIN_EXTRACTED_CHARS and self._url_fetch_fallback is not None:
                if self._limiter.exhausted():
                    # The fallback is a PAID model call (Gemini url_context); the direct HTML
                    # fetch above stays free and is never counted. Skip it outright rather than
                    # issuing a call the deployment has already spent its daily budget on.
                    cause = cause or _DAILY_LIMIT_FALLBACK_MESSAGE
                else:
                    self._limiter.record()
                    fallback_text = ""
                    try:
                        fallback_text = self._url_fetch_fallback(url) or ""
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("url_fetch_fallback failed for %r: %s", url, exc)
                        cause = cause or str(exc)
                    if len(fallback_text.strip()) > len(full_text.strip()):
                        full_text = fallback_text.strip()
                        if not title:
                            title = url

            if not full_text.strip():
                # Name WHY, not just that it failed: "no extractable content" alone sent the
                # planner off to retry other reads when the real cause was an HTTP 403, a
                # connection timeout, or a refusal from the fallback fetcher. A status line or an
                # error the planner acts on has to be honest about the cause, and the typed
                # reason is what the user is told.
                detail = f" ({cause})" if cause else ""
                return Observation(
                    kind="error", rel_path=url,
                    error=link_not_read_error(
                        url, reason or REASON_JS_SHELL,
                        f"{REASON_MESSAGES.get(reason or REASON_JS_SHELL, '')} "
                        f"[detail: no extractable content from {url}{detail}]"),
                )

            self._cache.set(
                "web_page", canonical, {"title": title, "text": full_text}, ttl_seconds=self._page_ttl
            )

        display_title = title or url
        try:
            part_number = int(part) if part is not None and not isinstance(part, bool) else None
        except (TypeError, ValueError):
            part_number = None
        if part_number is not None and part_number < 1:
            part_number = 1

        if full or part_number is not None:
            return self._full_observation(url, display_title, full_text, part_number or 1)

        passages = split_passages(full_text)
        chosen = select_passages(passages, (focus or "").strip(), self._page_token_budget)
        body = "\n...\n".join(chosen) if chosen else full_text[: self._page_token_budget * 4]

        header = [f"PAGE: {display_title} | {url}"]
        if focus:
            header.append(f"(relevant passages for: {focus}; cite as [{display_title}]({url}))")
        total_tokens = estimate_tokens(full_text)
        if total_tokens > self._page_token_budget * 1.25:
            header.append(
                f"(excerpts only: this document is about {total_tokens} tokens. If these excerpts do "
                f"not answer the question, do not re-read with another focus: read all of it in "
                f"order with {{\"web_page\": \"{url}\", \"full\": true}})")

        text = "\n".join(header) + "\n\n" + body
        return Observation(kind="read", rel_path=url, locator=f"web extract: {url}", text=text)

    def _full_observation(self, url: str, display_title: str, full_text: str,
                          part_number: int) -> Observation:
        """One part of the whole document, in order, with where it sits and how to continue."""
        parts = paginate_text(full_text, self._full_page_token_budget * _CHARS_PER_TOKEN)
        count = len(parts)
        header = [f"PAGE: {display_title} | {url}"]
        if part_number > count:
            header.append(
                f"(this document has {count} part(s); part {part_number} does not exist. "
                f"cite as [{display_title}]({url}))")
            return Observation(kind="read", rel_path=url,
                               locator=f"web extract: {url} (full, part {part_number})",
                               text="\n".join(header))
        header.append(
            f"(full text, part {part_number} of {count}, in document order; "
            f"cite as [{display_title}]({url}))")
        if part_number < count:
            header.append(
                f"(the document continues: read the next part with "
                f"{{\"web_page\": \"{url}\", \"full\": true, \"part\": {part_number + 1}}}. "
                "Answer from what you have if it already covers the question.)")
        else:
            header.append("(end of the document)")
        text = "\n".join(header) + "\n\n" + parts[part_number - 1]
        return Observation(kind="read", rel_path=url,
                           locator=f"web extract: {url} (full, part {part_number} of {count})",
                           text=text)

    def _fetch_and_extract(self, url: str, target: Optional[LinkTarget] = None,
                           shown_url: Optional[str] = None) -> Any:
        """Returns ``(title, text, hard_error, cause, reason)``.

        ``hard_error`` is a ready ``LINK NOT READ [...]`` string for a failure no fallback can
        fix (private, missing, too large, wrong kind of file). Any other fetch failure returns
        empty text plus a short ``cause`` and its typed ``reason``, so the caller can still try
        ``url_fetch_fallback`` and can still say WHY if nothing worked.
        """
        shown = shown_url or url
        try:
            page = self._page_fetcher(url)
        except PageFetchError as exc:
            reason = getattr(exc, "reason", REASON_UNREACHABLE)
            if reason in HARD_FAILURE_REASONS:
                message = exc.user_message or REASON_MESSAGES.get(reason, "")
                return "", "", link_not_read_error(shown, reason, f"{message} [detail: {exc}]"), None, reason
            logger.debug("page fetch failed for %r: %s", url, exc)
            return "", "", None, str(exc), reason
        except Exception as exc:  # noqa: BLE001
            logger.debug("page fetch failed for %r: %s", url, exc)
            return "", "", None, str(exc), REASON_UNREACHABLE

        content_type = (getattr(page, "content_type", "") or "").lower()
        data = getattr(page, "data", b"") or b""
        html = getattr(page, "html", "") or ""
        if "pdf" in content_type or (data and looks_like_pdf(data)):
            try:
                text = extract_pdf_text(data)
            except LinkReadError as exc:
                return "", "", link_not_read_error(shown, exc.reason, f"{exc.user_message} [detail: {exc}]"), None, exc.reason
            return "", text, None, None, None

        if target is not None and target.kind == "drive_file" and drive_download_is_too_big(html):
            return "", "", link_not_read_error(shown, REASON_TOO_LARGE), None, REASON_TOO_LARGE

        if target is not None and target.text_format in ("text", "csv"):
            # A document / presentation / sheet export is already plain text or CSV: no HTML
            # extraction (which would drop short lines), just strip the byte-order mark.
            text = html.lstrip("\ufeff").strip()
            if (html.lstrip().lower().startswith(("<!doctype html", "<html"))
                    and not text.lower().startswith("<?xml")):
                # Google answered with an HTML page (sign-in or error) instead of the export.
                return "", "", link_not_read_error(shown, REASON_NOT_SHARED), None, REASON_NOT_SHARED
            title = ""
            return title, _with_truncation_note(text, page), None, None, None

        if content_type.startswith("text/plain") or content_type.startswith("text/csv"):
            return "", _with_truncation_note(html.lstrip("\ufeff").strip(), page), None, None, None

        try:
            title, text = extract_main_text(html)
        except Exception:  # noqa: BLE001
            logger.debug("extract_main_text failed for %r", url, exc_info=True)
            title, text = "", ""
        reason = None
        if len(text.strip()) < _MIN_EXTRACTED_CHARS:
            reason = classify_thin_page(html, text)
            if reason == REASON_TOO_LARGE or reason == REASON_NOT_SHARED:
                # A Drive "too large to scan" page or a login wall: a fallback fetch sees the
                # same wall, so say so now instead of spending a paid call.
                return "", "", link_not_read_error(shown, reason), None, reason
            if reason == REASON_BLOCKED:
                # A bot check is not content. A paid fallback fetch may still get past it.
                return "", "", None, "the page is a bot check, not content", reason
        return title, _with_truncation_note(text, page), None, None, reason


_CHARS_PER_TOKEN = 4


def paginate_text(text: str, max_chars: int) -> List[str]:
    """Split ``text`` into consecutive parts of at most about ``max_chars`` characters, in order,
    cutting at a paragraph break when there is one in the last fifth of a part, else at a line
    break, else at a space. Joining the parts reproduces the text (whitespace at the cuts aside).
    Always returns at least one part."""
    text = (text or "").strip()
    max_chars = max(200, int(max_chars))
    if len(text) <= max_chars:
        return [text]
    parts: List[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + max_chars)
        if end < n:
            floor = start + int(max_chars * 0.8)
            for sep in ("\n\n", "\n", " "):
                cut = text.rfind(sep, floor, end)
                if cut > start:
                    end = cut + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            parts.append(chunk)
        start = end
    return parts or [text]


def _with_truncation_note(text: str, page: Any) -> str:
    """Say so when the body was cut at the size cap, so a partial read is never silent."""
    if getattr(page, "truncated", False) and text:
        return text + f"\n\n[Note: this file is larger than {MAX_PAGE_BYTES // (1024 * 1024)} MB; only the start was read.]"
    return text


# ---------------------------------------------------------------------------
# build_web_research_from_env
# ---------------------------------------------------------------------------


def _build_gemini_url_fallback(env: Mapping[str, str]) -> Optional[Callable[[str], str]]:
    """A ``url_fetch_fallback`` closure using Gemini's ``url_context`` tool, or ``None`` without
    a Gemini key. Google retrieves the page server-side, so this works where a direct GET from
    this machine is blocked, rate-limited, or returns a JS shell instead of content."""
    key = ""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_AI_API_KEY"):
        v = env.get(name)
        if v:
            key = v
            break
    if not key:
        return None
    model = env.get("QAR_WEB_SEARCH_MODEL") or "gemini-2.5-flash-lite"

    def fallback(url: str) -> str:
        from .gemini_provider import GeminiProvider

        gp = GeminiProvider(api_key=key)
        result = gp.fetch_url(url, model=model)
        return result.get("text", "")

    return fallback


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def build_web_research_from_env(
    env: Optional[Mapping[str, str]] = None, *, provider: Any = None
) -> Optional[WebResearchAdapter]:
    """Build a ``WebResearchAdapter`` from environment config, or ``None`` if web search is
    unavailable (disabled, or no backend has a key/config present).

    Tuning env vars (backend selection vars are documented on ``select_search_backend``):
      WEB_SEARCH_MAX_RESULTS      -- max results per search (default 5).
      QAR_WEB_PAGE_TOKEN_BUDGET   -- max tokens kept from one FOCUSED page read: the passages
                                     most relevant to the focus (default 3000).
      QAR_WEB_PAGE_FULL_TOKEN_BUDGET -- max tokens in one part of a WHOLE-document read
                                     ({"web_page": url, "full": true}); a longer document is paged
                                     with "part": N (default 12000).
      QAR_WEB_SEARCH_TTL_SECONDS  -- search-result cache TTL in seconds (default 86400 = 1 day).
      QAR_WEB_PAGE_TTL_SECONDS    -- fetched-page cache TTL in seconds (default 604800 = 7 days).
      QAR_WEB_CACHE_DIR           -- on-disk cache directory (unset = memory-only cache).
      QAR_WEB_SEARCH_DAILY_LIMIT  -- max REAL, PAID calls per UTC day: backend searches plus
                                     url_fetch_fallback (Gemini url_context) fetches that are
                                     actually issued (unset/0 = no limit). Cache hits and direct
                                     HTML fetches don't count. Persisted under QAR_WEB_CACHE_DIR
                                     when that's set, so a restart doesn't reset the count. Once
                                     reached, ``search()`` returns an error instead of calling the
                                     backend; ``fetch()`` skips the fallback and keeps the thin
                                     direct result, or names the limit as the cause when there is
                                     nothing else to show.
    """
    env = env if env is not None else os.environ
    backend = select_search_backend(env, provider=provider)
    if backend is None:
        return None

    max_results = _int_env(env, "WEB_SEARCH_MAX_RESULTS", _DEFAULT_MAX_RESULTS)
    page_token_budget = _int_env(env, "QAR_WEB_PAGE_TOKEN_BUDGET", _DEFAULT_PAGE_TOKEN_BUDGET)
    full_page_token_budget = _int_env(
        env, "QAR_WEB_PAGE_FULL_TOKEN_BUDGET", _DEFAULT_FULL_PAGE_TOKEN_BUDGET)
    search_ttl = _int_env(env, "QAR_WEB_SEARCH_TTL_SECONDS", _DEFAULT_SEARCH_TTL)
    page_ttl = _int_env(env, "QAR_WEB_PAGE_TTL_SECONDS", _DEFAULT_PAGE_TTL)
    daily_limit = _int_env(env, "QAR_WEB_SEARCH_DAILY_LIMIT", 0) or None
    cache_dir = env.get("QAR_WEB_CACHE_DIR") or None
    cache = WebCache(directory=cache_dir)
    url_fetch_fallback = _build_gemini_url_fallback(env)

    logger.info("web research: backend chain = %s", getattr(backend, "name", "unknown"))

    return WebResearchAdapter(
        backend,
        max_results=max_results,
        page_token_budget=page_token_budget,
        full_page_token_budget=full_page_token_budget,
        cache=cache,
        search_ttl_seconds=search_ttl,
        page_ttl_seconds=page_ttl,
        url_fetch_fallback=url_fetch_fallback,
        daily_limit=daily_limit,
    )


# The module is split by role; these names stay importable from here.
from .web_research_fetch import (  # noqa: F401  (kept importable from here)
    FETCH_TIMEOUT,
    FetchedPage,
    HARD_FAILURE_REASONS,
    MAX_PAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_REDIRECTS,
    PageFetchError,
    TEXT_CONTENT_PREFIXES,
    UNREADABLE_PREFIXES,
    USER_AGENT,
    UnsupportedContentTypeError,
    canonicalize_url,
    charset_from_content_type,
    collapse_ws,
    decode_page,
    default_page_fetcher,
    sniff_meta_charset,
    status_failure,
    trim_chars,
)
from .web_research_limiter import (  # noqa: F401  (kept importable from here)
    DailyLimiter,
    LOCK_POLL_INTERVAL_SECONDS,
    LOCK_TIMEOUT_SECONDS,
)
