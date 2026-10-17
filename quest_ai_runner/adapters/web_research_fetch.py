"""web_research, page fetching: the default SSRF-guarded page fetcher (redirects re-checked, byte caps,
charset decoding), the ``FetchedPage`` value and the ``PageFetchError`` family, plus the URL and text
helpers the adapter shares.

``adapters.web_research`` re-exports every name here.
"""
from __future__ import annotations

import re

from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .web_link_access import (
    is_login_host,
    looks_like_pdf,
    REASON_BLOCKED,
    REASON_NOT_FOUND,
    REASON_NOT_SHARED,
    REASON_TOO_LARGE,
    REASON_UNREACHABLE,
    REASON_UNSUPPORTED_TYPE,
)
from .web_page_extract import check_url_is_safe

FETCH_TIMEOUT = 8.0
MAX_PAGE_BYTES = 2 * 1024 * 1024
#: A PDF is binary and its text is a fraction of its size, so it gets a larger byte cap than a
#: web page (the extracted text is capped separately, see ``web_link_access.PDF_MAX_TEXT_CHARS``).
MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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


def collapse_ws(s: str) -> str:
    return " ".join((s or "").split())


def trim_chars(s: str, n: int) -> str:
    s = collapse_ws(s)
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
    #: Raw bytes, kept only for binary bodies (a PDF) that ``html`` cannot represent.
    data: bytes = b""
    #: True when the body was cut at the size cap (text bodies only; a cut PDF is ``too_large``).
    truncated: bool = False
    #: The URL the body came from after redirects.
    final_url: str = ""


