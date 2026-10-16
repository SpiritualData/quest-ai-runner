"""google_link_reader: read ONE pasted Google Drive / Docs / Sheets / Slides link, read-only, for the
file server (``serve-files``, operation ``link``).

Why it exists. A brain that runs on another machine (a hosted Quest) cannot open a private Google
file a person pastes into a chat: the public export URL answers with a sign-in page. The machine
that runs ``serve-files`` can hold the Google credentials instead, so the brain asks it. Nothing
here knows who runs the server or which Workspace it is.

Access rules (the point of the module, each enforced in one place and tested):

  * READ-ONLY IN CODE. Every request goes through ``ReadOnlyGoogleClient.get``, which issues only
    HTTP GET and only to an allowlist of Drive v3 read paths (``files.get``, ``files.list``,
    ``files.export``) and the three Docs-family ``export`` URLs. There is no create, update,
    permissions or delete path to call, whatever scope the credential carries (impersonation needs
    the full Drive scope; this allowlist is what keeps it read-only).
  * PEOPLE SEE WHAT THEY CAN SEE. When the request names ``as_user`` and that address is in an
    allowed impersonation domain, the file is read AS that person (domain-wide delegation). A 403 or
    404 then means "not shared with you" and is reported as such. The service account is NOT tried
    for that person afterwards (it would show them everything the service account can see), unless
    the operator sets ``QAR_FILES_GOOGLE_SA_FALLBACK=1``.
  * A request with no usable ``as_user`` (absent, malformed, or outside the allowed domains) is
    refused, unless the operator set ``QAR_FILES_GOOGLE_SA_FALLBACK=1``, in which case the service
    account reads it.
  * Output is bounded (file size, text size), redacted for secret shapes like every other file the
    server returns, and returned as data; the client frames it as untrusted content.

Configuration (environment, all optional; with no credentials the capability is simply off):

  * ``QAR_FILES_GOOGLE_CREDENTIALS``         path to a service account JSON key
  * ``QAR_FILES_GOOGLE_IMPERSONATE_DOMAINS`` comma separated domains people may be impersonated in
  * ``QAR_FILES_GOOGLE_IMPERSONATE_SCOPE``   scope for impersonation (default: full Drive scope,
                                             the only one domain-wide delegation grants in most
                                             Workspaces)
  * ``QAR_FILES_GOOGLE_SA_SCOPE``            scope for direct service account reads (default
                                             ``drive.readonly``)
  * ``QAR_FILES_GOOGLE_SA_FALLBACK``         ``1`` lets the service account read for people who
                                             cannot be impersonated or have no access

Needs the optional ``google-auth`` and ``requests`` packages (the ``[google]`` extra) to mint tokens.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .files_redaction import redact
from .web_link_access import (
    REASON_NOT_FOUND,
    REASON_NOT_SHARED,
    REASON_TOO_LARGE,
    REASON_UNREACHABLE,
    REASON_UNSUPPORTED_TYPE,
)

log = logging.getLogger("quest-ai-runner.google_link_reader")

#: Reasons only this module speaks (the others come from ``web_link_access``).
REASON_UNAVAILABLE = "unavailable"          # this server cannot read Google links at all
REASON_NOT_ALLOWED = "not_allowed"          # the request is refused by policy (no usable user)

SCOPE_DRIVE_FULL = "https://www.googleapis.com/auth/drive"
SCOPE_DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"

API_HOST = "www.googleapis.com"
DOCS_HOST = "docs.google.com"
REQUEST_TIMEOUT_SECONDS = 25.0
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024       # binary file download cap before extraction
MAX_EXPORT_BYTES = 12 * 1024 * 1024         # Google's own export ceiling is 10 MB
MAX_TEXT_CHARS = 1_500_000                  # text kept per file; the answer says when it was cut
MAX_FOLDER_ENTRIES = 200
DEFAULT_FOCUS_TOKENS = 3000
DEFAULT_FULL_TOKENS = 12000
CHARS_PER_TOKEN = 4
CACHE_TTL_SECONDS = 120.0                   # a paged read re-asks within seconds; stay near-live
CACHE_MAX_ENTRIES = 16

MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_SLIDES = "application/vnd.google-apps.presentation"
MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_FORM = "application/vnd.google-apps.form"
MIME_SHORTCUT = "application/vnd.google-apps.shortcut"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

KIND_DOC = "google_doc"
KIND_SHEET = "google_sheet"
KIND_SLIDES = "google_slides"
KIND_FORM = "google_form"
KIND_FOLDER = "drive_folder"
KIND_FILE = "drive_file"

GOOGLE_HOSTS = frozenset({"docs.google.com", "drive.google.com", "sheets.google.com", "slides.google.com"})
ID_PATTERN = r"[A-Za-z0-9_-]{10,}"
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]{1,64}@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)$")


# --------------------------------------------------------------------------- URL parsing


@dataclass(frozen=True)
class GoogleRef:
    """What a pasted link points at: its kind, the file id (folder id for a folder) and, for a
    sheet, the tab's numeric gid when the link names one."""
    kind: str
    file_id: str
    gid: Optional[str] = None


