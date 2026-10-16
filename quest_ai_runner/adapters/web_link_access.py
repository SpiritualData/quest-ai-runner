"""web_link_access -- read links that are NOT ordinary public web pages, and fail honestly.

``WebResearchAdapter.fetch`` used to treat every URL the same: a plain GET, HTML only. That made
three common links a dead end: a Google Doc / Sheet / Slides / Drive file shared with "anyone with
the link" (Google serves an HTML app shell, or a sign-in page, instead of the content), a PDF, and
any page that needs a login. This module adds the two small, generic pieces that fix that without
any OAuth or account:

  * ``rewrite_for_export(url)`` -- recognises Google Docs / Sheets / Slides / Drive-file links and
    returns the public EXPORT url that serves the file's text when it is shared with a link
    anyone can open (document -> txt, spreadsheet -> csv with the tab's gid, presentation -> txt,
    drive file -> direct download). A file that is not shared answers with a sign-in redirect,
    which is reported as ``not_shared`` below, never as "Quest cannot open Google links".
  * Typed failure reasons (``REASON_*``) with ONE user-facing sentence each, so the planner and the
    answer can tell the user exactly why a link was not read and how to fix it.

Plus ``extract_pdf_text`` (optional ``pypdf``, degrades honestly when absent) and
``classify_thin_page`` (login wall vs JS shell). Pure functions, no network: the fetch itself stays
in ``web_research`` so the SSRF guard, timeout, size cap and redirect re-checks stay in one place.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlsplit

# ---------------------------------------------------------------------------
# Typed failure reasons
# ---------------------------------------------------------------------------

REASON_NOT_SHARED = "not_shared"
REASON_NOT_FOUND = "not_found"
REASON_TOO_LARGE = "too_large"
REASON_UNSUPPORTED_TYPE = "unsupported_type"
REASON_JS_SHELL = "js_shell"
REASON_BLOCKED = "blocked"
#: Network error, timeout, or a server error: nothing about the link itself is wrong.
REASON_UNREACHABLE = "unreachable"

ALL_REASONS = (
    REASON_NOT_SHARED,
    REASON_NOT_FOUND,
    REASON_TOO_LARGE,
    REASON_UNSUPPORTED_TYPE,
    REASON_JS_SHELL,
    REASON_BLOCKED,
    REASON_UNREACHABLE,
)

#: The sentence the user is told for each reason. Written for a stranger's team: it names no
#: product, org or account, and it never claims links of a whole kind (such as Google links) cannot
#: be opened.
REASON_MESSAGES = {
    REASON_NOT_SHARED: (
        "I could not open this link because the file is not shared with a link that anyone can "
        "open (it asks for a sign-in). To fix it, change its sharing setting to \"Anyone with the "
        "link can view\", or paste the text here."
    ),
    REASON_NOT_FOUND: (
        "I could not find anything at this link. The page or file may have been moved or deleted, "
        "or the link may be incomplete. Please check the link, or paste the text here."
    ),
    REASON_TOO_LARGE: (
        "This file is too large for me to read in full (my limit is 2 MB). Please share a smaller "
        "section of it, or paste the part you want me to look at."
    ),
    REASON_UNSUPPORTED_TYPE: (
        "I can read web pages, Google Docs, Sheets and Slides, and PDFs with selectable text, but "
        "I cannot read this kind of file. Please paste the text, or upload it as a PDF."
    ),
    REASON_JS_SHELL: (
        "This page builds its content with scripts that I cannot run, so all I could see was an "
        "empty shell. Please paste the text that matters, or share a plain-text or PDF version."
    ),
    REASON_BLOCKED: (
        "This site refused to let me read the page (it blocks automated requests or is limiting "
        "how often it can be read). Please paste the text here."
    ),
    REASON_UNREACHABLE: (
        "I could not reach this link (a network error or a timeout). Please try again in a "
        "moment, or paste the text here."
    ),
}

#: Replaces the generic unsupported-type sentence when the file IS a PDF but cannot be read.
PDF_NO_READER_MESSAGE = (
    "I could not read this PDF because PDF reading is not installed on this deployment. Please "
    "paste the text here."
)
PDF_NO_TEXT_MESSAGE = (
    "This PDF has no selectable text (it is probably a scan or a set of images), so I cannot read "
    "it. Please paste the text here."
)
FORM_MESSAGE = (
    "This is a Google Form, which I cannot open or read. Please paste the questions or the "
    "responses here, or share them as a Doc, Sheet or PDF."
)

FOLDER_MESSAGE = (
    "This is a folder link, and I can only read one file at a time. Please share the link to the "
    "file itself."
)


def link_not_read_error(url: str, reason: str, message: Optional[str] = None) -> str:
    """The error string an unreadable link becomes. The ``LINK NOT READ [reason]`` prefix is a
    stable marker the planner prompt refers to, so the model relays the reason."""
    return f"LINK NOT READ [{reason}] {url} : {message or REASON_MESSAGES.get(reason, '')}"


class LinkReadError(Exception):
    """A link could not be read, with a typed ``reason`` (one of ``ALL_REASONS``)."""

    def __init__(self, reason: str, detail: str = "", message: Optional[str] = None) -> None:
        self.reason = reason
        self.detail = detail
        self.user_message = message or REASON_MESSAGES.get(reason, "")
        super().__init__(f"{reason}: {detail}" if detail else reason)


# ---------------------------------------------------------------------------
# Google link recognition and export rewriting
# ---------------------------------------------------------------------------

#: Hosts whose sign-in redirect means "this file is not shared with everyone".
LOGIN_HOSTS = frozenset({"accounts.google.com", "login.microsoftonline.com", "login.live.com"})

_GOOGLE_ID = r"([A-Za-z0-9_-]{10,})"
_DOC_RE = re.compile(r"^/(?:a/[^/]+/)?document/(?:u/\d+/)?d/" + _GOOGLE_ID)
_SHEET_RE = re.compile(r"^/(?:a/[^/]+/)?spreadsheets/(?:u/\d+/)?d/" + _GOOGLE_ID)
_SLIDES_RE = re.compile(r"^/(?:a/[^/]+/)?presentation/(?:u/\d+/)?d/" + _GOOGLE_ID)
_FORMS_RE = re.compile(r"^/(?:a/[^/]+/)?forms/")
_DRIVE_FILE_RE = re.compile(r"^/(?:u/\d+/)?file/d/" + _GOOGLE_ID)
_DRIVE_FOLDER_RE = re.compile(r"^/(?:u/\d+/)?(?:drive/)?folders/")
_GID_RE = re.compile(r"(?:^|[#&?])gid=(\d+)")

KIND_DOC = "google_doc"
KIND_SHEET = "google_sheet"
KIND_SLIDES = "google_slides"
KIND_DRIVE_FILE = "drive_file"
KIND_DRIVE_FOLDER = "drive_folder"
KIND_FORM = "google_form"


@dataclass(frozen=True)
class LinkTarget:
    """A recognised Google link and the public URL that serves its content.

    ``export_url`` is empty for a folder (not readable). ``text_format`` says how the response body
    is read: ``"text"`` (document / slides export), ``"csv"`` (a sheet tab) or ``"file"`` (a Drive
    file of any type, decided from its bytes).
    """

    kind: str
    file_id: str
    export_url: str
    text_format: str


def rewrite_for_export(url: str) -> Optional[LinkTarget]:
    """Map a Google Docs / Sheets / Slides / Drive link to its public export URL, or ``None`` for
    any other URL (including published-to-web ``/d/e/...`` pages, which are ordinary public HTML).

    Never raises. The result is a plain https URL on a Google host; the caller still runs it
    through the SSRF guard and re-checks every redirect hop.
    """
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if parts.scheme not in ("http", "https"):
        return None

    if host == "docs.google.com":
        m = _DOC_RE.match(path)
        if m:
            fid = m.group(1)
            return LinkTarget(KIND_DOC, fid, f"https://docs.google.com/document/d/{fid}/export?format=txt", "text")
        m = _SHEET_RE.match(path)
        if m:
            fid = m.group(1)
            gid = _GID_RE.search(parts.query) or _GID_RE.search(parts.fragment)
            tab = f"&gid={gid.group(1)}" if gid else ""
            return LinkTarget(
                KIND_SHEET, fid,
                f"https://docs.google.com/spreadsheets/d/{fid}/export?format=csv{tab}", "csv")
        m = _SLIDES_RE.match(path)
        if m:
            fid = m.group(1)
            return LinkTarget(
                KIND_SLIDES, fid, f"https://docs.google.com/presentation/d/{fid}/export?format=txt", "text")
        if _FORMS_RE.match(path):
            return LinkTarget(KIND_FORM, "", "", "file")
        return None

    if host == "drive.google.com":
        if _DRIVE_FOLDER_RE.match(path):
            return LinkTarget(KIND_DRIVE_FOLDER, "", "", "file")
        m = _DRIVE_FILE_RE.match(path)
        fid = m.group(1) if m else ""
        if not fid and path in ("/open", "/uc"):
            ids = parse_qs(parts.query).get("id") or []
            if ids and re.fullmatch(r"[A-Za-z0-9_-]{10,}", ids[0]):
                fid = ids[0]
        if fid:
            return LinkTarget(
                KIND_DRIVE_FILE, fid,
                f"https://drive.google.com/uc?export=download&id={fid}", "file")
    return None


def is_login_host(host: str) -> bool:
    return (host or "").lower() in LOGIN_HOSTS


# ---------------------------------------------------------------------------
# Thin / blocked page classification
# ---------------------------------------------------------------------------

_LOGIN_WALL_RE = re.compile(
    r"\b(sign[ -]?in|log[ -]?in|log in to continue|sign in to continue|please (?:log|sign) in|"
    r"create an account to (?:view|continue|read)|members only|subscribe to (?:read|continue))\b",
    re.I,
)
_DRIVE_TOO_BIG_RE = re.compile(
    r"(can'?t scan this file for viruses|virus scan warning|too large to scan)", re.I)
_BOT_WALL_RE = re.compile(
    r"(verify you are (?:a )?human|are you a robot|enable javascript and cookies|"
    r"checking your browser|access denied|attention required)", re.I)


def classify_thin_page(html: str, text: str) -> str:
    """Why a page that parsed fine still gave almost no text: a login wall, a bot wall, a Drive
    "too large to scan" interstitial, or (the default) a JavaScript app shell."""
    blob = f"{text or ''}\n{(html or '')[:6000]}"
    if _DRIVE_TOO_BIG_RE.search(blob):
        return REASON_TOO_LARGE
    if _BOT_WALL_RE.search(blob):
        return REASON_BLOCKED
    if _LOGIN_WALL_RE.search(text or "") and len((text or "").strip()) < 400:
        return REASON_NOT_SHARED
    return REASON_JS_SHELL


def drive_download_is_too_big(html: str) -> bool:
    return bool(_DRIVE_TOO_BIG_RE.search((html or "")[:20000]))


# ---------------------------------------------------------------------------
# PDF text
# ---------------------------------------------------------------------------


def looks_like_pdf(data: bytes) -> bool:
    return (data or b"")[:1024].lstrip(b"\xef\xbb\xbf \r\n\t\x00").startswith(b"%PDF")


#: Stop reading PDF pages once this much text has been extracted, so a very long file cannot
#: stall a turn or fill the page cache; the text says so when it stops early.
PDF_MAX_TEXT_CHARS = 3_000_000


def extract_pdf_text(data: bytes) -> str:
    """Text of a PDF via the optional ``pypdf`` package.

    Raises ``LinkReadError(unsupported_type)`` with an honest sentence when ``pypdf`` is not
    installed, when the file is encrypted or damaged, or when it has no text layer (a scan).
    """
    try:
        import pypdf  # type: ignore
    except ImportError as exc:
        raise LinkReadError(REASON_UNSUPPORTED_TYPE, "pypdf is not installed", PDF_NO_READER_MESSAGE) from exc
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            raise LinkReadError(
                REASON_UNSUPPORTED_TYPE, "encrypted pdf",
                "This PDF is password protected, so I cannot read it. Please paste the text here.")
        page_texts = []
        total_chars = 0
        stopped_at = 0
        pages = list(reader.pages)
        for index, page in enumerate(pages):
            page_text = page.extract_text() or ""
            page_texts.append(page_text)
            total_chars += len(page_text)
            if total_chars >= PDF_MAX_TEXT_CHARS and index + 1 < len(pages):
                stopped_at = index + 1
                break
        text = "\n".join(page_texts)
        if stopped_at:
            text += (f"\n\n[Note: this PDF has {len(pages)} pages; only the first {stopped_at} "
                     "were read.]")
    except LinkReadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LinkReadError(REASON_UNSUPPORTED_TYPE, f"pdf parse failed: {exc}", PDF_NO_TEXT_MESSAGE) from exc
    if len(text.strip()) < 20:
        raise LinkReadError(REASON_UNSUPPORTED_TYPE, "pdf has no text layer", PDF_NO_TEXT_MESSAGE)
    return text
