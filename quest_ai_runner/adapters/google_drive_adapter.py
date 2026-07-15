"""GoogleDriveAdapter -- RetrievalAdapter over Google Drive files and folders.

This lets the orchestrator brain ground answers in a Workspace's Drive documents: list a folder's
contents, read a file's text content (Google Docs, Google Sheets, PDFs, plain text), and expose all
of it through the same ``RetrievalAdapter`` surface every other source uses (read / grep / query +
discovery). It mirrors ``google_chat_adapter.GoogleChatAdapter`` exactly:

  * HTTP is stdlib-only (``urllib.request`` + ``json``) for everything except PDF text extraction,
    which lazily imports the optional ``pypdf`` package only when a PDF is actually read -- so the
    module loads fine without it, and importing this file never requires it.
  * AUTH is injected. The adapter never knows how a bearer token is minted; it calls a
    ``token_provider()`` callable you supply. Reuses the SAME token-provider helpers the Chat adapter
    ships (``static_token_provider`` / ``service_account_token_provider`` in ``google_chat_adapter``)
    rather than duplicating that logic -- pass ``scopes=DEFAULT_DRIVE_SCOPES`` (or your own read
    scopes) when building a service-account provider for Drive.
  * Every public method catches all exceptions and returns ``Observation(kind="error")`` rather than
    raising, so a missing token, an unreachable endpoint, a 403/404, or an unsupported mimeType never
    breaks the orchestrator loop.

Wire it into a CompositeRetrievalAdapter alongside the local corpus::

    from quest_ai_runner.adapters import CompositeRetrievalAdapter, FilesAdapter, GoogleDriveAdapter
    from quest_ai_runner.adapters.google_chat_adapter import service_account_token_provider
    from quest_ai_runner.adapters.google_drive_adapter import DEFAULT_DRIVE_SCOPES

    drive = GoogleDriveAdapter(
        token_provider=service_account_token_provider(
            service_account_file="/path/to/sa.json",
            subject="someone@your-workspace.org",   # domain-wide delegation
            scopes=DEFAULT_DRIVE_SCOPES,
        ),
        root_folder_id="1AbCdEfGhIjKlMnOpQrStUvWxYz",  # optional, for list_sources()
    )
    retrieval = CompositeRetrievalAdapter([FilesAdapter(corpus_root), drive])

CONTENT EXTRACTION, by mimeType:
  * Google Docs (``application/vnd.google-apps.document``) -> Drive ``files.export`` as
    ``text/plain``.
  * Google Sheets (``application/vnd.google-apps.spreadsheet``) -> Drive ``files.export`` as
    ``text/csv``. LIMITATION: the ``files.export`` endpoint only ever exports the FIRST sheet/tab of
    a multi-sheet spreadsheet; other tabs are not reachable through this adapter.
  * ``application/pdf`` -> ``files.get(alt="media")`` (raw bytes) then text extraction via the
    optional ``pypdf`` package (lazy-imported; a clean, install-me error Observation if it's absent).
  * Plain-text-ish mimeTypes (``text/*``, ``application/json``, ``application/xml``) ->
    ``files.get(alt="media")`` decoded as UTF-8.
  * Anything else -> an error Observation naming the unsupported mimeType, never raw binary as text.

A Drive FOLDER id (or URL) given to ``read_section`` / ``describe_source`` is auto-detected via its
metadata mimeType and handled as a listing rather than a read.
"""
from __future__ import annotations

import io
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import Observation, RetrievalAdapterBase

_log = logging.getLogger("quest-ai-runner.google-drive")

_DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

# Read-only scope sufficient to list and read Drive content.
DEFAULT_DRIVE_SCOPES = (
    "https://www.googleapis.com/auth/drive.readonly",
)

# A token provider returns a bearer access token string (or None when it cannot mint one -- the
# adapter then degrades to an error Observation rather than raising). Same shape as
# ``google_chat_adapter.TokenProvider``; duplicated here only as a type alias (no logic), so this
# module has no import-time dependency on google_chat_adapter for consumers that only want Drive.
TokenProvider = Callable[[], Optional[str]]

_MIME_GOOGLE_DOC = "application/vnd.google-apps.document"
_MIME_GOOGLE_SHEET = "application/vnd.google-apps.spreadsheet"
_MIME_GOOGLE_FOLDER = "application/vnd.google-apps.folder"
_MIME_PDF = "application/pdf"
# Non-"text/*" mimeTypes that are still safely UTF-8-decodable plain text.
_TEXT_LIKE_MIMES = {"application/json", "application/xml"}