_DOCS_PATH = re.compile(
    r"^/(?:a/[^/]+/)?(document|spreadsheets|presentation|forms)/(?:u/\d+/)?d/(?:e/)?(" + ID_PATTERN + ")")
_DRIVE_FILE_PATH = re.compile(r"^/(?:u/\d+/)?file/d/(" + ID_PATTERN + ")")
_DRIVE_FOLDER_PATH = re.compile(r"^/(?:u/\d+/)?(?:drive/(?:u/\d+/)?)?folders/(" + ID_PATTERN + ")")
_GID = re.compile(r"(?:^|[#&?])gid=(\d+)")
_KINDS = {"document": KIND_DOC, "spreadsheets": KIND_SHEET, "presentation": KIND_SLIDES, "forms": KIND_FORM}


def parse_google_link(url: str) -> Optional[GoogleRef]:
    """The Google file a link names, or ``None`` for anything that is not a Docs, Sheets, Slides,
    Forms or Drive link. Never raises, never makes a network call."""
    try:
        parts = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or host not in GOOGLE_HOSTS:
        return None
    path = parts.path or ""
    if host in ("docs.google.com", "sheets.google.com", "slides.google.com"):
        match = _DOCS_PATH.match(path)
        if match:
            kind = _KINDS[match.group(1)]
            gid = None
            if kind == KIND_SHEET:
                found = _GID.search(parts.query) or _GID.search(parts.fragment)
                gid = found.group(1) if found else None
            return GoogleRef(kind, match.group(2), gid)
        return None
    match = _DRIVE_FOLDER_PATH.match(path)
    if match:
        return GoogleRef(KIND_FOLDER, match.group(1))
    match = _DRIVE_FILE_PATH.match(path)
    if match:
        return GoogleRef(KIND_FILE, match.group(1))
    if path in ("/open", "/uc", "/folderview"):
        ids = urllib.parse.parse_qs(parts.query).get("id") or []
        if ids and re.fullmatch(ID_PATTERN, ids[0]):
            return GoogleRef(KIND_FOLDER if path == "/folderview" else KIND_FILE, ids[0])
    return None


# --------------------------------------------------------------------------- errors and results


class GoogleReadError(Exception):
    """A link could not be read. ``reason`` is one of the ``web_link_access`` reasons or
    ``REASON_UNAVAILABLE`` / ``REASON_NOT_ALLOWED``; ``message`` is the sentence the person is told."""

    def __init__(self, reason: str, message: str, detail: str = "") -> None:
        self.reason = reason
        self.message = message
        self.detail = detail
        super().__init__(f"{reason}: {detail or message}")


@dataclass
class LinkText:
    title: str
    text: str
    kind: str
    cut: bool = False


NOT_SHARED_WITH_USER = (
    "This file is not shared with your account, so I cannot open it for you. Ask the owner to share "
    "it with you, or paste the text here.")
NOT_SHARED_SERVICE = (
    "I could not open this file with the access this server has. Ask the owner to share it, or paste "
    "the text here.")
NOT_FOUND_MESSAGE = (
    "I could not find this file. The link may be incomplete, or the file may have been deleted.")
NO_USER_MESSAGE = (
    "I can only open a private Google file as the person who asked, and I do not have a verified "
    "account for this request, so I cannot open it. Paste the text here instead.")
