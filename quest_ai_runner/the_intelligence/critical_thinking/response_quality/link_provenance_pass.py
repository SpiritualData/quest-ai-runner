"""A deterministic pass that removes any link or file address from a reply that the turn never held.

A model writes addresses that read perfectly and exist nowhere: a plausible URL, a ``file://`` path, a relative path made into a
markdown link. The rule is a data check, not a wording check: an address stays only if it appears in the material the turn had
(the person's message, the context shown to the model, what the turn read) or sits on a host the consumer declared trusted.
Everything else is replaced by its label (a markdown link) or dropped (a bare address), so the words around it still read well.

It reads the reply's addresses to compare them with that data; it decides nothing from the model's phrasing (hard rule #3).
In-app routes that start with ``/``, ``#``, ``mailto:``, ``tel:`` and consumer-declared schemes are not judged here: the link
guard's route table owns them.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Sequence, Tuple
from urllib.parse import urlsplit

from hubofhubs.api import normalize_url_for_match

MARKDOWN_OR_BARE = re.compile(
    r"\[(?P<label>[^\]\n]*)\]\(\s*<?(?P<target>[^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)"
    r"|(?P<bare>(?:https?://|file://|www\.)[^\s<>()\[\]\"'`]+)",
    re.IGNORECASE)
TRAILING_PUNCTUATION = ".,;:!?"
EVIDENCE_TOKEN = re.compile(r"[^\s<>()\[\]\"'`]+")
JUDGED_SCHEMES = ("http", "https", "file")


def trim_address(address: str) -> str:
    return address.rstrip(TRAILING_PUNCTUATION)


def seen_addresses(evidence: str) -> frozenset:
    """Every whitespace-delimited token of the turn's material, normalised the way addresses are compared."""
    return frozenset(normalize_url_for_match(trim_address(m.group(0))) for m in EVIDENCE_TOKEN.finditer(evidence or ""))


def is_judged(target: str) -> bool:
    """True for an address this pass judges: an http(s) or file address, or a scheme-less relative path (a file address)."""
    if target.startswith(("/", "#")):
        return False
    scheme = target.split(":", 1)[0].lower() if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", target) else ""
    if scheme:
        return scheme in JUDGED_SCHEMES
    return True


def host_of(address: str) -> str:
    raw = address if "://" in address else "http://" + address
    return (urlsplit(raw).hostname or "").lower().removeprefix("www.")


def normalise_hosts(hosts: Iterable[str]) -> frozenset:
    return frozenset(h.lower().removeprefix("www.") for h in hosts if h)


def strip_unseen_links(text: str, evidence: str, trusted_hosts: Sequence[str] = ()) -> Tuple[str, List[str]]:
    """``text`` without any address that is neither in ``evidence`` nor on a trusted host, and the addresses removed."""
    if not text or not isinstance(text, str):
        return text, []
    seen, trusted, removed = seen_addresses(evidence), normalise_hosts(trusted_hosts), []

    def keep(address: str) -> bool:
        if normalize_url_for_match(address) in seen:
            return True
        return "://" in address and host_of(address) in trusted

    def judge(match: "re.Match[str]") -> str:
        bare = match.group("bare")
        if bare is not None:
            address = trim_address(bare)
            if keep(address):
                return match.group(0)
            removed.append(address)
            return match.group(0)[len(address):]
        target = match.group("target")
        if not is_judged(target):
            return match.group(0)
        relative = "://" not in target and not target.lower().startswith("www.")
        held = keep(trim_address(target))
        if held and not relative:
            return match.group(0)
        if not held:
            removed.append(target)
        label = match.group("label").strip()
        if relative:
            # A relative file path is not an address a reader can open, so it is written as plain words: the file name stays
            # when the turn held it, and nothing is claimed to be clickable.
            return label or (target if held else "")
        return "" if normalize_url_for_match(label) == normalize_url_for_match(target) else label

    out = MARKDOWN_OR_BARE.sub(judge, text)
    if removed:
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    return out, removed


def failed_page_reads(gathered: Iterable) -> frozenset:
    """Normalised addresses whose web page read came back as an error observation (structure: kind and rel_path, no text read)."""
    return frozenset(normalize_url_for_match(trim_address(str(o.get("rel_path"))))
                     for o in (gathered or []) if isinstance(o, dict) and o.get("kind") == "error"
                     and str(o.get("rel_path") or "").lower().startswith(("http://", "https://")))


def opened_web_pages(gathered: Iterable) -> bool:
    """True when the turn holds at least one web page read that succeeded (a read observation whose rel_path is an http(s) address)."""
    return any(isinstance(o, dict) and o.get("kind") == "read"
               and str(o.get("rel_path") or "").lower().startswith(("http://", "https://")) for o in (gathered or []))


def unopened_pages_note(addresses: Sequence[str]) -> str:
    """One honest sentence naming the sites whose page could not be opened (domains only, so the link guard has nothing to strip)."""
    hosts = sorted({host_of(a) for a in addresses if host_of(a)})
    where = ", ".join(hosts) if hosts else "the page you asked for"
    return f"I could not open the page at {where} from here, so I have not confirmed it myself."


def http_addresses(text: str) -> frozenset:
    """Normalised http(s) addresses in a reply, markdown links and bare ones alike (the shape the link guard judges)."""
    found = set()
    for m in MARKDOWN_OR_BARE.finditer(text or ""):
        address = trim_address(m.group("target") if m.group("target") is not None else m.group("bare"))
        if address.lower().startswith(("http://", "https://")):
            found.add(normalize_url_for_match(address))
    return frozenset(found)
