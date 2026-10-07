"""web_page_extract -- SSRF guard, main-text HTML extraction, and focus-scored passage selection.

Three small, independent pieces used by ``web_research.WebResearchAdapter.fetch()``:

  * ``check_url_is_safe``      -- refuse non-http(s) schemes and private/loopback/link-local hosts
                                   before we ever open a socket.
  * ``extract_main_text``      -- turn raw page HTML into ``(title, text)``, dropping chrome
                                   (nav/header/footer/ads/etc.) and preferring ``<article>``/
                                   ``<main>`` content when it's substantial. Tries ``trafilatura``
                                   first if it happens to be installed (it is NOT a hard
                                   dependency), falling back to a stdlib ``html.parser`` extractor
                                   that has to be good on its own.
  * ``split_passages`` /
    ``select_passages``        -- break extracted text into ~paragraph passages and keep only the
                                   ones relevant to a focus string, within a token budget, scored
                                   with a tiny from-scratch BM25 (no index, no persistence --
                                   O(passages) per fetch, which is all a single page needs).

Stdlib only. No network I/O happens in this module.
"""
from __future__ import annotations

import html as html_lib
import ipaddress
import logging
import math
import re
import socket
from html.parser import HTMLParser
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("quest-ai-runner.web-page-extract")

# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

Resolver = Callable[[str], Sequence[str]]


def default_resolver(host: str) -> List[str]:
    """Resolve ``host`` to its IP addresses via ``socket.getaddrinfo``. Returns [] on failure."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return []
    return sorted({info[4][0] for info in infos if info and info[4]})


def _is_private_address(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # unparsable -- treat as unsafe rather than let it through
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url_is_safe(url: str, *, resolver: Optional[Resolver] = None) -> Optional[str]:
    """Return an error message if ``url`` is unsafe to fetch from this process, else ``None``.

    Refuses:
      * any scheme other than http/https (``file://``, ``ftp://``, etc.)
      * a literal IP, or a hostname that resolves to one, that is loopback/private/link-local/
        reserved/multicast (127.0.0.1, 10.x, 169.254.x, ...)

    ``resolver`` is injectable so tests can simulate DNS without real network access.
    """
    resolve = resolver or default_resolver
    try:
        parts = urlsplit(url)
    except ValueError:
        return f"refused: could not parse URL {url!r}"

    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"refused: unsupported URL scheme {scheme or '(none)'!r}"

    host = parts.hostname or ""
    if not host:
        return "refused: URL has no host"

    try:
        ipaddress.ip_address(host)
        is_literal_ip = True
    except ValueError:
        is_literal_ip = False

    if is_literal_ip:
        if _is_private_address(host):
            return f"refused: {host} is a private/loopback/link-local address"
        return None

    addrs = resolve(host)
    if not addrs:
        return f"refused: could not resolve host {host!r}"
    for addr in addrs:
        if _is_private_address(addr):
            return f"refused: {host} resolves to a private/loopback/link-local address ({addr})"
    return None


# ---------------------------------------------------------------------------
# HTML main-text extraction
# ---------------------------------------------------------------------------

_DROP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "iframe"}
_BLOCK_TAGS = {"p", "div", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "section"}
# HTML5 void elements: never have a closing tag, so they must never be pushed onto the
# open-tag stack (see handle_starttag) -- there is nothing "inside" them to skip or keep.
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
_NOISE_CLASS_RE = re.compile(r"nav|menu|footer|header|sidebar|cookie|banner|subscribe|advert|share|comment", re.I)
_MIN_ARTICLE_CHARS = 200
#: Below this many extracted characters, the structured pass is treated as having failed outright
#: and ``_salvage_text`` gets a chance (see ``_extract_builtin``).
_SALVAGE_WHEN_UNDER_CHARS = 100


class _MainTextParser(HTMLParser):
    """A cheap chrome-stripping HTML parser: drops script/nav/ads/etc., prefers article/main."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self._in_title = False
        self._skip_depth = 0
        self._article_depth = 0
        # (tag, is_dropped) stack, matched loosely (HTML in the wild isn't always well-formed).
        self._tag_stack: List[Tuple[str, bool]] = []
        self.article_chunks: List[str] = []
        self.general_chunks: List[str] = []

    @staticmethod
    def _is_noise(attrs: List[Tuple[str, Optional[str]]]) -> bool:
        for k, v in attrs:
            if k in ("class", "id") and v and _NOISE_CLASS_RE.search(v):
                return True
        return False

    def _active_target(self) -> List[str]:
        return self.article_chunks if self._article_depth > 0 else self.general_chunks

    def _emit_break(self) -> None:
        target = self._active_target()
        if target and target[-1] != "\n":
            target.append("\n")

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag == "br":
            self._emit_break()
        if tag in _VOID_TAGS:
            # Void elements (input, img, meta, link, hr, ...) never get a matching
            # handle_endtag, so they must NEVER be pushed onto the stack / affect
            # skip_depth: a dropped void element (e.g. an <input class="...menu...">) would
            # otherwise increment skip_depth permanently, since nothing ever decrements it
            # back -- which silently blanked out the rest of the document in practice (hit
            # on a real Wikipedia page via an <input> carrying a "main-menu" class).
            return
        if tag == "title":
            self._in_title = True
        # The noise-class check never applies to html/head/body: real pages commonly carry
        # long feature-flag class lists on these ROOT elements (e.g. Wikipedia's
        # "vector-feature-main-menu-pinned-disabled" on <html>) whose substrings happen to
        # match nav/menu/header/etc even though the element is not navigation chrome at all.
        # Dropping a root element drops the entire page, so it's excluded outright.
        dropped = tag in _DROP_TAGS or (tag not in ("html", "head", "body") and self._is_noise(attrs))
        self._tag_stack.append((tag, dropped))
        if dropped:
            self._skip_depth += 1
        if tag in ("article", "main"):
            self._article_depth += 1

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "br":
            self._emit_break()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
        for i in range(len(self._tag_stack) - 1, -1, -1):
            if self._tag_stack[i][0] == tag:
                _, was_dropped = self._tag_stack.pop(i)
                if was_dropped:
                    self._skip_depth = max(0, self._skip_depth - 1)
                break
        if tag in ("article", "main"):
            self._article_depth = max(0, self._article_depth - 1)
        if tag in _BLOCK_TAGS:
            self._emit_break()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth > 0:
            return
        if not data or not data.strip():
            return
        self._active_target().append(data)


def _quick_title(raw_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
    if not match:
        return ""
    return re.sub(r"\s+", " ", html_lib.unescape(match.group(1))).strip()


def _render_body(raw: str) -> str:
    """Collapse intra-line whitespace, keep paragraph breaks, unescape entities."""
    unescaped = html_lib.unescape(raw)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in unescaped.split("\n")]
    lines = [ln for ln in lines if ln]
    return "\n\n".join(lines)


_SCRIPTISH_BLOCK_RE = re.compile(
    r"<(script|style|noscript|svg|template)\b[^>]*>.*?</\1\s*>", re.I | re.S
)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|li|ul|ol|h[1-6]|tr|br|section|article|main|table|blockquote|pre)\b[^>]*>", re.I
)