_METADATA_FIELDS = "id, name, mimeType, webViewLink, modifiedTime, size"
_LIST_FIELDS = f"nextPageToken, files({_METADATA_FIELDS})"

# Hard cap on a single folder listing, independent of any caller-supplied max_bytes: stops paginating
# once this many children have been collected, and truncates the serialized listing to this many
# entries. Without this, a folder with thousands of items would fold an unbounded metadata blob into
# an LLM's grounding context (pagination itself is otherwise unbounded).
_MAX_LIST_FILES = 200


# ---------------------------------------------------------------------------
# URL parsing helper
# ---------------------------------------------------------------------------

_FOLDER_URL_RE = re.compile(r"drive\.google\.com/drive/folders/([a-zA-Z0-9_-]+)")
_FILE_URL_RE = re.compile(r"drive\.google\.com/file/d/([a-zA-Z0-9_-]+)")
_DOC_URL_RE = re.compile(r"docs\.google\.com/document/d/([a-zA-Z0-9_-]+)")
_SHEET_URL_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9_-]+)")


def parse_drive_url(url: str) -> Optional[Dict[str, str]]:
    """Parse a Google Drive/Docs/Sheets URL into ``{"kind": "file"|"folder", "id": "..."}``.

    Recognizes:
      - ``drive.google.com/file/d/<id>/...``        -> ``{"kind": "file", "id": ...}``
      - ``drive.google.com/drive/folders/<id>``     -> ``{"kind": "folder", "id": ...}``
      - ``docs.google.com/document/d/<id>/...``     -> ``{"kind": "file", "id": ...}``
      - ``docs.google.com/spreadsheets/d/<id>/...`` -> ``{"kind": "file", "id": ...}``

    Returns ``None`` for anything else -- not a recognized Drive URL, or not a URL at all (e.g. a
    bare file/folder id, which callers should just use directly). Never raises.
    """
    try:
        s = str(url or "").strip()
        if not s:
            return None
        for rx, kind in (
            (_FOLDER_URL_RE, "folder"),
            (_FILE_URL_RE, "file"),
            (_DOC_URL_RE, "file"),
            (_SHEET_URL_RE, "file"),
        ):
            m = rx.search(s)
            if m:
                return {"kind": kind, "id": m.group(1)}
        return None
    except Exception:  # noqa: BLE001
        return None