class PageFetchError(Exception):
    """The page could not be retrieved at all (network/timeout/HTTP error). Retryable via a
    ``url_fetch_fallback`` unless ``reason`` is one a fallback cannot change (see
    ``HARD_FAILURE_REASONS``).

    ``reason`` is one of ``web_link_access.ALL_REASONS`` (``unreachable`` when not set) and
    ``user_message`` optionally replaces that reason's standard sentence.
    """

    def __init__(self, message: str, reason: str = REASON_UNREACHABLE,
                 user_message: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.user_message = user_message


class UnsupportedContentTypeError(PageFetchError):
    """The content-type is one we deliberately don't extract (e.g. a video). NOT retried via
    ``url_fetch_fallback`` -- a fallback fetch would hit the same file."""

    def __init__(self, message: str, reason: str = REASON_UNSUPPORTED_TYPE,
                 user_message: Optional[str] = None) -> None:
        super().__init__(message, reason, user_message)


#: Failures a paid ``url_fetch_fallback`` cannot fix: the file is private, missing, too big or the
#: wrong kind. ``blocked``, ``unreachable`` and a thin page still get the fallback.
HARD_FAILURE_REASONS = frozenset({
    REASON_NOT_SHARED, REASON_NOT_FOUND, REASON_TOO_LARGE, REASON_UNSUPPORTED_TYPE,
})

#: Types read as text. ``text/*`` covers html, plain, csv and markdown.
TEXT_CONTENT_PREFIXES = ("text/", "application/xhtml", "application/json", "application/xml")
UNREADABLE_PREFIXES = ("video/", "audio/", "image/", "font/")


def charset_from_content_type(header: str) -> str:
    """The ``charset=`` parameter of a content-type header, lowercased, or ``""``."""
    for part in (header or "").split(";")[1:]:
        name, _, value = part.partition("=")
        if name.strip().lower() == "charset":
            return value.strip().strip("\"'").lower()
    return ""


def sniff_meta_charset(raw: bytes) -> str:
    """The charset declared in the document's own ``<meta>``, from the first 4 KB, or ``""``.

    Sniffed as ASCII-ish bytes because that is all a charset declaration can legally be, and we
    do not yet know the encoding of the rest of the document.
    """
    head = raw[:4096].decode("ascii", errors="ignore")
    match = re.search(r"<meta[^>]+charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:-]+)", head, re.I)
    return match.group(1).strip().lower() if match else ""


def decode_page(raw: bytes, content_type_header: str) -> str:
    """Decode page bytes with the charset the page actually declares, not an assumed UTF-8.

    Before this, every page was decoded as UTF-8 with ``errors="replace"``, so a windows-1252 /
    latin-1 / Shift-JIS page arrived as U+FFFD soup: every non-ASCII character in it (names,
    prices, quotes, every accented word) became a replacement character, which then also poisoned
    the BM25 focus scoring. The declared charset wins, the document's own ``<meta>`` is the
    fallback, and UTF-8 with replacement is the last resort so this still never raises.
    """
    candidates = [
        charset_from_content_type(content_type_header),
        sniff_meta_charset(raw),
    ]
    for charset in candidates:
        if not charset or charset in ("utf-8", "utf8"):
            continue
        try:
            return raw.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


def status_failure(status_code: int, current: str) -> PageFetchError:
    """The typed failure for an HTTP error status. A Google host answering 401/403 means the file
    is not shared with everyone; the same status from another site is a bot wall or a login."""
    host = (urlsplit(current).hostname or "").lower()
    google = host.endswith("google.com") or host.endswith("googleusercontent.com")
    msg = f"web fetch: HTTP {status_code} for {current}"
    if status_code == 401 or (status_code == 403 and google):
        return PageFetchError(msg, REASON_NOT_SHARED)
    if status_code in (404, 410):
        return PageFetchError(msg, REASON_NOT_FOUND)
    if status_code in (403, 429, 451):
        return PageFetchError(msg, REASON_BLOCKED)
    return PageFetchError(msg, REASON_UNREACHABLE)


def default_page_fetcher(url: str) -> FetchedPage:
    """GET ``url`` with bounded size/time/content-type. Raises ``PageFetchError`` on failure, with
    a typed ``reason`` (see ``web_link_access``).

    Redirects are followed BY HAND, re-running ``check_url_is_safe`` on every hop, because
    ``follow_redirects=True`` would have walked straight past the SSRF guard: the guard only ever
    saw the URL the planner asked for, so any public URL that 302s to ``http://127.0.0.1:9000/``
    or ``http://169.254.169.254/latest/meta-data/`` was fetched anyway, and the guard's promise
    (checked "before we ever open a socket") held only for the first hop. The same loop is what
    makes a private Google file visible: its export URL answers with a redirect to a sign-in host,
    which is reported as ``not_shared`` instead of being followed.
    """
    import httpx

    headers = {"User-Agent": USER_AGENT}
    current = url
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            with httpx.stream(
                "GET", current, headers=headers, timeout=FETCH_TIMEOUT, follow_redirects=False
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
                        raise PageFetchError(f"web fetch: redirect to {current} {safety_error}",
                                             REASON_BLOCKED)
                    if is_login_host(urlsplit(current).hostname or ""):
                        raise PageFetchError(
                            f"web fetch: {url} redirects to a sign-in page ({current})",
                            REASON_NOT_SHARED)
                    continue
                content_type = content_type_header.split(";")[0].strip().lower()
                if status_code >= 400:
                    raise status_failure(status_code, current)
                if any(content_type.startswith(p) for p in UNREADABLE_PREFIXES):
                    raise UnsupportedContentTypeError(
                        f"web fetch: unsupported content-type {content_type!r} for {url}")
                is_text = any(content_type.startswith(p) for p in TEXT_CONTENT_PREFIXES)
                is_pdf = "pdf" in content_type
                sniff = not content_type or content_type == "application/octet-stream"
                if not (is_text or is_pdf or sniff):
                    raise UnsupportedContentTypeError(
                        f"web fetch: unsupported content-type {content_type!r} for {url}")
                declared = resp.headers.get("content-length") or ""
                # A declared PDF may be large; an undeclared type is read up to the PDF cap too and
                # cut back to the page cap below if it turns out not to be a PDF.
                byte_cap = MAX_PDF_BYTES if (is_pdf or sniff) else MAX_PAGE_BYTES
                if (is_pdf or sniff) and declared.isdigit() and int(declared) > byte_cap:
                    raise PageFetchError(
                        f"web fetch: {url} is {declared} bytes, over the {byte_cap} byte cap",
                        REASON_TOO_LARGE)
                chunks: List[bytes] = []
                total = 0
                truncated = False
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    if total > byte_cap:
                        truncated = True
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks)
            if is_pdf or (sniff and looks_like_pdf(raw)):
                if truncated:
                    raise PageFetchError(
                        f"web fetch: {url} is over the {MAX_PDF_BYTES} byte cap", REASON_TOO_LARGE)
                return FetchedPage(html="", content_type="application/pdf", status_code=status_code,
                                   data=raw, final_url=current)
            if sniff and len(raw) > MAX_PAGE_BYTES:
                raw = raw[:MAX_PAGE_BYTES]
                truncated = True
            if sniff and (b"\x00" in raw[:2048]):
                if truncated:
                    raise PageFetchError(
                        f"web fetch: {url} is over the {MAX_PAGE_BYTES} byte cap", REASON_TOO_LARGE)
                raise UnsupportedContentTypeError(
                    f"web fetch: {url} is a binary file ({content_type or 'unknown type'})")
            return FetchedPage(
                html=decode_page(raw, content_type_header),
                content_type=content_type or "text/plain",
                status_code=status_code,
                truncated=truncated,
                final_url=current,
            )
        raise PageFetchError(f"web fetch: too many redirects for {url}")
    except (PageFetchError, UnsupportedContentTypeError):
        raise
    except httpx.TimeoutException as exc:
        raise PageFetchError(f"web fetch: request timed out for {current}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise PageFetchError(f"web fetch: request failed for {current}: {exc}") from exc
