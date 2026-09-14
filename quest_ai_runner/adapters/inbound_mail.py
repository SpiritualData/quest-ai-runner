"""InboundMail -- read what arrived at a mailbox, so a real person's email can become an ask.

Joshua's own framing of what this exists for: "Everything should make it into quests, even if
from different sources, they can be tracked as asks, even stuff from external people who send to
support, but should be real people not marketing emails etc, and an LLM will have to make that
distinction." A support inbox, a contact-form mailbox, a grants inbox -- any address a deployment
already reads by hand -- is a context source exactly like a Drive comment or a quest note, and
belongs in the SAME engine (``runner/context_updates.py``) rather than in a bespoke poller a
consumer writes for itself.

TWO GATES, IN ORDER, BEFORE ANYTHING REACHES A RUN OR THE ACCOUNT'S OWN ASKS RECORD:

1. A DETERMINISTIC HEADER CHECK (``is_bulk_mail``, this module), for free, before any model call.
   ``List-Unsubscribe``, ``Precedence: bulk``, ``Auto-Submitted`` and the handful of headers a
   mail SYSTEM sets about its own automated nature identify most marketing and machine mail
   outright. Spending a model call to prove a newsletter is a newsletter is waste, and it is also
   the wrong KIND of check: header checks only, never a keyword or subject-line rule, which is
   this repo's hard rule #3 -- a fixed string list silently misses whatever wording it did not
   anticipate, while a header a system sets about itself is either present, honestly, or absent.
   This is the same shape of check quest-backend's own ``quest_inbox_service.is_automated`` already
   uses to keep vacation responders and list traffic out of its notes; restated here rather than
   imported, because this library has no dependency on any particular consumer's backend package.

2. AN LLM ADMISSION JUDGMENT (``runner.context_updates.llm_admission_judge``), for what survives
   the header check. A message with none of those headers is not automatically a person: it is
   just mail that did not IDENTIFY itself as bulk. "Is this a real person asking for something" is
   a judgment this module hands to the engine rather than making itself, for the same reason
   ``runner.insights`` refuses to match tags against quest names -- see that module's own
   docstring and this repo's hard rule #3.

AUTH mirrors ``adapters.drive_comments``/``config.drive_comments_auth`` deliberately: a service
account with Workspace domain-wide delegation, impersonating a mailbox through
``service_account_token_provider`` (``adapters.google_chat_adapter``), read-only scope
(``INBOUND_MAIL_READ_SCOPES``). The one place this credential differs from Drive's is that a
DIFFERENT mailbox needs a DIFFERENT impersonated subject -- Drive's one subject can hold many
folders, but a Gmail mailbox and its owning identity are the same thing -- so ``InboundMail``
mints one token provider PER mailbox it is asked to read, lazily, and ``allowed_addresses`` is the
only code-side limit on which mailboxes that minting is allowed to reach: domain-wide delegation
grants the key the ABILITY to impersonate any address in the domain, and this allowlist is what
keeps a typo'd or malicious ``mailbox`` spec from reading someone else's inbox.

WHAT THIS DELIBERATELY DOES NOT DO, mirroring ``quest_inbox_service`` again:
* It does not trust ``From`` for anything but display -- the mailbox itself is what a card's spec
  names, and reading it is already gated by ``allowed_addresses``.
* It does not mark anything read or delete anything: ``gmail.readonly`` cannot, and the caller's
  own watermark is what says "already offered", not mailbox state.
* It never reaches into ``ai@`` or any address that parses as a quest's own reply address (see
  ``refuses_as_inbound_mailbox``). That mailbox is already polled by a consumer's own inbound-reply
  service; a second poller on the same mailbox ingests every reply twice.

HTTP is stdlib-only (``urllib.request`` + ``json``), matching ``drive_comments``'s own choice:
typed rows and honest failure reporting are worth more here than the convenience of the Google API
client library, and every read degrades to an empty list on failure -- a mailbox with nothing new
is the normal case, not an error.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from html import unescape
from typing import Any, Callable, Dict, List, Optional, Sequence

log = logging.getLogger("quest-ai-runner.inbound-mail")

_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

# Enough to LIST and READ messages. Nothing here ever needs to send, label, or delete mail.
INBOUND_MAIL_READ_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
)

# Same shape as ``drive_comments.TokenProvider``: a callable returning a bearer token, or None.
TokenProvider = Callable[[], Optional[str]]

# Fan-out bounds. A mailbox with a burst of mail (a campaign someone replied to, a mailing list
# leak) must cost one bounded pass, not the poll.
_PAGE_SIZE = 100
_MAX_PAGES = 3

# The local-part(s) a consumer's OWN reply-mail service is conventionally addressed at (see
# quest-backend's ``app/utils/quest_reply_address.py``, ``DEFAULT_MAILBOX = "ai"``). This module
# has no dependency on that package -- a public library must not assume any particular consumer's
# backend is even installed -- so the SHAPE of the rule is restated rather than imported: a bare
# "ai@..." address and any "ai+...@..." tag on it are refused as an inbound-mail mailbox outright.
_RESERVED_REPLY_LOCAL_PARTS = frozenset({"ai"})


def refuses_as_inbound_mailbox(address: str) -> Optional[str]:
    """Why ``address`` must never be configured as this source's mailbox, or None when it is fine.

    A quest's own reply mailbox (``ai@...``, ``ai+q-...@...``) is already polled by the consumer's
    own inbound-reply service, continuously, on its own schedule -- quest-backend's
    ``quest_inbox_service`` module docstring is explicit that two environments polling ONE mailbox
    both ingest every reply. A second poller reading the same address here would not be a second
    source, it would be the same mail arriving twice: once as this source's own capture, once as
    the note the reply service already wrote. Refused outright rather than left to a deployment to
    avoid by convention, because the failure mode of getting this wrong is a duplicated ask, not a
    missed one.
    """
    local, _, domain = (address or "").strip().lower().partition("@")
    if not local or not domain:
        return f"{address!r} is not a usable mailbox address"
    reserved = local.split("+", 1)[0]
    if reserved in _RESERVED_REPLY_LOCAL_PARTS:
        return (f"{address!r} looks like a quest reply address (local part {reserved!r} is the "
                f"conventional inbound-reply mailbox); it may already be polled by a consumer's "
                f"own reply service, and a second poller on the same mailbox double-ingests every "
                f"reply")
    return None


# Standard headers a mail SYSTEM sets about its own automated/bulk nature. Header checks only --
# see the module docstring and this repo's hard rule #3 on why a keyword/subject rule is refused.
_AUTO_SUBMITTED_HEADER = "auto-submitted"
_OTHER_AUTOMATED_HEADERS = ("x-autoreply", "x-autorespond", "x-auto-response-suppress")
_BULK_PRECEDENCE_VALUES = frozenset({"bulk", "auto_reply", "junk", "list"})


def is_bulk_mail(headers: Dict[str, str]) -> bool:
    """Does this message identify itself as bulk or automated mail, from its OWN headers?

    Every check here is a header a sending system sets about itself, never a guess about content:
    ``Auto-Submitted`` (RFC 3834, vacation responders and the like), ``X-Autoreply`` /
    ``X-Autorespond`` / ``X-Auto-Response-Suppress`` (the same idea, older conventions),
    ``Precedence: bulk`` (mailing-list software), and ``List-Unsubscribe`` / ``List-Id`` (present
    on essentially all commercial marketing mail, since CAN-SPAM/CASL/GDPR compliance requires an
    unsubscribe path). A message with none of these is not thereby proven human -- it is only mail
    that did not identify itself as bulk -- which is exactly the boundary the admission judge picks
    up from here (see ``runner.context_updates.llm_admission_judge``).
    """
    headers = headers or {}
    auto_submitted = (headers.get(_AUTO_SUBMITTED_HEADER) or "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if any(headers.get(h) for h in _OTHER_AUTOMATED_HEADERS):
        return True
    if (headers.get("precedence") or "").strip().lower() in _BULK_PRECEDENCE_VALUES:
        return True
    return bool(headers.get("list-unsubscribe") or headers.get("list-id"))


@dataclass
class MailMessage:
    """One message as read from a mailbox, with the headers the deterministic filter needs.

    ``headers`` is every header, lowercased, first occurrence wins -- kept whole (not just the
    handful ``is_bulk_mail`` reads) so a future check never has to widen what this module fetches.
    """
    message_id: str = ""
    thread_id: str = ""
    sender_email: str = ""
    sender_name: str = ""
    subject: str = ""
    body_text: str = ""
    received_at: Optional[datetime] = None
    headers: Dict[str, str] = field(default_factory=dict)
    url: str = ""


def _epoch_ms_to_utc(raw: Any) -> Optional[datetime]:
    try:
        ms = int(raw)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _decode_body(payload: Dict[str, Any]) -> str:
    """The message's plain-text body, falling back to HTML stripped of tags.

    Mirrors ``quest_inbox_service.decode_body``'s approach (walk every MIME part, prefer
    text/plain, fall back to a de-tagged text/html) without importing it: same reasoning as
    ``is_bulk_mail`` above.
    """
    def walk(part: Dict[str, Any]) -> List[Dict[str, Any]]:
        parts = [part]
        for child in (part or {}).get("parts") or []:
            parts.extend(walk(child))
        return parts

    text, html_body = "", ""
    for part in walk(payload or {}):
        data = ((part.get("body") or {}).get("data") or "")
        if not data:
            continue
        try:
            decoded = base64.urlsafe_b64decode(
                data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 -- a malformed part never breaks the message
            continue
        mime = (part.get("mimeType") or "").lower()
        if mime == "text/plain" and not text:
            text = decoded
        elif mime == "text/html" and not html_body:
            html_body = decoded
    if text.strip():
        return text.strip()
    if html_body.strip():
        stripped = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_body)
        return unescape(re.sub(r"<[^>]+>", " ", stripped)).strip()
    return ""


class InboundMail:
    """Read a mailbox through an injected service-account credential.

    One token provider is minted PER MAILBOX, lazily and cached, because impersonating a
    different Gmail address means authenticating as a different subject -- unlike
    ``DriveComments``, where one subject's Drive can hold many folders. ``allowed_addresses`` is
    checked before ANY token is minted: an empty allowlist means this credential is not configured
    to read anything (fail closed), never "read whatever is asked of it".
    """

    def __init__(self, *, service_account_file: Optional[str] = None,
                service_account_info: Optional[Dict[str, Any]] = None,
                scopes: Optional[Sequence[str]] = None,
                allowed_addresses: Sequence[str] = (),
                timeout: float = 20.0) -> None:
        self._service_account_file = service_account_file
        self._service_account_info = service_account_info
        self._scopes = list(scopes) if scopes else list(INBOUND_MAIL_READ_SCOPES)
        self._allowed = frozenset(
            str(a).strip().lower() for a in (allowed_addresses or ()) if str(a).strip())
        self._timeout = timeout
        self._providers: Dict[str, TokenProvider] = {}

    # --- auth ------------------------------------------------------------------------------

    def _token_for(self, mailbox: str) -> Optional[str]:
        provider = self._providers.get(mailbox)
        if provider is None:
            try:
                from .google_chat_adapter import service_account_token_provider
            except Exception as e:  # noqa: BLE001 -- an optional dependency never crashes a read
                log.warning("inbound mail: token provider unavailable (%s)", e)
                return None
            provider = service_account_token_provider(
                service_account_file=self._service_account_file,
                service_account_info=self._service_account_info,
                subject=mailbox, scopes=self._scopes)
            self._providers[mailbox] = provider
        try:
            return provider()
        except Exception as e:  # noqa: BLE001 -- a broken provider degrades to "no access"
            log.warning("inbound mail: token provider failed for %s: %s", mailbox, e)
            return None

    # --- HTTP ------------------------------------------------------------------------------

    def _url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        url = f"{_GMAIL_API_BASE}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    def _request(self, method: str, url: str, token: str) -> Dict[str, Any]:
        req = urllib.request.Request(url, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    @staticmethod
    def _http_error_text(e: urllib.error.HTTPError) -> str:
        try:
            payload = json.loads(e.read().decode("utf-8"))
            msg = ((payload.get("error") or {}).get("message") or "").strip()
            if msg:
                return f"HTTP {e.code}: {msg}"
        except Exception:  # noqa: BLE001 -- the body is optional and often absent
            pass
        return f"HTTP {e.code}"

    # --- reads -----------------------------------------------------------------------------

    def messages_since(self, mailbox: str, *, since: Optional[datetime] = None,
                       max_messages: int = 25) -> List[MailMessage]:
        """Every message that reached ``mailbox`` since ``since``, newest first, or [] on failure.

        Raises ``ValueError`` when ``mailbox`` is not on ``allowed_addresses`` -- this is a
        configuration mistake worth surfacing loudly (the caller reports it as a failed source,
        never swallows it), unlike an ordinary read failure, which degrades to an empty list the
        same as every other best-effort source in this library.
        """
        mailbox = (mailbox or "").strip().lower()
        if self._allowed and mailbox not in self._allowed:
            raise ValueError(
                f"{mailbox!r} is not in this credential's allowed_addresses; domain-wide "
                f"delegation can impersonate any mailbox in the domain, so this allowlist is the "
                f"only code-side limit on which one actually gets read")
        token = self._token_for(mailbox)
        if not token:
            return []
        query = ""
        if since is not None:
            # Gmail's "after:" query operator is DAY-granular in the mailbox's own timezone, so
            # the query widens by a day and the exact cut is enforced below on internalDate --
            # trusting the query alone would silently drop a message that arrived earlier the
            # same day the watermark happens to fall on.
            margin = since - timedelta(days=1)
            query = f"after:{int(margin.timestamp())}"
        ids: List[str] = []
        page_token = None
        cap = max(1, min(int(max_messages or 25), 500))
        for _ in range(_MAX_PAGES):
            params: Dict[str, Any] = {"maxResults": min(_PAGE_SIZE, cap)}
            if query:
                params["q"] = query
            if page_token:
                params["pageToken"] = page_token
            try:
                payload = self._request(
                    "GET", self._url("/users/me/messages", params), token)
            except urllib.error.HTTPError as e:
                log.warning("inbound mail: list failed for %s: %s",
                            mailbox, self._http_error_text(e))
                return []
            except Exception as e:  # noqa: BLE001 -- transport failure degrades to what we have
                log.warning("inbound mail: list failed for %s: %s", mailbox, e)
                return []
            ids.extend(str(m.get("id")) for m in payload.get("messages") or [] if m.get("id"))
            page_token = payload.get("nextPageToken")
            if not page_token or len(ids) >= cap:
                break
        out: List[MailMessage] = []
        for mid in ids[:cap]:
            try:
                raw = self._request(
                    "GET", self._url(f"/users/me/messages/{mid}", {"format": "full"}), token)
            except urllib.error.HTTPError as e:
                log.warning("inbound mail: read failed for %s: %s", mid, self._http_error_text(e))
                continue
            except Exception as e:  # noqa: BLE001
                log.warning("inbound mail: read failed for %s: %s", mid, e)
                continue
            msg = self._parse_message(raw)
            if msg is None:
                continue
            if since is not None and msg.received_at and msg.received_at <= since:
                continue  # the query's day-wide margin over-fetched; enforce the real cut here
            out.append(msg)
        out.sort(key=lambda m: m.received_at or datetime.min.replace(tzinfo=timezone.utc),
                 reverse=True)
        return out

    @staticmethod
    def _parse_message(raw: Dict[str, Any]) -> Optional[MailMessage]:
        message_id = str(raw.get("id") or "")
        if not message_id:
            return None
        payload = raw.get("payload") or {}
        headers: Dict[str, str] = {}
        for h in payload.get("headers") or []:
            name = str(h.get("name") or "").strip().lower()
            if name and name not in headers:
                headers[name] = h.get("value") or ""
        sender_name, sender_email = parseaddr(headers.get("from", ""))
        return MailMessage(
            message_id=message_id,
            thread_id=str(raw.get("threadId") or ""),
            sender_email=(sender_email or "").strip().lower(),
            sender_name=(sender_name or "").strip(),
            subject=headers.get("subject", ""),
            body_text=_decode_body(payload),
            received_at=_epoch_ms_to_utc(raw.get("internalDate")),
            headers=headers,
            url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
        )
