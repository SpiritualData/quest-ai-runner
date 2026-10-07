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
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Union
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.adapters import Observation
from ..core.file_modes import match_umask
from .web_cache import WebCache, normalize_query
from .web_page_extract import (
    check_url_is_safe,
    estimate_tokens,
    extract_main_text,
    select_passages,
    split_passages,
)
from .web_search_backends import (
    SearchBackend,
    SearchBackendError,
    SearchHit,
    SearchResponse,
    select_search_backend,
)

logger = logging.getLogger("quest-ai-runner.web-research")

_DEFAULT_MAX_RESULTS = 5
_DEFAULT_PAGE_TOKEN_BUDGET = 800
_DEFAULT_SEARCH_TTL = 86400
_DEFAULT_PAGE_TTL = 604800
_FETCH_TIMEOUT = 8.0
_MAX_PAGE_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 5
_ALLOWED_CONTENT_TYPES = ("text/html", "text/plain", "application/xhtml")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
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


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def canonicalize_url(url: str) -> str:
    """Drop the fragment and any ``utm_*`` tracking params, for stable cache/dedupe keys."""
    try:
        parts = urlsplit(url)
        kept = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_")
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))
    except Exception:  # noqa: BLE001
        return url


def _collapse_ws(s: str) -> str:
    return " ".join((s or "").split())


def _trim(s: str, n: int) -> str:
    s = _collapse_ws(s)
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0] + "..."


# ---------------------------------------------------------------------------
# Page fetching (injectable for tests)
# ---------------------------------------------------------------------------


@dataclass
class FetchedPage:
    html: str
    content_type: str
    status_code: int


class PageFetchError(Exception):
    """The page could not be retrieved at all (network/timeout/HTTP error). Retryable via a
    ``url_fetch_fallback``."""


class UnsupportedContentTypeError(PageFetchError):
    """The content-type is one we deliberately don't extract (e.g. a PDF). NOT retried via
    ``url_fetch_fallback`` -- a fallback fetch would hit the same PDF."""


def _charset_from_content_type(header: str) -> str:
    """The ``charset=`` parameter of a content-type header, lowercased, or ``""``."""
    for part in (header or "").split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("\"'").lower()
    return ""


def _sniff_meta_charset(raw: bytes) -> str:
    """The charset declared in the document's own ``<meta>``, from the first 4 KB, or ``""``.

    Sniffed as ASCII-ish bytes because that is all a charset declaration can legally be, and we
    do not yet know the encoding of the rest of the document.
    """
    head = raw[:4096].decode("ascii", errors="ignore")
    match = re.search(r"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", head, re.I)
    return match.group(1).strip().lower() if match else ""


def _decode_page(raw: bytes, content_type_header: str) -> str:
    """Decode page bytes with the charset the page actually declares, not an assumed UTF-8.

    Before this, every page was decoded as UTF-8 with ``errors="replace"``, so a windows-1252 /
    latin-1 / Shift-JIS page arrived as U+FFFD soup: every non-ASCII character in it (names,
    prices, quotes, every accented word) became a replacement character, which then also poisoned
    the BM25 focus scoring. The declared charset wins, the document's own ``<meta>`` is the
    fallback, and UTF-8 with replacement is the last resort so this still never raises.
    """
    candidates = [
        _charset_from_content_type(content_type_header),
        _sniff_meta_charset(raw),
    ]
    for charset in candidates:
        if not charset or charset in ("utf-8", "utf8"):
            continue
        try:
            return raw.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


