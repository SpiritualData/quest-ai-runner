"""link_guard -- no reply leaves the brain carrying a link nobody checked.

The risk this manages (product owner, 2026-09-28): a model writes a URL that reads perfectly and
does not exist. It happened for real in Quest chat, where replies linked
``https://ai.batmanhq.duckdns.org/api/tasks/<id>``: a plausible, wrong address that answers 401
JSON and is not a page anyone can open. The reader taps it, gets nothing, and stops trusting every
other link in the thread.

So every terminal reply is run through ``LinkGuard.sanitize`` before it is emitted. Each link it
finds gets one of three verdicts:

* ``ok``      -- proven to exist. An external URL that answered (2xx/3xx, or a 401/403 that is
                 itself a PAGE: HTML such as a sign-in wall or a private document, never a JSON or
                 other API answer, which no reader can open), a host on the trusted list, an in-app
                 pseudo-scheme the consumer declared, or an internal path that matches the host
                 app's REAL route table.
* ``dead``    -- proven not to exist: 404/410, a host that does not resolve, a refused connection,
                 a 401/403 that is not a page, or an internal path with no matching route.
* ``unknown`` -- could not be settled this pass (timeout, 5xx, network down, or no route table
                 configured to check an internal path against).

``ok`` links are left exactly as written. ``dead`` and ``unknown`` links are stripped: the author's
label survives as plain text with a short "(link removed: ...)" note, and the URL itself does not
reach the reader. Unverified is treated like dead on purpose: a link we cannot stand behind is not
a link worth sending, and the note tells the reader the truth instead of a dead tap.

MULTIPLE PASSES, which is the part that matters. One network reading is noisy: a slow host or a
blip would delete a perfectly good link. So an ``unknown`` is re-checked up to ``policy.passes``
times before the verdict sticks, and after the text is rewritten it is SCANNED AGAIN and the whole
process repeats until a scan finds nothing left to strip (a fixed point, bounded by
``policy.passes``). Rewrites can introduce text that scans differently, and the fixed point is what
guarantees the emitted string has no unverified URL in it, rather than merely that the first pass
handled the ones it happened to see.

Everything is stdlib (``urllib``), matching the rest of the core, and every network call is
bounded by ``policy.timeout`` with a per-instance result cache so a long-lived poller checks the
same URL once, not once per turn.

PROVENANCE (opt-in, ``LinkPolicy.require_provenance``). Reachable is not the same as real: a model
can invent a URL that happens to resolve (a home page, a search page) and present it as the answer.
With the flag on, an external link is ``ok`` only if it is both reachable AND appears in the material
the turn actually had (``sanitize(text, evidence=...)``): the user's words, the conversation, what
was read, searched or fetched, and the context in the prompt. In-app links are judged by the route
table and are exempt, as are mailto/tel and declared schemes.

GENERIC BY CONSTRUCTION: this module knows nothing about Quest. The route table, the app's own
origins, the trusted hosts, the in-app schemes and the rewrite rules all come from a consumer-
supplied ``LinkPolicy`` (in practice a JSON file named by ``QAR_LINK_POLICY_FILE``). With no policy
configured, external URLs are still checked over the network, in-app paths cannot be checked
against anything and therefore come back ``unknown``.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit

log = logging.getLogger(__name__)

# Verdict values (a str, not an enum, so a verdict survives a round trip through JSON/event data).
OK = "ok"
DEAD = "dead"
UNKNOWN = "unknown"


# ---------------------------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------------------------

@dataclass
class LinkPolicy:
    """What "verified" means for one host application. All of it is consumer-supplied data."""

    # Route patterns the host app actually has, in expo-router / Next-style notation:
    # "/profile/ai-tasks", "/quest/[questId]/chat", "/events/[slug]/[...rest]". An internal path
    # that matches none of these is FABRICATED, and that is the whole point of the table.
    routes: Tuple[str, ...] = ()
    # Hosts that ARE the app: an absolute URL on one of these is checked as an internal route
    # rather than fetched, because the route table is a stronger check than an HTTP status from a
    # single-page app that serves 200 for every path (including the ones it has no screen for).
    origins: Tuple[str, ...] = ()
    # Hosts accepted without a network call. For hosts that are known-good and slow, rate-limited,
    # or hostile to HEAD. Keep it short: every entry is a promise nobody re-checks.
    trusted_hosts: Tuple[str, ...] = ()
    # In-app pseudo-schemes the consumer's renderer handles itself (Quest's "app-task:" routes a
    # tap to the task dashboard). Never fetched, always kept.
    allowed_schemes: Tuple[str, ...] = ("app-task",)
    # (regex, replacement) pairs applied to a URL BEFORE it is judged. This is how a known-wrong
    # address the model keeps writing gets turned into the right one instead of merely deleted.
    rewrites: Tuple[Tuple[str, str], ...] = ()
    # Turn the network arm off entirely (tests, air-gapped hosts). External URLs then come back
    # unknown unless their host is trusted, so they are stripped, which is the safe direction.
    check_external: bool = True
    # Seconds for one HTTP check. Short on purpose: this sits between the answer and the reader.
    timeout: float = 4.0
    # How many times an unsettled link is re-checked, and the cap on rescan rounds. 1 would make
    # a single slow response delete a good link.
    passes: int = 3
    # Hard cap on links checked in one reply, so a pathological answer cannot stall a turn.
    max_urls: int = 25
    # How long a cached verdict stays good, in seconds (default 1 hour).
    cache_ttl: float = 3600.0
    # How many distinct URLs ``sanitize()`` checks at once. A reply on the answer path can carry up
    # to ``max_urls`` links, each worth up to ``passes`` x ``timeout`` seconds if checked one at a
    # time; checking them concurrently is what keeps a reply full of slow links from holding up the
    # whole turn.
    concurrency: int = 8
    # Wall-clock budget, in seconds, for one whole ``sanitize()`` call (every pass, every link
    # together). Whatever has not settled when it elapses is treated as ``unknown`` (stripped, same
    # as any other unverified link) rather than making the reply wait on it.
    overall_timeout: float = 8.0
    # Opt-in provenance rule: an EXTERNAL link counts as ``ok`` only if it is reachable AND was
    # present in the evidence passed to ``sanitize`` (compared case-insensitively, ignoring scheme,
    # a leading ``www.``, fragments and trailing slashes). Off by default: a library consumer that
    # cannot supply the turn's material would otherwise strip every external link.
    require_provenance: bool = False
    # Optional consumer hook that judges the PARAMETERS of an in-app route that already matched the
    # table: ``(route_pattern, params) -> verdict``. ``route_pattern`` is the matched pattern as
    # written in ``routes`` ("/quest/[questId]/chat"); ``params`` maps each bracket name to the
    # concrete path segment ({"questId": "..."}). A route table only proves a SCREEN exists, not
    # that the id in the link names something the reader can open; this is where a consumer says so.
    # Return ``None``, ``True`` or ``"ok"`` to accept; ``False`` or ``"dead"`` to reject with a
    # default reason; ``"unknown"`` when it could not decide; or a ``(verdict, reason)`` pair to
    # supply the reason. An exception counts as ``unknown``. It runs on a worker thread and must be
    # quick. Code-only: it cannot come from a JSON policy file.
    route_validator: Optional[Callable[[str, Dict[str, str]], Any]] = None

    @staticmethod
    def from_dict(data: Dict[str, Any], *, base_dir: Optional[Path] = None) -> "LinkPolicy":
        """Build a policy from plain JSON data.

        ``routes_file`` is resolved relative to ``base_dir`` (the policy file's own directory) and
        may be either a bare list of routes or the ``{"routes": [...]}`` object a route generator
        emits, so the host app can keep its route table generated and current in its own repo
        instead of copied into this one.
        """
        routes: List[str] = [str(r) for r in (data.get("routes") or []) if str(r).strip()]
        routes_file = (data.get("routes_file") or "").strip()
        if routes_file:
            p = Path(routes_file)
            if not p.is_absolute() and base_dir is not None:
                p = base_dir / p
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001 -- a missing route table must not break a turn
                log.warning("link guard: routes_file %s unreadable (%s: %s); internal paths will "
                            "come back unverified", p, type(e).__name__, e)
                loaded = None
            if isinstance(loaded, dict):
                loaded = loaded.get("routes")
            if isinstance(loaded, list):
                routes.extend(str(r) for r in loaded if str(r).strip())
        rewrites: List[Tuple[str, str]] = []
        for item in (data.get("rewrites") or []):
            if isinstance(item, dict) and item.get("match"):
                rewrites.append((str(item["match"]), str(item.get("replace") or "")))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                rewrites.append((str(item[0]), str(item[1])))
        def _tuple(key: str, default: Sequence[str] = ()) -> Tuple[str, ...]:
            vals = data.get(key)
            if vals is None:
                return tuple(default)
            return tuple(str(v).strip().lower() for v in vals if str(v).strip())
        return LinkPolicy(
            routes=tuple(dict.fromkeys(routes)),
            origins=_tuple("origins"),
            trusted_hosts=_tuple("trusted_hosts"),
            allowed_schemes=_tuple("allowed_schemes", ("app-task",)),
            rewrites=tuple(rewrites),
            check_external=bool(data.get("check_external", True)),
            timeout=float(data.get("timeout", 4.0)),
            passes=max(1, int(data.get("passes", 3))),
            max_urls=max(1, int(data.get("max_urls", 25))),
            cache_ttl=float(data.get("cache_ttl", 3600.0)),
            concurrency=max(1, int(data.get("concurrency", 8))),
            overall_timeout=float(data.get("overall_timeout", 8.0)),
            require_provenance=bool(data.get("require_provenance", False)),
        )

    @staticmethod
    def from_file(path: str) -> Optional["LinkPolicy"]:
        """Load a policy JSON file. Returns None (never raises) when it cannot be read."""
        try:
            p = Path(path).expanduser()
            return LinkPolicy.from_dict(json.loads(p.read_text(encoding="utf-8")), base_dir=p.parent)
        except Exception as e:  # noqa: BLE001 -- a broken policy file must not break every turn
            log.warning("link guard: policy file %s unusable (%s: %s); falling back to defaults",
                        path, type(e).__name__, e)
            return None


@dataclass
class LinkVerdict:
    """One link's judgement, kept for logging and for the consumer's trace panel."""
    url: str
    verdict: str            # OK | DEAD | UNKNOWN
    reason: str = ""
    replaced_with: Optional[str] = None   # set when a rewrite rule redirected it to a real address
    note: str = ""          # the reader-facing stand-in when this link is stripped (else a default)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"url": self.url, "verdict": self.verdict}
        if self.reason:
            d["reason"] = self.reason
        if self.replaced_with:
            d["replaced_with"] = self.replaced_with
        return d


# ---------------------------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------------------------

# One left-to-right scan, trying each alternative at every position, exactly like the frontend's
# linkifyTaskMentions: whichever alternative matches a stretch of text owns it, so a URL inside a
# fenced block or an inline code span is never treated as a link (it is being SHOWN, not offered).
# Groups: 1 = markdown label, 2 = markdown target, 3 = angle autolink, 4 = bare URL.
SCAN_RE = re.compile(
    r"```[\s\S]*?```"                                   # fenced code block, untouched
    r"|`[^`\n]*`"                                       # inline code span, untouched
    r"|\[([^\]\n]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)"   # [label](target "title")
    r"|<((?:[a-zA-Z][\w+.-]*:)[^>\s]+)>"                # <https://...> autolink
    r"|(?<![\w@/])((?:https?://|www\.)[^\s<>()\[\]\"'`]+)"    # bare URL
)

# Trailing characters that are almost always sentence punctuation rather than part of the address.
TRAILING_PUNCT = ".,;:!?'\"*_)]}>"


def trim_url(url: str) -> str:
    """Strip sentence punctuation a bare URL swallowed at the end. Balanced parens are kept."""
    out = url
    while out and out[-1] in TRAILING_PUNCT:
        if out[-1] == ")" and out.count("(") >= out.count(")"):
            break
        out = out[:-1]
    return out


def route_to_regex(pattern: str) -> re.Pattern:
    """Compile one route pattern ("/quest/[questId]/chat") into a matcher for a real path.

    ``[param]`` matches one segment, ``[...rest]`` matches the remainder. A trailing slash is
    optional; the query string and fragment are matched separately by the caller, not here.
    """
    parts: List[str] = []
    for seg in pattern.strip("/").split("/"):
        if not seg:
            continue
        if seg.startswith("[...") and seg.endswith("]"):
            parts.append(r".+")
        elif seg.startswith("[") and seg.endswith("]"):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(seg))
    body = "/".join(parts)
    return re.compile(rf"^/{body}/?$" if body else r"^/$")


def route_to_matcher(pattern: str) -> Tuple[re.Pattern, Tuple[str, ...]]:
    """Like ``route_to_regex`` but every ``[param]`` and ``[...rest]`` is a capture group, and the
    bracket names come back in order, so a matched path can be turned into ``{name: value}``."""
    parts: List[str] = []
    names: List[str] = []
    for seg in pattern.strip("/").split("/"):
        if not seg:
            continue
        if seg.startswith("[...") and seg.endswith("]"):
            parts.append(r"(.+)")
            names.append(seg[4:-1])
        elif seg.startswith("[") and seg.endswith("]"):
            parts.append(r"([^/]+)")
            names.append(seg[1:-1])
        else:
            parts.append(re.escape(seg))
    body = "/".join(parts)
    return re.compile(rf"^/{body}/?$" if body else r"^/$"), tuple(names)



# ---------------------------------------------------------------------------------------------
# Provenance: was this address in material the turn actually had?
# ---------------------------------------------------------------------------------------------

EVIDENCE_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\[\]\"'`]+", re.IGNORECASE)


GOOGLE_DOC_HOSTS = ("docs.google.com", "drive.google.com", "sheets.google.com", "slides.google.com")
GOOGLE_DOC_ID_RES = (
    re.compile(r"/d/([\w-]{10,})"),                 # /document/d/<ID>/edit, /file/d/<ID>/view, /d/<ID>
    re.compile(r"/folders/([\w-]{10,})"),           # drive folders
    re.compile(r"[?&]id=([\w-]{10,})"),             # drive.google.com/open?id=<ID>
)


def google_document_key(url_no_scheme: str) -> Optional[str]:
    """For a Google Docs, Sheets, Slides, Forms or Drive address, a key made of the DOCUMENT ID, so
    ``/d/<ID>/edit``, ``/d/<ID>``, ``/d/<ID>/view?usp=sharing`` and ``/open?id=<ID>`` all match each
    other. None for any other address."""
    host = url_no_scheme.split("/", 1)[0].split("?", 1)[0]
    if host not in GOOGLE_DOC_HOSTS:
        return None
    rest = url_no_scheme[len(host):]
    for rx in GOOGLE_DOC_ID_RES:
        m = rx.search(rest)
        if m:
            return "google-document:" + m.group(1)
    return None


def normalize_url_for_match(url: str) -> str:
    """The comparison key for provenance: case, scheme, ``www.``, fragment and trailing slash fold.

    A Google document (Docs, Sheets, Slides, Forms, Drive) is keyed by its document id alone, since
    the same document is written ``/edit``, ``/view``, ``/preview`` or bare depending on who shared it.
    The id is case sensitive, so it is taken from the original text, not the lowered copy."""
    raw = (url or "").strip().strip("<>")
    t = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    t = re.sub(r"^www\.", "", t, flags=re.IGNORECASE)
    t = t.split("#", 1)[0]
    head = t.split("/", 1)[0].lower()
    doc = google_document_key(head + t[len(head):])
    if doc:
        return doc
    return t.lower().rstrip("/")


def evidence_text(*sources: Any) -> str:
    """Flatten whatever the turn held (strings, dicts, lists, anything with ``__str__``) to one text.

    Walks containers rather than JSON-encoding them, so an address is never escaped on the way in.
    """
    out: List[str] = []

    def walk(x: Any, depth: int = 0) -> None:
        if x is None or depth > 12:
            return
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(k, depth + 1)
                walk(v, depth + 1)
        elif isinstance(x, (list, tuple, set)):
            for v in x:
                walk(v, depth + 1)
        elif isinstance(x, (int, float, bool)):
            return
        else:
            out.append(str(x))

    for src in sources:
        walk(src)
    return "\n".join(out)


def evidence_urls(text: str) -> frozenset:
    """Every address mentioned in ``text``, normalised for matching."""
    return frozenset(normalize_url_for_match(trim_url(m.group(0)))
                     for m in EVIDENCE_URL_RE.finditer(text or ""))

# ---------------------------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------------------------

class LinkGuard:
    """Verify every link in a reply and strip the ones that do not check out.

    ``fetcher`` is the one network seam: a callable ``(url, timeout) -> (status, reason)`` where
    ``status`` is an int HTTP status or None when the request never got one. For a 401/403 it may add
    a third item, ``{"content_type": ...}``, which decides whether the gated answer is a page. Tests pass a fake and
    never touch the network.
    """

    def __init__(self, policy: Optional[LinkPolicy] = None,
                 *, fetcher: Optional[Callable[[str, float], Tuple[Any, ...]]] = None):
        self.policy = policy or LinkPolicy()
        self.fetch = fetcher or http_probe
        self.routes = [route_to_regex(r) for r in self.policy.routes]
        self.route_table = [(r, *route_to_matcher(r)) for r in self.policy.routes]
        self.rewrites = [(re.compile(m), r) for m, r in self.policy.rewrites]
        # url -> (verdict, reason, checked_at). Shared across turns for a long-lived process.
        self.cache: Dict[str, Tuple[str, str, float]] = {}

    # -- judging one link ----------------------------------------------------------------------

    def apply_rewrites(self, url: str) -> str:
        for rx, repl in self.rewrites:
            if rx.search(url):
                return rx.sub(repl, url)
        return url

    def unseen(self, url: str, target: str, seen_urls: Optional[frozenset]) -> Optional[LinkVerdict]:
        """The provenance verdict for an external link, or None when the rule does not apply."""
        if seen_urls is None or not self.policy.require_provenance:
            return None
        if normalize_url_for_match(url) in seen_urls or normalize_url_for_match(target) in seen_urls:
            return None
        return LinkVerdict(url, UNKNOWN, "not found in anything read this turn",
                           note="(link removed: I did not find this address in anything I read)")

    def settle_seen_unknown(self, verdict: str, reason: str,
                            seen_urls: Optional[frozenset]) -> Tuple[str, str]:
        """Under provenance, a link the turn read that the host merely would not confirm is kept.

        The link passed the provenance test, so it came from the turn's own search or page reads.
        An ``unknown`` probe (429 rate limit, 5xx, timeout, bot wall) says the check was unsettled,
        not that the page is absent, so it must not delete a real source the reader asked for.
        ``dead`` (404/410, no DNS, refused, not a page) still strips it.
        """
        if verdict == UNKNOWN and seen_urls is not None and self.policy.require_provenance:
            return OK, f"seen in this turn's material; host did not confirm ({reason})"
        return verdict, reason

    def verify(self, url: str, seen_urls: Optional[frozenset] = None) -> LinkVerdict:
        """Judge one URL. Re-checks an ``unknown`` up to ``policy.passes`` times before settling.

        ``seen_urls`` (from ``evidence_urls``) is the set of addresses the turn actually had; it only
        matters when ``policy.require_provenance`` is on, and only for external links.
        """
        rewritten = self.apply_rewrites(url)
        replaced = rewritten if rewritten != url else None
        target = rewritten

        scheme = ""
        if ":" in target.split("/", 1)[0]:
            scheme = target.split(":", 1)[0].lower()

        # In-app pseudo-schemes: the consumer's own renderer handles the tap, there is nothing to
        # fetch, and the consumer declared it, so it is verified by declaration.
        if scheme and scheme in self.policy.allowed_schemes:
            return LinkVerdict(url, OK, "in-app scheme", replaced)
        if scheme == "mailto":
            return LinkVerdict(url, OK, "mail address", replaced)
        if scheme == "tel":
            return LinkVerdict(url, OK, "phone number", replaced)

        # An internal path, either relative ("/profile/ai-tasks") or absolute on the app's own
        # origin. Judged against the route table, never fetched.
        if target.startswith("/") and not target.startswith("//"):
            return self.verify_path(target, url, replaced)
        if scheme in ("http", "https"):
            split = urlsplit(target)
            host = (split.hostname or "").lower()
            if host in self.policy.origins:
                path = split.path or "/"
                if split.query:
                    path_display = f"{path}?{split.query}"
                else:
                    path_display = path
                return self.verify_path(path_display, url, replaced)
            unseen = self.unseen(url, target, seen_urls)
            if unseen is not None:
                unseen.replaced_with = replaced
                return unseen
            if host in self.policy.trusted_hosts:
                return LinkVerdict(url, OK, "trusted host", replaced)
            if not self.policy.check_external:
                return LinkVerdict(url, UNKNOWN, "external checking is off", replaced)
            verdict, reason = self.check_external(target)
            verdict, reason = self.settle_seen_unknown(verdict, reason, seen_urls)
            return LinkVerdict(url, verdict, reason, replaced, note_for_reason(reason))
        if target.lower().startswith("www."):
            unseen = self.unseen(url, target, seen_urls)
            if unseen is not None:
                unseen.replaced_with = replaced
                return unseen
            if not self.policy.check_external:
                return LinkVerdict(url, UNKNOWN, "external checking is off", replaced)
            verdict, reason = self.check_external("https://" + target)
            verdict, reason = self.settle_seen_unknown(verdict, reason, seen_urls)
            return LinkVerdict(url, verdict, reason, replaced, note_for_reason(reason))

        # Anything else (a bare word in link position, an unknown scheme) is not something we can
        # stand behind.
        return LinkVerdict(url, UNKNOWN, "unrecognized link target", replaced)

    def verify_path(self, path: str, original: str, replaced: Optional[str]) -> LinkVerdict:
        """Match an in-app path against the host app's real route table."""
        if not self.routes:
            return LinkVerdict(original, UNKNOWN, "no route table configured", replaced)
        bare = path.split("#", 1)[0].split("?", 1)[0] or "/"
        for pattern, rx, names in self.route_table:
            m = rx.match(bare)
            if not m:
                continue
            validator = self.policy.route_validator
            if validator is None or not names:
                return LinkVerdict(original, OK, "matches an app route", replaced)
            params = {n: unquote(v) for n, v in zip(names, m.groups())}
            try:
                answer = validator(pattern, params)
            except Exception as e:  # noqa: BLE001 -- a failing hook must not break the reply
                log.warning("link guard: route_validator failed for %s: %s: %s",
                            pattern, type(e).__name__, e)
                return LinkVerdict(original, UNKNOWN, "could not check this page", replaced)
            verdict, reason = interpret_route_verdict(answer)
            if verdict == OK:
                return LinkVerdict(original, OK, reason or "matches an app route", replaced)
            return LinkVerdict(original, verdict, reason, replaced,
                               note=f"(link removed: {reason})")
        return LinkVerdict(original, DEAD, "no such screen in this app", replaced)

    def check_external(self, url: str) -> Tuple[str, str]:
        """Fetch a URL (cached, multi-pass) and turn the outcome into a verdict."""
        url = url.split("#", 1)[0]   # a fragment never reaches a server; one verdict per page
        hit = self.cache.get(url)
        now = time.time()
        if hit and (now - hit[2]) < self.policy.cache_ttl:
            return hit[0], hit[1]
        verdict, reason = UNKNOWN, "no response"
        for attempt in range(self.policy.passes):
            probed = self.fetch(url, self.policy.timeout)
            status, detail = probed[0], probed[1]
            info = probed[2] if len(probed) > 2 and isinstance(probed[2], dict) else None
            if status is not None and 200 <= status < 400:
                verdict, reason = OK, f"HTTP {status}"
                break
            if status in (401, 403):
                # The endpoint is there, it just will not serve US. That proves a PAGE exists only
                # when the answer is itself a page: a sign-in wall or a private document is HTML a
                # person can open, while a JSON 401 from an API is something nobody can open at all
                # (the exact shape of a fabricated /api/... address). A fetcher that reports no
                # content type (a consumer's own) keeps the earlier, trusting reading.
                ctype = str((info or {}).get("content_type") or "").lower()
                if info is None or "html" in ctype:
                    verdict, reason = OK, f"HTTP {status} (exists, access controlled)"
                else:
                    verdict, reason = DEAD, f"HTTP {status} {NOT_A_PAGE} ({ctype or 'no content type'})"
                break
            if status in (404, 410):
                verdict, reason = DEAD, f"HTTP {status}"
                break
            if detail in ("dns", "refused"):
                verdict, reason = DEAD, ("host does not resolve" if detail == "dns"
                                         else "connection refused")
                break
            verdict, reason = UNKNOWN, detail or (f"HTTP {status}" if status else "no response")
            if attempt + 1 < self.policy.passes:
                # A slow or briefly unhappy host gets another reading before we delete its link.
                time.sleep(0.2 * (attempt + 1))
        self.cache[url] = (verdict, reason, now)
        return verdict, reason

    # -- rewriting a whole reply ---------------------------------------------------------------

    def sanitize(self, text: str, *, evidence: Optional[str] = None) -> Tuple[str, List[LinkVerdict]]:
        """Return ``(safe_text, verdicts)``. Repeats until a scan finds nothing left to strip.

        ``evidence`` is the text of everything the turn actually had (see ``evidence_text``). It is
        consulted only when ``policy.require_provenance`` is on; ``None`` means "no evidence is
        available for this text", which exempts it from the provenance rule (reachability and the
        route table still apply).

        The whole call is held to ``policy.overall_timeout``: a reply full of slow links must not
        hold up a turn for minutes, so whatever is still unresolved when the budget runs out is
        treated as ``unknown``, same as any other link this guard could not stand behind.
        """
        if not text or not text.strip():
            return text, []
        verdicts: List[LinkVerdict] = []
        seen: Dict[str, LinkVerdict] = {}
        current = text
        deadline = time.time() + self.policy.overall_timeout
        seen_urls = evidence_urls(evidence) if evidence is not None else None
        for _round in range(self.policy.passes):
            current, changed = self.sanitize_once(current, seen, verdicts, deadline, seen_urls)
            if not changed:
                break
        return current, verdicts

    def _judge_pending(self, urls: List[str], seen: Dict[str, LinkVerdict],
                        verdicts: List[LinkVerdict], deadline: float,
                        seen_urls: Optional[frozenset] = None) -> None:
        """Judge every URL in ``urls`` (none yet in ``seen``), concurrently, within ``deadline``.

        Checking links one at a time is what let a reply full of slow hosts stall a turn: up to
        ``max_urls`` links, each up to ``passes`` x ``timeout`` seconds, back to back. A thread pool
        runs them side by side instead, and anything still outstanding when the deadline passes is
        recorded ``unknown`` without waiting on it further.
        """
        remaining = deadline - time.time()
        if remaining > 0 and urls:
            workers = min(len(urls), max(1, self.policy.concurrency))
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
            futures = {pool.submit(self.verify, url, seen_urls): url for url in urls}
            try:
                for fut in concurrent.futures.as_completed(futures, timeout=remaining):
                    url = futures[fut]
                    try:
                        v = fut.result()
                    except Exception as e:  # noqa: BLE001 -- one bad check must not sink the reply
                        v = LinkVerdict(url, UNKNOWN, f"{type(e).__name__}: {e}")
                    seen[url] = v
                    verdicts.append(v)
            except concurrent.futures.TimeoutError:
                pass
            finally:
                # Never block the reply on a straggler; threads still running finish on their own.
                pool.shutdown(wait=False)
        for url in urls:
            if url not in seen:
                v = LinkVerdict(url, UNKNOWN, "time limit reached before this link could be checked")
                seen[url] = v
                verdicts.append(v)

    def sanitize_once(self, text: str, seen: Dict[str, LinkVerdict],
                      verdicts: List[LinkVerdict], deadline: float,
                      seen_urls: Optional[frozenset] = None) -> Tuple[str, bool]:
        """One scan-and-rewrite pass. ``changed`` says whether anything was stripped or rewritten."""
        changed = False

        # Phase 1: find every not-yet-judged URL this pass would touch, capped at max_urls, and
        # judge them all together before rewriting anything. A second scan is cheap; a blocking
        # network call inside the substitution callback is what made link checks serialize.
        pending: List[str] = []

        def collect(m: re.Match) -> str:
            target = m.group(2) or m.group(3)
            bare = m.group(4)
            if bare is not None:
                target = trim_url(bare)
            if target and target not in seen and target not in pending \
                    and len(pending) < self.policy.max_urls:
                pending.append(target)
            return m.group(0)

        SCAN_RE.sub(collect, text)
        if pending:
            self._judge_pending(pending, seen, verdicts, deadline, seen_urls)

        def judge(url: str) -> LinkVerdict:
            if url in seen:
                return seen[url]
            v = LinkVerdict(url, UNKNOWN, "too many links in one reply to check")
            seen[url] = v
            verdicts.append(v)
            return v

        def replace(m: re.Match) -> str:
            nonlocal changed
            label, target, autolink, bare = m.group(1), m.group(2), m.group(3), m.group(4)
            if target is not None:
                v = judge(target)
                if v.verdict == OK:
                    if v.replaced_with and v.replaced_with != target:
                        changed = True
                        return f"[{label}]({v.replaced_with})"
                    return m.group(0)
                changed = True
                return strip_link(label, v)
            if autolink is not None:
                v = judge(autolink)
                if v.verdict == OK:
                    if v.replaced_with and v.replaced_with != autolink:
                        changed = True
                        return f"<{v.replaced_with}>"
                    return m.group(0)
                changed = True
                return note_for(v)
            if bare is not None:
                url = trim_url(bare)
                tail = bare[len(url):]
                v = judge(url)
                if v.verdict == OK:
                    if v.replaced_with and v.replaced_with != url:
                        changed = True
                        return v.replaced_with + tail
                    return m.group(0)
                changed = True
                return note_for(v) + tail
            return m.group(0)

        return SCAN_RE.sub(replace, text), changed


def interpret_route_verdict(answer: Any) -> Tuple[str, str]:
    """Turn a ``route_validator`` answer into ``(verdict, reason)``."""
    if isinstance(answer, tuple) and len(answer) == 2:
        verdict, reason = str(answer[0] or "").lower(), str(answer[1] or "")
    elif answer is None or answer is True:
        return OK, ""
    elif answer is False:
        verdict, reason = DEAD, ""
    else:
        verdict, reason = str(answer).lower(), ""
    if verdict not in (OK, DEAD, UNKNOWN):
        verdict = UNKNOWN
    if verdict != OK and not reason:
        reason = "not a page you can open" if verdict == DEAD else "could not check this page"
    return verdict, reason


def looks_like_url(s: str) -> bool:
    t = (s or "").strip().strip("<>")
    return bool(re.match(r"^(?:https?://|www\.|/|[a-zA-Z][\w+.-]*:)", t))


NOT_A_PAGE = "not a page"


def note_for_reason(reason: str) -> str:
    """A specific reader-facing note for the one dead reason that deserves its own words."""
    if NOT_A_PAGE in (reason or ""):
        return "(link removed: that address is not a web page)"
    return ""


def note_for(v: LinkVerdict) -> str:
    """The short, honest stand-in that replaces a link we could not stand behind."""
    if v.note:
        return v.note
    if v.verdict == DEAD:
        return "(link removed: that address does not exist)"
    return "(link removed: could not verify it)"


def strip_link(label: str, v: LinkVerdict) -> str:
    """Keep the author's words, drop the address. A label that IS the address goes too."""
    text = (label or "").strip()
    if not text or looks_like_url(text):
        return note_for(v)
    return f"{text} {note_for(v)}"


# ---------------------------------------------------------------------------------------------
# The network seam
# ---------------------------------------------------------------------------------------------

BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def http_probe(url: str, timeout: float) -> Tuple[Any, ...]:
    """HEAD a URL, falling back to GET, and report ``(status, detail)``. Never raises.

    A 401/403 answer carries a third item, ``{"content_type": ...}``, so the guard can tell a page
    (HTML) from an API error (JSON) without a second request.

    ``detail`` is "dns" or "refused" for the two failures that PROVE absence; anything else is a
    description the caller treats as unsettled.
    """
    for method in ("HEAD", "GET"):
        # Ask the way a browser does: the reader will click this link, and a single-page app
        # behind a proxy often serves its screens only to a request that accepts HTML (to
        # anything else the same path is the API's 404), so "*/*" struck real pages.
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": "quest-ai-runner link guard",
            "Accept": BROWSER_ACCEPT,
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(getattr(resp, "status", 0) or resp.getcode() or 0), method
        except urllib.error.HTTPError as e:
            code = int(e.code)
            # Plenty of servers reject HEAD with 405/501 while serving the page fine on GET. A 401
            # also retries as GET, because the guard needs the content type of a real answer to tell
            # a sign-in page from an API's JSON error.
            if method == "HEAD" and code in (400, 401, 403, 405, 501):
                continue
            if code in (401, 403):
                return code, f"HTTP {code}", {"content_type": e.headers.get("Content-Type", "") if e.headers else ""}
            return code, f"HTTP {code}"
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", None)
            if isinstance(reason, socket.gaierror):
                return None, "dns"
            if isinstance(reason, ConnectionRefusedError):
                return None, "refused"
            if isinstance(reason, socket.timeout):
                return None, "timeout"
            return None, str(reason or e)
        except socket.timeout:
            return None, "timeout"
        except Exception as e:  # noqa: BLE001 -- a probe must never break the reply it is guarding
            return None, f"{type(e).__name__}: {e}"
    return None, "no response"


def build_link_guard(policy_file: str = "", *, enabled: bool = True,
                     policy: Optional[LinkPolicy] = None) -> Optional[LinkGuard]:
    """Build the guard a consumer configured, or None when it is switched off.

    With no policy file the guard still runs: external URLs are checked over the network, and an
    internal path has no route table to be checked against, so it comes back unverified and is
    stripped. Configure ``routes``/``routes_file`` to let real in-app links through. A consumer that
    builds its policy in code passes ``policy`` (it wins over ``policy_file``).
    """
    if not enabled:
        return None
    if policy is None:
        policy = LinkPolicy.from_file(policy_file) if policy_file else None
    return LinkGuard(policy or LinkPolicy())