def _salvage_text(raw_html: str) -> str:
    """Last-resort extraction: drop script/style blocks, turn block tags into breaks, strip the
    rest. Keeps page chrome that the structured parser would have dropped, so it is only used
    when the structured parser produced (almost) nothing.

    Why it exists: the structured parser tracks "am I inside dropped chrome" with a depth counter
    decremented by the matching end tag, and real HTML often never sends one. An unclosed
    ``<aside class="sidebar">`` or ``<div class="banner">`` therefore left the counter pinned
    above zero and silently blanked the ENTIRE rest of the document (measured: 0 characters
    extracted from a page whose body was 3 KB of article text). The 2 MB read cap makes this
    systematic rather than rare, because a truncated document is an unclosed document by
    construction.
    """
    without_scripts = _SCRIPTISH_BLOCK_RE.sub(" ", raw_html or "")
    broken = _BLOCK_TAG_RE.sub("\n", without_scripts)
    stripped = re.sub(r"<[^>]*>", " ", broken)
    return _render_body(stripped)


def _extract_builtin(raw_html: str) -> Tuple[str, str]:
    parser = _MainTextParser()
    try:
        parser.feed(raw_html)
    except Exception:  # noqa: BLE001
        logger.debug("web_page_extract: HTML parse failed", exc_info=True)

    title = re.sub(r"\s+", " ", html_lib.unescape("".join(parser.title_parts))).strip()
    if not title:
        title = _quick_title(raw_html)

    article_text = "".join(parser.article_chunks)
    general_text = "".join(parser.general_chunks)
    body = article_text if len(article_text.strip()) >= _MIN_ARTICLE_CHARS else (general_text or article_text)
    rendered = _render_body(body)
    # Deliberately narrow: fires only when the structured pass came back with essentially nothing
    # AND the salvage finds a real page's worth of text. A page whose correct extraction is merely
    # SHORT (a stub, a notice, a disambiguation page) must keep its chrome dropped rather than get
    # the cookie banner pasted back in.
    if len(rendered.strip()) < _SALVAGE_WHEN_UNDER_CHARS:
        salvaged = _salvage_text(raw_html).strip()
        if len(salvaged) >= _MIN_ARTICLE_CHARS and len(salvaged) > 3 * len(rendered.strip()):
            return title, salvaged
    return title, rendered


def extract_main_text(raw_html: str) -> Tuple[str, str]:
    """Return ``(title, text)`` extracted from ``raw_html``.

    Tries ``trafilatura`` first when it happens to be installed (it is an optional dependency,
    never required), and falls back to the built-in stdlib extractor when trafilatura is absent
    or returns too little to be useful.
    """
    try:
        import trafilatura  # type: ignore  # optional dependency

        extracted = trafilatura.extract(raw_html, favor_precision=False, include_tables=False)
        if extracted and len(extracted.strip()) >= _MIN_ARTICLE_CHARS:
            title = ""
            try:
                meta = trafilatura.extract_metadata(raw_html)
                title = (getattr(meta, "title", "") or "") if meta else ""
            except Exception:  # noqa: BLE001
                title = ""
            if not title:
                title = _quick_title(raw_html)
            return title, extracted.strip()
    except ImportError:
        pass
    except Exception:  # noqa: BLE001
        logger.debug("web_page_extract: trafilatura extraction failed", exc_info=True)

    return _extract_builtin(raw_html)