def _default_page_fetcher(url: str) -> FetchedPage:
    """GET ``url`` with bounded size/time/content-type. Raises ``PageFetchError`` on failure.

    Redirects are followed BY HAND, re-running ``check_url_is_safe`` on every hop, because
    ``follow_redirects=True`` would have walked straight past the SSRF guard: the guard only ever
    saw the URL the planner asked for, so any public URL that 302s to ``http://127.0.0.1:9000/``
    or ``http://169.254.169.254/latest/meta-data/`` was fetched anyway, and the guard's promise
    (checked "before we ever open a socket") held only for the first hop.
    """
    import httpx

    headers = {"User-Agent": _USER_AGENT}
    current = url
    try:
        for _hop in range(_MAX_REDIRECTS + 1):
            with httpx.stream(
                "GET", current, headers=headers, timeout=_FETCH_TIMEOUT, follow_redirects=False
            ) as resp:
                status_code = resp.status_code
                content_type_header = resp.headers.get("content-type") or ""
                if status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location") or ""
                    if not location:
                        raise PageFetchError(
                            f"web fetch: HTTP {status_code} with no Location for {current}")
                    current = str(httpx.URL(current).join(location))
                    safety_error = check_url_is_safe(current)
                    if safety_error:
                        raise PageFetchError(f"web fetch: redirect to {current} {safety_error}")
                    continue
                content_type = content_type_header.split(";")[0].strip().lower()
                if status_code >= 400:
                    raise PageFetchError(f"web fetch: HTTP {status_code} for {current}")
                if content_type and not any(
                    content_type.startswith(ct) for ct in _ALLOWED_CONTENT_TYPES
                ):
                    if "pdf" in content_type:
                        raise UnsupportedContentTypeError(
                            f"web fetch: {url} is a PDF (content-type {content_type}); "
                            "request a deep run to read PDFs"
                        )
                    raise UnsupportedContentTypeError(
                        f"web fetch: unsupported content-type {content_type!r} for {url}"
                    )
                chunks: List[bytes] = []
                total = 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_PAGE_BYTES:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks)
            return FetchedPage(
                html=_decode_page(raw, content_type_header),
                content_type=content_type,
                status_code=status_code,
            )
        raise PageFetchError(f"web fetch: too many redirects for {url}")
    except (PageFetchError, UnsupportedContentTypeError):
        raise
    except httpx.HTTPError as exc:
        raise PageFetchError(f"web fetch: request failed for {current}: {exc}") from exc


# ---------------------------------------------------------------------------
# Daily cost guard
# ---------------------------------------------------------------------------


class _DailyLimiter:
    """UTC-day counter of REAL, PAID calls: backend searches and ``url_fetch_fallback`` (Gemini
    url_context) fetches that are actually issued. Cache hits and direct HTML fetches never
    count; they are free.

    Thread-safe; optionally persisted to ``state_dir`` (one small JSON file) so a process
    restart doesn't reset the count mid-day. A limit of ``None``/``0`` disables the guard.
    """

    def __init__(self, limit: Optional[int], *, state_dir: Optional[Path] = None) -> None:
        self._limit = int(limit) if limit else 0
        self._state_path = (state_dir / "web_search_daily_count.json") if state_dir else None
        self._lock = threading.Lock()
        self._day = self._today()
        self._count = 0
        if self._state_path is not None:
            self._load()

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))  # type: ignore[union-attr]
            if raw.get("day") == self._day:
                self._count = int(raw.get("count", 0))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_locked(self) -> None:
        if self._state_path is None:
            return
        tmp_path: Optional[str] = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(self._state_path.parent), prefix=".webcount-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"day": self._day, "count": self._count}, fh)
                fh.flush()
                match_umask(fh.fileno())
            os.replace(tmp_path, self._state_path)
            tmp_path = None
        except OSError:
            logger.debug("web search daily limiter: could not persist count", exc_info=True)
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _roll_day_locked(self) -> None:
        today = self._today()
        if today != self._day:
            self._day = today
            self._count = 0

    def exhausted(self) -> bool:
        if self._limit <= 0:
            return False
        with self._lock:
            self._roll_day_locked()
            return self._count >= self._limit

    def record(self) -> None:
        """Record one real, paid call (a backend search, or a url_fetch_fallback fetch that was
        actually issued). No-op when the guard is disabled."""
        if self._limit <= 0:
            return
        with self._lock:
            self._roll_day_locked()
            self._count += 1
            self._save_locked()


