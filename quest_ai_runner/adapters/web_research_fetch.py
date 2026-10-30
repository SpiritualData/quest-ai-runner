"""web_research, page fetching: the default SSRF-guarded page fetcher (redirects re-checked, byte caps,

Moved to the hub package (``hubofhubs.websearch.fetch``); this module re-exports its names so existing imports keep working (CHANGELOG).
"""
from hubofhubs.websearch.fetch import (  # noqa: F401
    FETCH_TIMEOUT,
    MAX_PAGE_BYTES,
    MAX_PDF_BYTES,
    MAX_REDIRECTS,
    USER_AGENT,
    canonicalize_url,
    collapse_ws,
    trim_chars,
    FetchedPage,
    PageFetchError,
    UnsupportedContentTypeError,
    HARD_FAILURE_REASONS,
    TEXT_CONTENT_PREFIXES,
    UNREADABLE_PREFIXES,
    charset_from_content_type,
    sniff_meta_charset,
    decode_page,
    status_failure,
    default_page_fetcher,
)