UNAVAILABLE_MESSAGE = "This server is not set up to open private Google links."
FORM_MESSAGE = ("This is a Google Form, which I cannot open or read. Share the questions or the "
                "responses as a Doc, Sheet or PDF.")
IMPERSONATION_FAILED = (
    "I could not open Google as your account, so I cannot open this file for you. Paste the text "
    "here instead.")


# --------------------------------------------------------------------------- tokens


TokenSource = Callable[[Optional[str], str], str]
"""``(subject or None, scope) -> bearer token``. Raises ``GoogleReadError`` when none can be made."""


class ServiceAccountTokens:
    """Bearer tokens from one service account key, per (subject, scope), cached and refreshed by
    google-auth. ``subject`` None is the service account itself."""

    def __init__(self, key_path: str) -> None:
        self.key_path = key_path
        self.credentials: Dict[Tuple[Optional[str], str], Any] = {}
        self.lock = threading.Lock()

    def __call__(self, subject: Optional[str], scope: str) -> str:
        with self.lock:
            try:
                from google.oauth2 import service_account
                from google.auth.transport.requests import Request
            except ImportError as exc:
                raise GoogleReadError(
                    REASON_UNAVAILABLE, UNAVAILABLE_MESSAGE, "google-auth is not installed") from exc
            key = (subject, scope)
            creds = self.credentials.get(key)
            try:
                if creds is None:
                    creds = service_account.Credentials.from_service_account_file(
                        self.key_path, scopes=[scope])
                    if subject:
                        creds = creds.with_subject(subject)
                    self.credentials[key] = creds
                if not creds.valid:
                    creds.refresh(Request())
                return str(creds.token)
            except GoogleReadError:
                raise
            except Exception as exc:  # noqa: BLE001 - google-auth raises many types
                self.credentials.pop(key, None)
                if subject:
                    raise GoogleReadError(REASON_NOT_SHARED, IMPERSONATION_FAILED,
                                          f"token for subject failed: {type(exc).__name__}") from exc
                raise GoogleReadError(REASON_UNAVAILABLE, UNAVAILABLE_MESSAGE,
                                      f"token failed: {type(exc).__name__}") from exc


# --------------------------------------------------------------------------- the read-only client


Transport = Callable[[str, Dict[str, str], float, int], Tuple[int, bytes, bool]]
"""``(url, headers, timeout, max_bytes) -> (status, body, truncated)``. Always a GET."""


def urllib_get(url: str, headers: Dict[str, str], timeout: float, max_bytes: int) -> Tuple[int, bytes, bool]:
    request = urllib.request.Request(url, headers=headers, method="GET")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: D102
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
            return response.status, body[:max_bytes], len(body) > max_bytes
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(64 * 1024), False


_API_PATHS = (
    re.compile(r"^/drive/v3/files$"),
    re.compile(r"^/drive/v3/files/[A-Za-z0-9_-]+$"),
    re.compile(r"^/drive/v3/files/[A-Za-z0-9_-]+/export$"),
)
_DOCS_EXPORT_PATH = re.compile(r"^/(?:document|spreadsheets|presentation)/d/[A-Za-z0-9_-]+/export$")