def _extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes via the optional ``pypdf`` package. May raise ImportError."""
    import pypdf  # type: ignore

    reader = pypdf.PdfReader(io.BytesIO(data))
    parts = [(page.extract_text() or "") for page in reader.pages]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class GoogleDriveAdapter(RetrievalAdapterBase):
    """RetrievalAdapter over Google Drive files and folders.

    Given a bearer token (via ``token_provider``), lists a folder's direct children or reads one
    file's text content, through the standard retrieval surface (``read_section`` / ``query`` +
    discovery). ``grep`` is not supported (Drive has no cheap full-text index behind this adapter);
    it returns a clear "use query() instead" error Observation, the same convention
    ``CachedDbAdapter`` / ``QuestRetrievalAdapter`` use for sources that aren't grep-able.
    """

    def __init__(
        self,
        token_provider: Optional[TokenProvider] = None,
        *,
        root_folder_id: Optional[str] = None,
        timeout_seconds: float = 30.0,
        api_base: str = _DRIVE_API_BASE,
        default_max_bytes: int = 200_000,
    ) -> None:
        """
        Args:
            token_provider: Callable returning a bearer access token (see the
                ``*_token_provider`` helpers in ``google_chat_adapter``, e.g.
                ``service_account_token_provider(..., scopes=DEFAULT_DRIVE_SCOPES)``). If None, the
                adapter is "unconfigured" and every method returns a clean error Observation.
            root_folder_id: Optional Drive folder id this adapter treats as its "root" for
                ``list_sources()`` (mirrors ``FilesAdapter.root``). Without it, ``list_sources()``
                just explains how to browse via an explicit folder id in ``query()``.
            timeout_seconds: Per-request HTTP timeout.
            api_base: Drive REST v3 base URL (override only for testing / proxies).
            default_max_bytes: Default cap on returned text size before truncation.
        """
        self._token_provider = token_provider
        self._root_folder_id = root_folder_id
        self._timeout = float(timeout_seconds)
        self._api_base = api_base.rstrip("/")
        self._default_max_bytes = int(default_max_bytes)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _build_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        base = f"{self._api_base}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                return f"{base}?{urllib.parse.urlencode(clean)}"
        return base

    def _get_bytes(self, url: str, token: str) -> bytes:
        """GET a Drive API URL with a bearer token; return the raw response body. May raise."""
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read()

    def _get_json(self, url: str, token: str) -> Dict[str, Any]:
        """GET a Drive API URL with a bearer token; return the parsed JSON. May raise."""
        return json.loads(self._get_bytes(url, token).decode("utf-8"))

    # ------------------------------------------------------------------
    # Drive API calls
    # ------------------------------------------------------------------

    def _get_metadata(self, file_id: str, token: str) -> Dict[str, Any]:
        url = self._build_url(
            f"files/{urllib.parse.quote(file_id, safe='')}",
            {"fields": _METADATA_FIELDS, "supportsAllDrives": True},
        )
        return self._get_json(url, token)

    def _list_folder(self, folder_id: str, token: str) -> tuple[List[Dict[str, Any]], bool]:
        """List files directly inside ``folder_id``, paginated up to ``_MAX_LIST_FILES``.

        Returns ``(files, truncated)`` -- ``truncated`` is True when the folder has more children
        than the cap (stops paginating early rather than walking every page for a huge folder). May
        raise (caller wraps in a try/except and converts to an error Observation).
        """
        out: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        q = f"'{folder_id}' in parents and trashed = false"
        while True:
            url = self._build_url(
                "files",
                {
                    "q": q,
                    "fields": _LIST_FIELDS,
                    "pageSize": 100,
                    "pageToken": page_token,
                    "supportsAllDrives": True,
                    "includeItemsFromAllDrives": True,
                },
            )
            data = self._get_json(url, token)
            out.extend(data.get("files", []) or [])
            page_token = data.get("nextPageToken")
            if len(out) >= _MAX_LIST_FILES:
                # Enforce the cap even if a single response returned more than one page's worth
                # (e.g. a server/fake ignoring pageSize) -- never rely on pagination alone.
                truncated = bool(page_token) or len(out) > _MAX_LIST_FILES
                return out[:_MAX_LIST_FILES], truncated
            if not page_token:
                return out, False

    def _export(self, file_id: str, mime_type: str, token: str) -> bytes:
        url = self._build_url(
            f"files/{urllib.parse.quote(file_id, safe='')}/export", {"mimeType": mime_type}
        )
        return self._get_bytes(url, token)

    def _download(self, file_id: str, token: str) -> bytes:
        url = self._build_url(
            f"files/{urllib.parse.quote(file_id, safe='')}",
            {"alt": "media", "supportsAllDrives": True},
        )
        return self._get_bytes(url, token)

    # ------------------------------------------------------------------
    # Fetch + normalize
    # ------------------------------------------------------------------

    def _list_as_observation(self, folder_id: str, token: str) -> Observation:
        try:
            files, truncated = self._list_folder(folder_id, token)
        except Exception as exc:  # noqa: BLE001
            return Observation(
                kind="error", rel_path=folder_id, error=f"could not list Drive folder {folder_id}: {exc}"
            )
        payload = [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "mimeType": f.get("mimeType"),
                "webViewLink": f.get("webViewLink"),
                "modifiedTime": f.get("modifiedTime"),
                "size": f.get("size"),
            }
            for f in files
        ]
        body: Dict[str, Any] = {"files": payload}
        if truncated:
            body["truncated"] = True
            body["note"] = (
                f"showing the first {len(payload)} items; this folder has more -- narrow the "
                "query or browse a subfolder instead"
            )
        return Observation(
            kind="query",
            rel_path=folder_id,
            locator="list",
            text=json.dumps(body, indent=2),
        )

    def _read_file(
        self, file_id: str, token: str, *, max_bytes: Optional[int] = None
    ) -> Observation:
        """Fetch metadata for ``file_id`` and dispatch to the right content-extraction path.

        A folder id is auto-detected via its mimeType and handled as a listing instead."""
        try:
            meta = self._get_metadata(file_id, token)
        except Exception as exc:  # noqa: BLE001
            return Observation(
                kind="error", rel_path=file_id,
                error=f"could not fetch Drive metadata for {file_id}: {exc}",
            )

        mime = meta.get("mimeType") or ""
        name = meta.get("name") or file_id

        if mime == _MIME_GOOGLE_FOLDER:
            return self._list_as_observation(file_id, token)

        try:
            if mime == _MIME_GOOGLE_DOC:
                text = self._export(file_id, "text/plain", token).decode("utf-8", errors="replace")
            elif mime == _MIME_GOOGLE_SHEET:
                # LIMITATION: files.export only ever exports the FIRST sheet/tab of a multi-sheet
                # spreadsheet as CSV; other tabs are not reachable through this endpoint.
                text = self._export(file_id, "text/csv", token).decode("utf-8", errors="replace")
            elif mime == _MIME_PDF:
                try:
                    import pypdf  # noqa: F401  # type: ignore
                except ImportError:
                    return Observation(
                        kind="error", rel_path=file_id,
                        error=(
                            "reading PDF content from Google Drive needs the 'pypdf' package; "
                            "install it with `pip install pypdf` (or the `quest-ai-runner[drive]` "
                            "extra)"
                        ),
                    )
                raw = self._download(file_id, token)
                text = _extract_pdf_text(raw)
            elif mime.startswith("text/") or mime in _TEXT_LIKE_MIMES:
                text = self._download(file_id, token).decode("utf-8", errors="replace")
            else:
                return Observation(
                    kind="error", rel_path=file_id,
                    error=(
                        f"mimeType {mime!r} ({name}) is not supported for content extraction yet; "
                        f"supported: Google Docs, Google Sheets (first sheet), PDF, and plain-text "
                        f"files"
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            return Observation(
                kind="error", rel_path=file_id, error=f"google drive read error for {file_id}: {exc}"
            )

        limit = max_bytes if (max_bytes and max_bytes > 0) else self._default_max_bytes
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) > limit:
            text = encoded[:limit].decode("utf-8", errors="ignore").rstrip() + "\n…[truncated]"
        return Observation(kind="read", rel_path=file_id, locator=f"mimeType={mime}", text=text)

    # ------------------------------------------------------------------
    # RetrievalAdapter interface
    # ------------------------------------------------------------------

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        """Read one Drive file's content (or list a folder) by id or URL, via the same
        read/list logic ``query()`` uses -- so this adapter works through the generic
        RetrievalAdapter surface, not only through ``query``. ``heading`` is accepted for interface
        parity but ignored (Drive content exported as flat text has no addressable heading index
        here)."""
        try:
            token = self._get_token()
            if not token:
                return Observation(kind="error", rel_path=rel_path, error=self._unconfigured_error())

            parsed = parse_drive_url(rel_path)
            item_id = parsed["id"] if parsed else str(rel_path or "").strip()
            if not item_id:
                return Observation(
                    kind="error", rel_path=rel_path,
                    error="read_section requires a Drive file/folder id or URL",
                )

            obs = self._read_file(item_id, token, max_bytes=max_bytes)
            obs.rel_path = rel_path
            if obs.kind != "read" or not obs.text:
                return obs  # error, or a folder-listing query Observation -- pass through as-is

            text = obs.text
            if start_line or end_line:
                lines = text.split("\n")
                start = (start_line or 1) - 1
                end = end_line or len(lines)
                text = "\n".join(lines[max(0, start): min(len(lines), end)])
            obs.text = text
            return obs
        except Exception as exc:  # noqa: BLE001
            _log.debug("google drive read_section failed for %r: %s", rel_path, exc)
            return Observation(kind="error", rel_path=rel_path, error=f"google drive read error: {exc}")

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Not supported -- Drive has no cheap full-text index behind this adapter; use query()."""
        return Observation(
            kind="error", pattern=pattern, scope=scope,
            error=(
                "grep is not supported on Google Drive; use query({'action': 'list'|'read', ...}) "
                "or read_section(file_or_folder_id) instead"
            ),
        )

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Structured Drive lookup.

        Spec keys:
          ``action``    -- "list" (folder contents) or "read" (one file's content). Required.
          ``folder_id`` -- Drive folder id, required when ``action == "list"``.
          ``file_id``   -- Drive file id, required when ``action == "read"``.
          ``max_bytes`` -- optional cap on returned text size for ``action == "read"``.
        """
        try:
            token = self._get_token()
            if not token:
                return Observation(kind="error", error=self._unconfigured_error())

            action = str(spec.get("action") or "").strip().lower()
            if action == "list":
                folder_id = str(spec.get("folder_id") or "").strip()
                if not folder_id:
                    return Observation(
                        kind="error", error="query({'action': 'list', ...}) requires a folder_id"
                    )
                return self._list_as_observation(folder_id, token)
            if action == "read":
                file_id = str(spec.get("file_id") or "").strip()
                if not file_id:
                    return Observation(
                        kind="error", error="query({'action': 'read', ...}) requires a file_id"
                    )
                return self._read_file(file_id, token, max_bytes=spec.get("max_bytes"))
            return Observation(
                kind="error", error=f"unknown Drive query action {action!r}; use 'list' or 'read'"
            )
        except Exception as exc:  # noqa: BLE001
            _log.debug("google drive query failed: %s", exc)
            return Observation(kind="error", error=f"google drive query error: {exc}")

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_sources(self) -> Observation:
        token = self._get_token()
        if not token:
            return Observation(kind="query", locator="list_sources", text=self._unconfigured_error())
        if not self._root_folder_id:
            return Observation(
                kind="query", locator="list_sources",
                text=(
                    "Google Drive has no configured root folder. Call query({'action': 'list', "
                    "'folder_id': '<id>'}) with a known folder id, or read_section(file_or_folder_id) "
                    "directly, to browse."
                ),
            )
        obs = self._list_as_observation(self._root_folder_id, token)
        if obs.kind == "error":
            return obs
        return Observation(kind="query", locator="list_sources", text=obs.text)

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        token = self._get_token()
        if not token:
            return Observation(kind="error", error=self._unconfigured_error())

        parsed = parse_drive_url(name)
        item_id = parsed["id"] if parsed else str(name or "").strip()
        if not item_id:
            return Observation(kind="error", error="describe_source requires a Drive file/folder id or URL")

        try:
            meta = self._get_metadata(item_id, token)
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"could not describe Drive item {name}: {exc}")

        mime = meta.get("mimeType") or "unknown"
        lines = [
            f"Google Drive item '{meta.get('name', item_id)}' (id={item_id})",
            f"mimeType: {mime}",
            f"modifiedTime: {meta.get('modifiedTime', 'unknown')}",
            f"webViewLink: {meta.get('webViewLink', 'unknown')}",
        ]
        if mime == _MIME_GOOGLE_FOLDER:
            try:
                children = self._list_folder(item_id, token)
                lines.append(
                    f"{len(children)} item(s) directly inside this folder "
                    f"(use query({{'action': 'list', 'folder_id': '{item_id}'}}) to see them)."
                )
            except Exception:  # noqa: BLE001
                pass
        else:
            lines.append(f"size: {meta.get('size', 'unknown')} bytes")
            readable = (
                mime in (_MIME_GOOGLE_DOC, _MIME_GOOGLE_SHEET, _MIME_PDF)
                or mime.startswith("text/")
                or mime in _TEXT_LIKE_MIMES
            )
            if readable:
                lines.append(
                    f"content is readable via query({{'action': 'read', 'file_id': '{item_id}'}}) "
                    f"or read_section('{item_id}')"
                )
            else:
                lines.append("content extraction is not supported yet for this mimeType")
        return Observation(kind="query", locator=f"describe_source({name})", text="\n".join(lines))

    def list_operations(self) -> Observation:
        return Observation(
            kind="query",
            locator="list_operations",
            text=(
                "drive_list: List files directly inside a Drive folder. "
                "query({'action': 'list', 'folder_id': '...'}).\n"
                "drive_read: Read one Drive file's content by id (Google Docs, Google Sheets first "
                "sheet, PDF, or plain text). query({'action': 'read', 'file_id': '...'}) or "
                "read_section(file_id)."
            ),
        )

    def describe_operation(self, name: str) -> Observation:
        ops = {
            "drive_list": (
                "drive_list: query({'action': 'list', 'folder_id': '<id>'}) -> file metadata "
                "(id, name, mimeType, webViewLink, modifiedTime, size) for each item directly "
                "inside the folder."
            ),
            "drive_read": (
                "drive_read: query({'action': 'read', 'file_id': '<id>'}) or read_section('<id>') "
                "-> the file's text content. Google Docs export as plain text; Google Sheets export "
                "their FIRST sheet as CSV; PDFs are extracted with pypdf; plain-text files are "
                "decoded as utf-8. Other mimeTypes return an error explaining content extraction "
                "isn't supported yet."
            ),
        }
        text = ops.get((name or "").lower().replace("-", "_").replace(" ", "_"))
        if not text:
            return Observation(kind="error", error=f"GoogleDriveAdapter: unknown operation {name!r}.")
        return Observation(kind="query", locator=f"describe_operation({name})", text=text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_token(self) -> Optional[str]:
        if self._token_provider is None:
            return None
        return self._token_provider()

    def _unconfigured_error(self) -> str:
        if self._token_provider is None:
            return "google drive not configured: no token_provider supplied"
        return "google drive not configured: token_provider returned no token"
