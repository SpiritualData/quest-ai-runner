"""link_detect -- deterministic detection of the links in a user's message.

The planner used to have to NOTICE a pasted URL on its own, and three prompt rules pulled in
different directions about what to do with one. This module takes the noticing away from the
model: it extracts the http(s) URLs from the message with a regex (no model call, no network) and
labels what each one is, so the planner prompt can list them explicitly.

Pure functions. Importing nothing from the brain keeps it trivially testable.
"""
from __future__ import annotations

import re
from typing import List, Tuple
from urllib.parse import urlsplit

#: Characters that end a URL in running prose or markdown.
_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\[{}|\\^]+", re.I)
_TRAILING_PUNCT = ".,;:!?*_~"

#: At most this many links are listed to the planner; the rest are counted.
MAX_LISTED_LINKS = 5


def extract_urls(text: str) -> List[str]:
    """The distinct http(s) URLs in ``text``, in order of first appearance.

    Handles bare URLs, URLs in parentheses, angle brackets and markdown links
    (``[label](https://...)``), and strips trailing sentence punctuation. A URL that has balanced
    parentheses inside it (``https://en.wikipedia.org/wiki/Foo_(bar)``) keeps them. Never raises.
    """
    found: List[str] = []
    seen = set()
    try:
        raw = text or ""
        for m in _URL_RE.finditer(raw):
            url = m.group(0).rstrip(_TRAILING_PUNCT)
            # A closing paren with no opener belongs to the prose or markdown around the URL; one
            # that closes a paren inside the URL (Wikipedia style) stays.
            while url.endswith(")") and url.count(")") > url.count("("):
                url = url[:-1].rstrip(_TRAILING_PUNCT)
            if not urlsplit(url).hostname:
                continue
            if url not in seen:
                seen.add(url)
                found.append(url)
    except Exception:  # noqa: BLE001
        return found
    return found


def describe_link(url: str) -> str:
    """A short, honest label for what a link points at, from its address alone."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = (parts.path or "").lower()
    except ValueError:
        return "link"
    if host == "docs.google.com":
        if "/document/" in path:
            return "Google Doc"
        if "/spreadsheets/" in path:
            return "Google Sheet"
        if "/presentation/" in path:
            return "Google Slides"
        if "/forms/" in path:
            return "Google Form"
    if host == "drive.google.com":
        if "/folders/" in path:
            return "Google Drive folder"
        return "Google Drive file"
    if path.endswith(".pdf"):
        return "PDF"
    return "web page"


def links_in_message(text: str) -> List[Tuple[str, str]]:
    """``[(url, label), ...]`` for the links in ``text`` (every one, not capped)."""
    return [(u, describe_link(u)) for u in extract_urls(text)]