# ---------------------------------------------------------------------------
# Passage splitting
# ---------------------------------------------------------------------------

_PASSAGE_MIN_WORDS = 20
_PASSAGE_MAX_WORDS = 120


def split_passages(text: str) -> List[str]:
    """Split ``text`` into ~paragraph passages: merge tiny ones, split huge ones to ~120 words."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return []

    passages: List[str] = []
    buffer = ""
    for para in paragraphs:
        words = para.split()
        if len(words) > _PASSAGE_MAX_WORDS:
            if buffer:
                passages.append(buffer)
                buffer = ""
            for i in range(0, len(words), _PASSAGE_MAX_WORDS):
                passages.append(" ".join(words[i : i + _PASSAGE_MAX_WORDS]))
            continue

        candidate = f"{buffer} {para}".strip() if buffer else para
        if len(candidate.split()) < _PASSAGE_MIN_WORDS:
            buffer = candidate
            continue
        passages.append(candidate)
        buffer = ""

    if buffer:
        if passages:
            passages[-1] = f"{passages[-1]} {buffer}".strip()
        else:
            passages.append(buffer)
    return passages


# ---------------------------------------------------------------------------
# Focus scoring (tiny BM25, no index) + token-budget selection
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for", "is", "are",
    "was", "were", "be", "been", "being", "with", "as", "by", "at", "from", "that", "this",
    "these", "those", "it", "its", "into", "about", "than", "then", "so", "such", "not", "no",
    "do", "does", "did", "can", "will", "would", "should", "could", "has", "have", "had", "i",
    "you", "he", "she", "they", "we", "their", "his", "her", "them", "what", "which", "who",
    "whom", "there", "here", "when", "where", "how", "all", "any", "each", "more", "most",
    "other", "some", "only", "own", "same", "also", "up", "out", "over", "under", "again",
}

_WORD_RE = re.compile(r"[a-z0-9']+")


def _tokenize(text: str) -> List[str]:
    return [t for t in _WORD_RE.findall((text or "").lower()) if t and t not in _STOPWORDS]


def estimate_tokens(text: str) -> int:
    """Cheap token estimate: ~4 chars/token. Good enough for a budget, not a billing figure."""
    return max(0, len(text or "") // 4)


def _bm25_lite_scores(passages: List[str], query_tokens: List[str]) -> List[float]:
    """A tiny from-scratch BM25 over ``passages`` for ``query_tokens``. No index, no persistence."""
    if not query_tokens or not passages:
        return [0.0] * len(passages)

    k1, b = 1.5, 0.75
    docs_tokens = [_tokenize(p) for p in passages]
    n_docs = len(docs_tokens)
    avg_len = (sum(len(d) for d in docs_tokens) / n_docs) if n_docs else 1.0

    df: Dict[str, int] = {}
    for toks in docs_tokens:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1

    query_set = set(query_tokens)
    scores: List[float] = []
    for toks in docs_tokens:
        if not toks:
            scores.append(0.0)
            continue
        tf: Dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        score = 0.0
        for term in query_set:
            f = tf.get(term, 0)
            if f == 0:
                continue
            n_t = df.get(term, 0)
            idf = math.log(1 + (n_docs - n_t + 0.5) / (n_t + 0.5))
            denom = f + k1 * (1 - b + b * (dl / avg_len if avg_len else 1.0))
            score += idf * (f * (k1 + 1)) / (denom or 1.0)
        scores.append(score)
    return scores


def select_passages(passages: List[str], focus: str, token_budget: int) -> List[str]:
    """Return the passages most relevant to ``focus``, within ``token_budget``, in document order.

    If ``focus`` is empty, keeps the leading passages up to the budget (no scoring). At least one
    passage is always returned when ``passages`` is non-empty, even if it alone exceeds the budget.
    """
    if not passages:
        return []

    if not (focus or "").strip():
        kept: List[str] = []
        used = 0
        for p in passages:
            cost = estimate_tokens(p)
            if kept and used + cost > token_budget:
                break
            kept.append(p)
            used += cost
        return kept

    query_tokens = _tokenize(focus)
    scores = _bm25_lite_scores(passages, query_tokens)
    order = sorted(range(len(passages)), key=lambda i: scores[i], reverse=True)

    chosen: List[int] = []
    used = 0
    for i in order:
        if scores[i] <= 0 and chosen:
            break  # once we have something, stop pulling in zero-relevance passages
        cost = estimate_tokens(passages[i])
        if chosen and used + cost > token_budget:
            continue
        chosen.append(i)
        used += cost
        if used >= token_budget:
            break

    if not chosen:
        chosen = [order[0]]
    chosen.sort()
    return [passages[i] for i in chosen]