def is_allowed_read_url(url: str, *, include_media: bool = True) -> bool:
    """Whether ``url`` is one of the read-only calls this module may make. The single gate every
    request passes: https only, one of two Google hosts, one of four read paths, and for the Drive
    file path a query that is metadata (or ``alt=media`` for a download) and nothing else."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or parts.fragment:
        return False
    host = (parts.hostname or "").lower()
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if host == API_HOST:
        if not any(p.match(parts.path) for p in _API_PATHS):
            return False
        alt = query.get("alt")
        if alt and (not include_media or alt != ["media"] or parts.path.endswith("/export")
                    or parts.path == "/drive/v3/files"):
            return False
        return True
    if host == DOCS_HOST:
        return bool(_DOCS_EXPORT_PATH.match(parts.path))
    return False


class ReadOnlyGoogleClient:
    """Reads from Google with GET only. ``get`` refuses, before any network call, a URL outside the
    allowlist of read calls (see ``is_allowed_read_url``)."""

    def __init__(self, tokens: TokenSource, *, transport: Optional[Transport] = None,
                 timeout_s: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self.tokens = tokens
        self.transport: Transport = transport or urllib_get
        self.timeout_s = timeout_s

    def get(self, url: str, *, subject: Optional[str], scope: str,
            max_bytes: int = MAX_EXPORT_BYTES) -> Tuple[bytes, bool]:
        """GET ``url`` as ``subject`` (None = the service account). Returns ``(body, truncated)``;
        raises ``GoogleReadError`` for a refusal, a missing file, a size limit or a network error."""
        if not is_allowed_read_url(url):
            raise GoogleReadError(REASON_UNAVAILABLE, UNAVAILABLE_MESSAGE, "request outside the read-only allowlist")
        token = self.tokens(subject, scope)
        headers = {"Authorization": f"Bearer {token}", "Accept": "*/*"}
        try:
            status, body, truncated = self.transport(url, headers, self.timeout_s, max_bytes)
        except TimeoutError as exc:
            raise GoogleReadError(REASON_UNREACHABLE, "Google did not answer in time.", "timeout") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise GoogleReadError(REASON_UNREACHABLE, "I could not reach Google.", type(exc).__name__) from exc
        if status == 200:
            return body, truncated
        raise self.failure(status, body, subject)

    @staticmethod
    def failure(status: int, body: bytes, subject: Optional[str]) -> GoogleReadError:
        reason_text = ""
        try:
            data = json.loads(body.decode("utf-8", errors="replace"))
            errors = (data.get("error") or {}).get("errors") or []
            reason_text = str((errors[0] or {}).get("reason") or "") if errors else ""
        except (ValueError, AttributeError, IndexError):
            reason_text = ""
        if status in (429, 500, 502, 503, 504) or reason_text in ("rateLimitExceeded", "userRateLimitExceeded"):
            return GoogleReadError(REASON_UNREACHABLE, "Google is busy right now. Try again in a moment.",
                                   f"http {status} {reason_text}")
        if reason_text in ("exportSizeLimitExceeded", "fileTooLarge"):
            return GoogleReadError(REASON_TOO_LARGE, "This file is too large for me to read in full.",
                                   f"http {status} {reason_text}")
        if status == 404:
            # Drive answers 404 for a file the caller cannot see as well as for one that is gone.
            return GoogleReadError(REASON_NOT_SHARED if subject else REASON_NOT_FOUND,
                                   NOT_SHARED_WITH_USER if subject else NOT_FOUND_MESSAGE, "http 404")
        if status in (401, 403):
            return GoogleReadError(REASON_NOT_SHARED,
                                   NOT_SHARED_WITH_USER if subject else NOT_SHARED_SERVICE,
                                   f"http {status} {reason_text}")
        return GoogleReadError(REASON_UNREACHABLE, "Google could not give me this file.", f"http {status}")


# --------------------------------------------------------------------------- the reader


@dataclass
class GoogleLinkConfig:
    credentials_path: str
    impersonate_domains: Tuple[str, ...] = ()
    impersonate_scope: str = SCOPE_DRIVE_FULL
    service_scope: str = SCOPE_DRIVE_READONLY
    service_fallback: bool = False

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> Optional["GoogleLinkConfig"]:
        """The configuration the environment describes, or ``None`` when Google links are off
        (no key path, or a path that is not a readable service account key)."""
        env = os.environ if env is None else env
        path = (env.get("QAR_FILES_GOOGLE_CREDENTIALS") or "").strip()
        if not path:
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                info = json.load(handle)
            if info.get("type") != "service_account":
                raise ValueError("not a service account key")
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("Google link reading is off: the credentials file is not usable (%s)", type(exc).__name__)
            return None
        domains = tuple(d.strip().lower().lstrip("@") for d in
                        (env.get("QAR_FILES_GOOGLE_IMPERSONATE_DOMAINS") or "").split(",") if d.strip())
        return cls(
            credentials_path=path,
            impersonate_domains=domains,
            impersonate_scope=(env.get("QAR_FILES_GOOGLE_IMPERSONATE_SCOPE") or "").strip() or SCOPE_DRIVE_FULL,
            service_scope=(env.get("QAR_FILES_GOOGLE_SA_SCOPE") or "").strip() or SCOPE_DRIVE_READONLY,
            service_fallback=(env.get("QAR_FILES_GOOGLE_SA_FALLBACK") or "").strip().lower() in ("1", "true", "yes", "on"),
        )


def clamp_text(text: str) -> Tuple[str, bool]:
    if len(text) <= MAX_TEXT_CHARS:
        return text, False
    return text[:MAX_TEXT_CHARS], True


class GoogleLinkReader:
    """Reads one Google link as the right identity and returns a bounded, redacted, paged answer."""

    def __init__(self, config: GoogleLinkConfig, *, client: Optional[ReadOnlyGoogleClient] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config
        self.client = client or ReadOnlyGoogleClient(ServiceAccountTokens(config.credentials_path))
        self.clock = clock
        self.cache: Dict[Tuple[str, str, str], Tuple[float, LinkText]] = {}
        self.cache_lock = threading.Lock()

    # --- capability -----------------------------------------------------------------

    def capabilities(self) -> Dict[str, Any]:
        return {"google": True,
                "impersonation": bool(self.config.impersonate_domains),
                "service_account_fallback": self.config.service_fallback}

    # --- identity ---------------------------------------------------------------------

    def identity_plan(self, as_user: Optional[str]) -> List[Tuple[Optional[str], str]]:
        """The ordered ``(subject, scope)`` identities to try. Empty means refuse.

        Impersonation, when ``as_user`` is in an allowed domain, is the ONLY identity tried for that
        person unless the operator enabled the service account fallback."""
        email = (as_user or "").strip().lower()
        match = EMAIL_RE.match(email) if email else None
        impersonable = bool(match) and match.group(1) in self.config.impersonate_domains
        plan: List[Tuple[Optional[str], str]] = []
        if impersonable:
            plan.append((email, self.config.impersonate_scope))
        if self.config.service_fallback:
            plan.append((None, self.config.service_scope))
        return plan

    # --- the operation ------------------------------------------------------------------

    def read(self, url: str, *, as_user: Optional[str] = None, full: bool = False,
             part: Optional[int] = None, focus: Optional[str] = None) -> Dict[str, Any]:
        """The ``link`` operation. Never raises: returns ``{"ok": True, ...}`` or
        ``{"ok": False, "error": "...", "reason": "..."}``."""
        try:
            ref = parse_google_link(url)
            if ref is None:
                return refusal(REASON_UNSUPPORTED_TYPE, "This is not a Google Docs, Sheets, Slides or Drive link.")
            if ref.kind == KIND_FORM:
                return refusal(REASON_UNSUPPORTED_TYPE, FORM_MESSAGE)
            plan = self.identity_plan(as_user)
            if not plan:
                return refusal(REASON_NOT_ALLOWED, NO_USER_MESSAGE)
            last: Optional[GoogleReadError] = None
            for subject, scope in plan:
                try:
                    doc = self.load(ref, subject, scope)
                    return self.shape(doc, full=full, part=part, focus=focus)
                except GoogleReadError as exc:
                    last = exc
                    if exc.reason not in (REASON_NOT_SHARED, REASON_NOT_FOUND):
                        break
                    log.info("google link: identity %s could not read (%s)",
                             "service account" if subject is None else "user", exc.reason)
            assert last is not None
            return refusal(last.reason, last.message)
        except GoogleReadError as exc:
            return refusal(exc.reason, exc.message)
        except Exception as exc:  # noqa: BLE001 - a link read must never take the server down
            log.warning("google link read failed: %s", type(exc).__name__)
            return refusal(REASON_UNREACHABLE, "I could not read this link right now.")

    # --- loading ---------------------------------------------------------------------------

    def cache_key(self, ref: GoogleRef, subject: Optional[str]) -> Tuple[str, str, str]:
        return (subject or "", ref.file_id, ref.gid or "")

    def load(self, ref: GoogleRef, subject: Optional[str], scope: str) -> LinkText:
        key = self.cache_key(ref, subject)
        now = self.clock()
        with self.cache_lock:
            hit = self.cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        doc = self.fetch(ref, subject, scope)
        with self.cache_lock:
            if len(self.cache) >= CACHE_MAX_ENTRIES:
                oldest = min(self.cache, key=lambda k: self.cache[k][0])
                self.cache.pop(oldest, None)
            self.cache[key] = (now + CACHE_TTL_SECONDS, doc)
        return doc

    def api_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        base = f"https://{API_HOST}/drive/v3/{path}"
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        return f"{base}?{urllib.parse.urlencode(clean)}" if clean else base

    def fetch(self, ref: GoogleRef, subject: Optional[str], scope: str) -> LinkText:
        fid = urllib.parse.quote(ref.file_id, safe="")

        def get(url: str, cap: int = MAX_EXPORT_BYTES) -> Tuple[bytes, bool]:
            return self.client.get(url, subject=subject, scope=scope, max_bytes=cap)

        meta_body, _ = get(self.api_url(f"files/{fid}", {
            "fields": "id,name,mimeType,size,shortcutDetails", "supportsAllDrives": "true"}))
        meta = json.loads(meta_body.decode("utf-8", errors="replace"))
        mime = str(meta.get("mimeType") or "")
        title = str(meta.get("name") or "")
        if mime == MIME_SHORTCUT:
            target = (meta.get("shortcutDetails") or {}).get("targetId")
            if not target:
                raise GoogleReadError(REASON_UNSUPPORTED_TYPE, "This shortcut does not point at a file I can read.")
            return self.fetch(GoogleRef(KIND_FILE, str(target)), subject, scope)
        if mime == MIME_FOLDER:
            return self.fetch_folder(ref.file_id, title, get)
        if mime == MIME_FORM:
            raise GoogleReadError(REASON_UNSUPPORTED_TYPE, FORM_MESSAGE)
        if mime in (MIME_DOC, MIME_SLIDES):
            body, cut = get(self.api_url(f"files/{fid}/export", {"mimeType": "text/plain"}))
            text, cut2 = clamp_text(body.decode("utf-8", errors="replace").lstrip("﻿"))
            return LinkText(title, text, KIND_DOC if mime == MIME_DOC else KIND_SLIDES, cut or cut2)
        if mime == MIME_SHEET:
            return self.fetch_sheet(ref, fid, title, get)
        return self.fetch_binary(fid, title, mime, meta, get)

    def fetch_folder(self, folder_id: str, title: str, get: Callable[..., Tuple[bytes, bool]]) -> LinkText:
        rows: List[str] = []
        token: Optional[str] = None
        cut = False
        while True:
            body, _ = get(self.api_url("files", {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime)",
                "pageSize": 100, "pageToken": token,
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}))
            data = json.loads(body.decode("utf-8", errors="replace"))
            for entry in data.get("files") or []:
                is_dir = entry.get("mimeType") == MIME_FOLDER
                name = str(entry.get("name") or "")
                rows.append(f"{name}/" if is_dir else
                            f"{name}  ({kind_label(str(entry.get('mimeType') or ''))}, modified {entry.get('modifiedTime') or '?'})")
            token = data.get("nextPageToken")
            if len(rows) >= MAX_FOLDER_ENTRIES:
                cut = bool(token) or len(rows) > MAX_FOLDER_ENTRIES
                rows = rows[:MAX_FOLDER_ENTRIES]
                break
            if not token:
                break
        header = f"Folder {title or '(untitled)'}: {len(rows)} entr{'y' if len(rows) == 1 else 'ies'}" + (
            " shown, more exist" if cut else "")
        return LinkText(title, header + "\n" + ("\n".join(rows) if rows else "(empty folder)"), KIND_FOLDER, cut)

    def fetch_sheet(self, ref: GoogleRef, fid: str, title: str,
                    get: Callable[..., Tuple[bytes, bool]]) -> LinkText:
        if ref.gid:
            url = (f"https://{DOCS_HOST}/spreadsheets/d/{fid}/export?"
                   + urllib.parse.urlencode({"format": "csv", "gid": ref.gid}))
            body, cut = get(url)
            text, cut2 = clamp_text(body.decode("utf-8", errors="replace").lstrip("﻿"))
            return LinkText(title, text, KIND_SHEET, cut or cut2)
        # No tab named: every tab, through the library's own workbook reader. Without openpyxl the
        # first tab alone is still read, and the text says so.
        try:
            body, cut = get(self.api_url(f"files/{fid}/export", {"mimeType": MIME_XLSX}))
            from ..core import attachments
            text = attachments._extract_xlsx(body) if not cut else None
        except GoogleReadError as exc:
            if exc.reason != REASON_TOO_LARGE:
                raise
            text = None
        if text:
            text, cut2 = clamp_text(text)
            return LinkText(title, text, KIND_SHEET, cut2)
        body, cut = get(self.api_url(f"files/{fid}/export", {"mimeType": "text/csv"}))
        text, cut2 = clamp_text(body.decode("utf-8", errors="replace").lstrip("﻿"))
        return LinkText(title, "[Only the first tab of this spreadsheet could be read.]\n" + text,
                        KIND_SHEET, cut or cut2)

    def fetch_binary(self, fid: str, title: str, mime: str, meta: Dict[str, Any],
                     get: Callable[..., Tuple[bytes, bool]]) -> LinkText:
        from ..core import attachments
        try:
            size = int(meta.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MAX_DOWNLOAD_BYTES:
            raise GoogleReadError(REASON_TOO_LARGE, "This file is too large for me to read.")
        extractor = {"application/pdf": attachments._extract_pdf, MIME_DOCX: attachments._extract_docx,
                     MIME_XLSX: attachments._extract_xlsx}.get(mime)
        is_text = mime.startswith("text/") or mime in ("application/json", "application/xml")
        if extractor is None and not is_text:
            raise GoogleReadError(REASON_UNSUPPORTED_TYPE,
                                  "I can read Docs, Sheets, Slides, PDFs, Word and Excel files and plain text, "
                                  f"but not this kind of file ({kind_label(mime)}).")
        body, cut = get(self.api_url(f"files/{fid}", {"alt": "media", "supportsAllDrives": "true"}),
                        MAX_DOWNLOAD_BYTES)
        if cut:
            raise GoogleReadError(REASON_TOO_LARGE, "This file is too large for me to read.")
        text = extractor(body) if extractor else body.decode("utf-8", errors="replace")
        if not text or not text.strip():
            raise GoogleReadError(REASON_UNSUPPORTED_TYPE,
                                  "I could not find readable text in this file (it may be a scan or protected).")
        text, cut2 = clamp_text(text)
        return LinkText(title, text, KIND_FILE, cut2)

    # --- shaping ------------------------------------------------------------------------

    def shape(self, doc: LinkText, *, full: bool, part: Optional[int], focus: Optional[str]) -> Dict[str, Any]:
        """Redact, then return the focused passages or one part of the whole text."""
        from .web_page_extract import select_passages, split_passages
        from .web_research import paginate_text
        text = redact(doc.text)
        answer: Dict[str, Any] = {"ok": True, "title": doc.title, "kind": doc.kind, "truncated": doc.cut}
        try:
            part_number = int(part) if part is not None and not isinstance(part, bool) else None
        except (TypeError, ValueError):
            part_number = None
        if part_number is not None and part_number < 1:
            part_number = 1
        if full or part_number is not None:
            parts = paginate_text(text, DEFAULT_FULL_TOKENS * CHARS_PER_TOKEN)
            wanted = part_number or 1
            answer.update(mode="full", part=wanted, parts=len(parts),
                          text=parts[wanted - 1] if wanted <= len(parts) else "")
            return answer
        total_tokens = max(1, len(text) // CHARS_PER_TOKEN)
        passages = split_passages(text)
        chosen = select_passages(passages, (focus or "").strip(), DEFAULT_FOCUS_TOKENS)
        body = "\n...\n".join(chosen) if chosen else text[: DEFAULT_FOCUS_TOKENS * CHARS_PER_TOKEN]
        answer.update(mode="focus", text=body, total_tokens=total_tokens,
                      excerpt_only=total_tokens > DEFAULT_FOCUS_TOKENS * 1.25)
        return answer


def refusal(reason: str, message: str) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "error": message}


def kind_label(mime: str) -> str:
    labels = {MIME_DOC: "Google Doc", MIME_SHEET: "Google Sheet", MIME_SLIDES: "Google Slides",
              MIME_FORM: "Google Form", MIME_FOLDER: "folder", "application/pdf": "PDF"}
    return labels.get(mime) or (mime.split("/")[-1][:30] if mime else "file")


def build_link_reader_from_env(env: Optional[Mapping[str, str]] = None) -> Optional[GoogleLinkReader]:
    """The reader the environment configures, or ``None`` when Google links are off."""
    config = GoogleLinkConfig.from_env(env)
    return GoogleLinkReader(config) if config else None
