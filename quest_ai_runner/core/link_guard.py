"""link_guard -- no reply leaves the brain carrying a link nobody checked.

The risk this manages (product owner, 2026-09-28): a model writes a URL that reads perfectly and
does not exist. It happened for real in Quest chat, where replies linked
``https://ai.batmanhq.duckdns.org/api/tasks/<id>``: a plausible, wrong address that answers 401
JSON and is not a page anyone can open. The reader taps it, gets nothing, and stops trusting every
other link in the thread.

So every terminal reply is run through ``LinkGuard.sanitize`` before it is emitted. Each link it
finds gets one of three verdicts:

* ``ok``      -- proven to exist. An external URL that answered (2xx/3xx, or 401/403, which prove
                 the endpoint is there and merely gated), a host on the trusted list, an in-app
                 pseudo-scheme the consumer declared, or an internal path that matches the host
                 app's REAL route table.
* ``dead``    -- proven not to exist: 404/410, a host that does not resolve, a refused connection,
                 or an internal path with no matching route.
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

GENERIC BY CONSTRUCTION: this module knows nothing about Quest. The route table, the app's own
origins, the trusted hosts, the in-app schemes and the rewrite rules all come from a consumer-
supplied ``LinkPolicy`` (in practice a JSON file named by ``QAR_LINK_POLICY_FILE``). With no policy
configured, external URLs are still checked over the network, in-app paths cannot be checked
against anything and therefore come back ``unknown``.
"""
from __future__ import annotations

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
from urllib.parse import urlsplit

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


# ---------------------------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------------------------

class LinkGuard:
    """Verify every link in a reply and strip the ones that do not check out.

    ``fetcher`` is the one network seam: a callable ``(url, timeout) -> (status, reason)`` where
    ``status`` is an int HTTP status or None when the request never got one. Tests pass a fake and
    never touch the network.
    """

    def __init__(self, policy: Optional[LinkPolicy] = None,
                 *, fetcher: Optional[Callable[[str, float], Tuple[Optional[int], str]]] = None):
        self.policy = policy or LinkPolicy()
        self.fetch = fetcher or http_probe
        self.routes = [route_to_regex(r) for r in self.policy.routes]
        self.rewrites = [(re.compile(m), r) for m, r in self.policy.rewrites]
        # url -> (verdict, reason, checked_at). Shared across turns for a long-lived process.
        self.cache: Dict[str, Tuple[str, str, float]] = {}

    # -- judging one link ----------------------------------------------------------------------

    def apply_rewrites(self, url: str) -> str:
        for rx, repl in self.rewrites:
            if rx.search(url):
                return rx.sub(repl, url)
        return url

    def verify(self, url: str) -> LinkVerdict:
        """Judge one URL. Re-checks an ``unknown`` up to ``policy.passes`` times before settling."""
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
            if host in self.policy.trusted_hosts:
                return LinkVerdict(url, OK, "trusted host", replaced)
            if not self.policy.check_external:
                return LinkVerdict(url, UNKNOWN, "external checking is off", replaced)
            verdict, reason = self.check_external(target)
            return LinkVerdict(url, verdict, reason, replaced)
        if target.lower().startswith("www."):
            if not self.policy.check_external:
                return LinkVerdict(url, UNKNOWN, "external checking is off", replaced)
            verdict, reason = self.check_external("https://" + target)
            return LinkVerdict(url, verdict, reason, replaced)

        # Anything else (a bare word in link position, an unknown scheme) is not something we can
        # stand behind.
        return LinkVerdict(url, UNKNOWN, "unrecognized link target", replaced)

    def verify_path(self, path: str, original: str, replaced: Optional[str]) -> LinkVerdict:
        """Match an in-app path against the host app's real route table."""
        if not self.routes:
            return LinkVerdict(original, UNKNOWN, "no route table configured", replaced)
        bare = path.split("#", 1)[0].split("?", 1)[0] or "/"
        for rx in self.routes:
            if rx.match(bare):
                return LinkVerdict(original, OK, "matches an app route", replaced)
        return LinkVerdict(original, DEAD, "no such screen in this app", replaced)

    def check_external(self, url: str) -> Tuple[str, str]:
        """Fetch a URL (cached, multi-pass) and turn the outcome into a verdict."""
        hit = self.cache.get(url)
        now = time.time()
        if hit and (now - hit[2]) < self.policy.cache_ttl:
            return hit[0], hit[1]
        verdict, reason = UNKNOWN, "no response"
        for attempt in range(self.policy.passes):
            status, detail = self.fetch(url, self.policy.timeout)
            if status is not None and 200 <= status < 400:
                verdict, reason = OK, f"HTTP {status}"
                break
            if status in (401, 403):
                # The endpoint is there, it just will not serve US. That proves existence, which is
                # the only claim a link makes.
                verdict, reason = OK, f"HTTP {status} (exists, access controlled)"
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

    def sanitize(self, text: str) -> Tuple[str, List[LinkVerdict]]:
        """Return ``(safe_text, verdicts)``. Repeats until a scan finds nothing left to strip."""
        if not text or not text.strip():
            return text, []
        verdicts: List[LinkVerdict] = []
        seen: Dict[str, LinkVerdict] = {}
        current = text
        for _round in range(self.policy.passes):
            current, changed = self.sanitize_once(current, seen, verdicts)
            if not changed:
                break
        return current, verdicts

    def sanitize_once(self, text: str, seen: Dict[str, LinkVerdict],
                      verdicts: List[LinkVerdict]) -> Tuple[str, bool]:
        """One scan-and-rewrite pass. ``changed`` says whether anything was stripped or rewritten."""
        changed = False
        checked = 0

        def judge(url: str) -> LinkVerdict:
            nonlocal checked
            if url in seen:
                return seen[url]
            checked += 1
            if checked > self.policy.max_urls:
                v = LinkVerdict(url, UNKNOWN, "too many links in one reply to check")
            else:
                v = self.verify(url)
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


def looks_like_url(s: str) -> bool:
    t = (s or "").strip().strip("<>")
    return bool(re.match(r"^(?:https?://|www\.|/|[a-zA-Z][\w+.-]*:)", t))


def note_for(v: LinkVerdict) -> str:
    """The short, honest stand-in that replaces a link we could not stand behind."""
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

def http_probe(url: str, timeout: float) -> Tuple[Optional[int], str]:
    """HEAD a URL, falling back to GET, and report ``(status, detail)``. Never raises.

    ``detail`` is "dns" or "refused" for the two failures that PROVE absence; anything else is a
    description the caller treats as unsettled.
    """
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers={
            "User-Agent": "quest-ai-runner link guard",
            "Accept": "*/*",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(getattr(resp, "status", 0) or resp.getcode() or 0), method
        except urllib.error.HTTPError as e:
            code = int(e.code)
            # Plenty of servers reject HEAD with 405/501 while serving the page fine on GET.
            if method == "HEAD" and code in (400, 403, 405, 501):
                continue
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


def build_link_guard(policy_file: str = "", *, enabled: bool = True) -> Optional[LinkGuard]:
    """Build the guard a consumer configured, or None when it is switched off.

    With no policy file the guard still runs: external URLs are checked over the network, and an
    internal path has no route table to be checked against, so it comes back unverified and is
    stripped. Configure ``routes``/``routes_file`` to let real in-app links through.
    """
    if not enabled:
        return None
    policy = LinkPolicy.from_file(policy_file) if policy_file else None
    return LinkGuard(policy or LinkPolicy())
