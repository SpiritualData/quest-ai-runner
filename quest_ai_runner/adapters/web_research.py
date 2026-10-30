"""web_research -- QAR's one-call constructor over the hub's WebResearchAdapter.

The adapter and its helpers moved to the hub package (``hubofhubs.websearch.research``, ``.fetch``, ``.limiter``); the hub
reads no environment, so what stays here is QAR's: the ``search-the-web`` / ``know-the-world`` implementation rows, the
``QAR_WEB_*`` tuning names, and the Gemini ``url_context`` fallback for pages that resist direct extraction. Names the old
module defined or re-exported stay importable from here (CHANGELOG).
"""
from __future__ import annotations

import logging
import os
from typing import Any, Callable, Mapping, Optional

from esoi import implementation
from hubofhubs.websearch import research as hub_research
from hubofhubs.websearch.fetch import (  # noqa: F401
    FETCH_TIMEOUT,
    HARD_FAILURE_REASONS,
    MAX_PAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_REDIRECTS,
    TEXT_CONTENT_PREFIXES,
    UNREADABLE_PREFIXES,
    USER_AGENT,
    FetchedPage,
    PageFetchError,
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
from hubofhubs.websearch.limiter import LOCK_POLL_INTERVAL_SECONDS, LOCK_TIMEOUT_SECONDS, DailyLimiter  # noqa: F401
from hubofhubs.websearch.page_extract import check_url_is_safe, estimate_tokens  # noqa: F401
from hubofhubs.websearch.research import paginate_text  # noqa: F401

from .web_search_backends import gemini_api_key_from_env, select_search_backend

logger = logging.getLogger("quest-ai-runner.web-research")


@implementation("search-the-web", "web_research")
@implementation("know-the-world", "web_with_support_check")
class WebResearchAdapter(hub_research.WebResearchAdapter):
    """The hub's snippet-first search and focused page fetch, declared under QAR's solutions."""


def build_gemini_url_fallback(env: Mapping[str, str]) -> Optional[Callable[[str], str]]:
    """A ``url_fetch_fallback`` using Gemini's ``url_context`` tool, or ``None`` without a Gemini key. Google retrieves the
    page server-side, so this works where a direct GET from this machine is blocked, rate-limited or returns a JS shell."""
    key = gemini_api_key_from_env(env)
    if not key:
        return None
    model = env.get("QAR_WEB_SEARCH_MODEL") or "gemini-2.5-flash-lite"

    def fallback(url: str) -> str:
        from .gemini_provider import GeminiProvider

        return GeminiProvider(api_key=key).fetch_url(url, model=model).get("text", "")

    return fallback


def int_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    """The integer setting ``name`` in ``env``, or ``default`` when unset or not a number."""
    try:
        return int(env.get(name) or default)
    except (TypeError, ValueError):
        return default


def build_web_research_from_env(
    env: Optional[Mapping[str, str]] = None, *, provider: Any = None
) -> Optional[WebResearchAdapter]:
    """Build a ``WebResearchAdapter`` from environment config, or ``None`` if web search is unavailable (disabled, or no
    backend has a key/config present). Backend selection settings are documented on ``select_search_backend``.

    Tuning settings:
      WEB_SEARCH_MAX_RESULTS         max results per search (default 5).
      QAR_WEB_PAGE_TOKEN_BUDGET      tokens kept from one FOCUSED page read (default 3000).
      QAR_WEB_PAGE_FULL_TOKEN_BUDGET tokens in one part of a WHOLE-document read, paged with "part": N (default 12000).
      QAR_WEB_SEARCH_TTL_SECONDS     search-result cache TTL (default 86400 = 1 day).
      QAR_WEB_PAGE_TTL_SECONDS       fetched-page cache TTL (default 604800 = 7 days).
      QAR_WEB_CACHE_DIR              on-disk cache directory (unset = memory-only cache).
      QAR_WEB_SEARCH_DAILY_LIMIT     max REAL, PAID calls per UTC day: backend searches plus url_fetch_fallback fetches
                                     actually issued (unset/0 = no limit). Cache hits and direct HTML fetches do not count.
                                     Persisted under QAR_WEB_CACHE_DIR when set, so a restart does not reset the count.
                                     Once reached, ``search()`` returns an error instead of calling the backend and
                                     ``fetch()`` skips the fallback, keeping the thin direct result or naming the limit.
    """
    env = env if env is not None else os.environ
    backend = select_search_backend(env, provider=provider)
    if backend is None:
        return None
    logger.info("web research: backend chain = %s", getattr(backend, "name", "unknown"))
    return WebResearchAdapter(
        backend,
        max_results=int_from_env(env, "WEB_SEARCH_MAX_RESULTS", hub_research.DEFAULT_MAX_RESULTS),
        page_token_budget=int_from_env(env, "QAR_WEB_PAGE_TOKEN_BUDGET", hub_research.DEFAULT_PAGE_TOKEN_BUDGET),
        full_page_token_budget=int_from_env(env, "QAR_WEB_PAGE_FULL_TOKEN_BUDGET", hub_research.DEFAULT_FULL_PAGE_TOKEN_BUDGET),
        cache=hub_research.WebCache(directory=env.get("QAR_WEB_CACHE_DIR") or None),
        search_ttl_seconds=int_from_env(env, "QAR_WEB_SEARCH_TTL_SECONDS", hub_research.DEFAULT_SEARCH_TTL),
        page_ttl_seconds=int_from_env(env, "QAR_WEB_PAGE_TTL_SECONDS", hub_research.DEFAULT_PAGE_TTL),
        url_fetch_fallback=build_gemini_url_fallback(env),
        daily_limit=int_from_env(env, "QAR_WEB_SEARCH_DAILY_LIMIT", 0) or None,
    )