# ---------------------------------------------------------------------------
# WebResearchAdapter
# ---------------------------------------------------------------------------


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
        page_token_budget: int = 800,
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
        self._cache = cache if cache is not None else WebCache()
        self._search_ttl = search_ttl_seconds
        self._page_ttl = page_ttl_seconds
        self._page_fetcher = page_fetcher or _default_page_fetcher
        self._url_fetch_fallback = url_fetch_fallback
        self._max_parallel = max(1, max_parallel)
        self._limiter = _DailyLimiter(daily_limit, state_dir=self._cache.directory)

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
                lines.append(f"Summary: {_trim(resp.answer, _SUMMARY_MAX_CHARS)}")
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
            shown_answer = _trim(resp.answer, _SUMMARY_MAX_CHARS) if resp.answer else ""

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
                snippet = _trim(hit.snippet, _SNIPPET_MAX_CHARS)
                # An already-truncated snippet carries a trailing ellipsis that the summary does
                # not, so the containment check compares the part before it.
                collapsed_snippet = _collapse_ws(hit.snippet).rstrip(".… ")
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

    def fetch(self, url: str, *, focus: Optional[str] = None, fresh: bool = False) -> Observation:
        try:
            return self._fetch_inner(url, focus=focus, fresh=fresh)
        except Exception as exc:  # noqa: BLE001
            logger.debug("WebResearchAdapter.fetch failed for %r", url, exc_info=True)
            return Observation(kind="error", rel_path=url, error=f"web fetch failed: {exc}")

    def _fetch_inner(self, url: str, *, focus: Optional[str], fresh: bool) -> Observation:
        url = (url or "").strip()
        if not url:
            return Observation(kind="error", error="web fetch: no URL given")

        safety_error = check_url_is_safe(url)
        if safety_error:
            return Observation(kind="error", rel_path=url, error=safety_error)

        canonical = canonicalize_url(url)
        cached_page = None if fresh else self._cache.get("web_page", canonical)

        if cached_page is not None:
            title = cached_page.get("title", "")
            full_text = cached_page.get("text", "")
        else:
            title, full_text, hard_error, cause = self._fetch_and_extract(url)
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
                # error the planner acts on has to be honest about the cause.
                detail = f" ({cause})" if cause else ""
                return Observation(
                    kind="error", rel_path=url,
                    error=f"web fetch: no extractable content from {url}{detail}",
                )

            self._cache.set(
                "web_page", canonical, {"title": title, "text": full_text}, ttl_seconds=self._page_ttl
            )

        passages = split_passages(full_text)
        chosen = select_passages(passages, (focus or "").strip(), self._page_token_budget)
        body = "\n...\n".join(chosen) if chosen else full_text[: self._page_token_budget * 4]

        display_title = title or url
        header = [f"PAGE: {display_title} | {url}"]
        if focus:
            header.append(f"(relevant passages for: {focus}; cite as [{display_title}]({url}))")

        text = "\n".join(header) + "\n\n" + body
        return Observation(kind="read", rel_path=url, locator=f"web extract: {url}", text=text)

    def _fetch_and_extract(self, url: str) -> Any:
        """Returns ``(title, text, hard_error, cause)``. ``hard_error`` is set only for a
        content-type we deliberately refuse (e.g. PDF); any other fetch failure returns empty text
        plus a short ``cause`` string, so the caller can still try ``url_fetch_fallback`` and can
        still say WHY if nothing worked."""
        try:
            page = self._page_fetcher(url)
        except UnsupportedContentTypeError as exc:
            return "", "", str(exc), None
        except Exception as exc:  # noqa: BLE001
            logger.debug("page fetch failed for %r: %s", url, exc)
            return "", "", None, str(exc)

        try:
            title, text = extract_main_text(page.html)
        except Exception:  # noqa: BLE001
            logger.debug("extract_main_text failed for %r", url, exc_info=True)
            title, text = "", ""
        return title, text, None, None


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
      QAR_WEB_PAGE_TOKEN_BUDGET   -- max tokens kept from one fetched page (default 800).
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
        cache=cache,
        search_ttl_seconds=search_ttl,
        page_ttl_seconds=page_ttl,
        url_fetch_fallback=url_fetch_fallback,
        daily_limit=daily_limit,
    )
