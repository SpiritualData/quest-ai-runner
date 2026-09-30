"""NotionAdapter -- a READ-ONLY RetrievalAdapter over a configured set of Notion databases.

This lets the orchestrator brain ground answers in the rows and pages of the Notion databases a
deployment names: list them, query them with simple filters, read one page (its properties plus
its block text, bounded), and grep across their rows, all through the same ``RetrievalAdapter``
surface every other source uses.

It is GENERIC and READ-ONLY by construction, in the same way ``GoogleChatAdapter`` is:

  * HTTP is stdlib-only (``urllib.request`` + ``json``). No SDK, no third-party dependency.
  * AUTH is injected. The adapter never knows where a token comes from; it calls a
    ``token_provider()`` you supply. ``static_token_provider`` (a token you hold),
    ``env_token_provider`` (the NAME of an environment variable) and ``file_token_provider`` (a
    file path) are ready-made. A token value is never written in code, config, or an error
    message.
  * THERE ARE NO WRITE CALLS IN THIS MODULE. Every request goes through ``NotionAdapter.request``,
    which refuses anything but a ``GET`` or the one ``POST`` Notion uses to QUERY a database
    (``POST /databases/{id}/query`` reads; it changes nothing). A create, update, archive or delete
    is not merely unwired, it cannot be sent. The Notion integration's own capabilities should be
    set to "read content" only as well; this is the second lock, not the only one.
  * ONLY CONFIGURED DATABASES ARE READ. ``database_ids`` maps an alias to an id. A database that is
    not in that map is refused, a page whose parent is not one of those databases is refused, and
    a filter or read naming anything else gets an error naming what IS configured.
  * Every retrieval method catches all exceptions and returns ``Observation(kind="error")`` rather
    than raising, so a missing token, an unreachable API or a 403 never breaks the orchestrator
    loop. The typed entry points (``fetch_rows``, ``read_page``) DO raise ``NotionError`` so a caller
    that must tell "the read failed" from "there was nothing" (the ``notion_database`` context
    source, whose watermark must not advance past a read that never happened) can.

The Notion-Version header is pinned (``NOTION_VERSION``). 2022-06-28 is the last version in which
a database is queried at ``/databases/{id}/query``; later versions split a database into data
sources and change that shape, so moving the pin is a deliberate change with its own tests.

Wire it into a CompositeRetrievalAdapter alongside the local corpus::

    from quest_ai_runner.adapters import CompositeRetrievalAdapter, FilesAdapter, NotionAdapter
    from quest_ai_runner.adapters.notion_adapter import env_token_provider

    notion = NotionAdapter(
        token_provider=env_token_provider("NOTION_TOKEN"),
        database_ids={"tasks": "0123456789abcdef0123456789abcdef"},
    )
    retrieval = CompositeRetrievalAdapter([FilesAdapter(corpus_root), notion])

Or declaratively, with a ``[notion]`` block in the lane's TOML (see ``docs/adapters.md``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..core.adapters import Observation, RetrievalAdapterBase
from .tfdfidf_sampling import keywords_from_text

log = logging.getLogger("quest-ai-runner.notion")

NOTION_API_BASE = "https://api.notion.com/v1"

# Pinned on purpose; see the module docstring.
NOTION_VERSION = "2022-06-28"

# Notion caps a page of results at 100.
MAX_PAGE_SIZE = 100

# A token provider returns the integration token (or None when it cannot produce one; the adapter
# then reports a clean "not configured" error, never a crash).
TokenProvider = Callable[[], Optional[str]]

# Property types whose value changes on EVERY edit and so say nothing about what a person changed.
VOLATILE_TYPES = frozenset({"last_edited_time", "last_edited_by", "created_time", "created_by"})

QUERY_PATH = re.compile(r"^/databases/[0-9a-fA-F-]{32,36}/query$")
READ_PREFIXES = ("/pages/", "/databases/", "/blocks/")

MAX_PROPERTY_CHARS = 300
MAX_PROPERTIES_PER_ROW = 40


class NotionError(RuntimeError):
    """A Notion read failed or was refused. The message is safe to show: it never carries a token."""


# ---------------------------------------------------------------------------
# Token providers (auth is injected; a token is never hardcoded)
# ---------------------------------------------------------------------------

def static_token_provider(token: str) -> TokenProvider:
    """A provider that always returns a token the host already holds."""
    def provider() -> Optional[str]:
        return token or None
    return provider


def env_token_provider(env_name: str) -> TokenProvider:
    """A provider that reads the token from the environment variable NAMED ``env_name``.

    Read on every call, so a rotated token is picked up without a restart. The name is config;
    the value never is.
    """
    def provider() -> Optional[str]:
        value = (os.environ.get(env_name) or "").strip() if env_name else ""
        return value or None
    return provider


def file_token_provider(path: str) -> TokenProvider:
    """A provider that reads the token from a file (whitespace stripped), on every call."""
    def provider() -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
        except OSError:
            return None
        return value or None
    return provider


# ---------------------------------------------------------------------------
# Identifiers, time, text
# ---------------------------------------------------------------------------

def normalize_id(value: Any) -> str:
    """A Notion id as 32 lowercase hex characters, or "" when ``value`` holds none.

    Accepts a bare id, a dashed uuid, ``alias/<id>``, or a notion.so link (the id is the last 32
    characters of the last path segment, which is how Notion builds page links).
    """
    text = str(value or "").strip().lower().split("?")[0].split("#")[0]
    if not text:
        return ""
    segment = text.rstrip("/").rsplit("/", 1)[-1]
    tail = segment.replace("-", "")[-32:]
    if len(tail) == 32 and all(c in "0123456789abcdef" for c in tail):
        return tail
    return ""


def dashed(compact: str) -> str:
    """32 hex characters as the dashed uuid form Notion prints."""
    c = compact
    return f"{c[0:8]}-{c[8:12]}-{c[12:16]}-{c[16:20]}-{c[20:32]}"


def parse_time(value: Any) -> Optional[datetime]:
    """An RFC 3339 timestamp as aware UTC, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def minute_floor_iso(moment: datetime) -> str:
    """``moment`` floored to the minute, as the string Notion's timestamp filter takes.

    Notion reports ``last_edited_time`` at minute granularity, so a filter at 10:05:30 would miss a
    row edited at 10:05:45 (stored as 10:05:00). Flooring errs toward re-reading one extra minute,
    never toward losing an edit; the source's snapshot comparison removes the duplicates.
    """
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:00.000Z")


def clip(text: Any, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + " [...truncated]"


def plain(rich: Any) -> str:
    """The plain text of a Notion rich-text array."""
    return "".join(str((part or {}).get("plain_text") or "") for part in (rich or []))


def render_property(prop: Dict[str, Any]) -> str:
    """One Notion property value as a short string ("" when empty or of no textual meaning)."""
    kind = str((prop or {}).get("type") or "")
    value = (prop or {}).get(kind)
    if kind in ("title", "rich_text"):
        return plain(value)
    if kind == "number":
        return "" if value is None else str(value)
    if kind in ("select", "status"):
        return str((value or {}).get("name") or "")
    if kind == "multi_select":
        return ", ".join(str((v or {}).get("name") or "") for v in (value or []) if v)
    if kind == "date":
        start = str((value or {}).get("start") or "")
        end = str((value or {}).get("end") or "")
        return f"{start} to {end}" if start and end else start
    if kind == "checkbox":
        return "yes" if value else "no"
    if kind in ("url", "email", "phone_number"):
        return str(value or "")
    if kind == "people":
        return ", ".join(str((p or {}).get("name") or (p or {}).get("id") or "") for p in (value or []))
    if kind == "relation":
        ids = [str((r or {}).get("id") or "") for r in (value or [])]
        return f"{len(ids)} linked" if ids else ""
    if kind == "formula":
        inner = value or {}
        inner_kind = inner.get("type")
        if not inner_kind:
            return ""
        return render_property({"type": inner_kind, inner_kind: inner.get(inner_kind)})
    if kind == "files":
        return ", ".join(str((f or {}).get("name") or "") for f in (value or []))
    if kind == "unique_id":
        number = (value or {}).get("number")
        prefix = (value or {}).get("prefix")
        return "" if number is None else f"{prefix + '-' if prefix else ''}{number}"
    if kind in VOLATILE_TYPES:
        return ""
    return ""


@dataclass
class NotionRow:
    """One page of a configured database, with its properties rendered to short strings."""
    page_id: str = ""
    database: str = ""                   # the alias it was read under
    title: str = ""
    url: str = ""
    created_at: Optional[datetime] = None
    edited_at: Optional[datetime] = None
    properties: Dict[str, str] = field(default_factory=dict)   # non-empty, non-volatile only
    archived: bool = False


@dataclass
class NotionRows:
    rows: List[NotionRow] = field(default_factory=list)
    truncated: bool = False              # more rows matched than ``limit``


def parse_row(page: Dict[str, Any], database: str = "") -> NotionRow:
    """A ``NotionRow`` from a Notion page object."""
    title = ""
    props: Dict[str, str] = {}
    for name, prop in (page.get("properties") or {}).items():
        kind = str((prop or {}).get("type") or "")
        if kind in VOLATILE_TYPES:
            continue
        rendered = clip(render_property(prop), MAX_PROPERTY_CHARS)
        if kind == "title":
            title = rendered
            continue
        if rendered and len(props) < MAX_PROPERTIES_PER_ROW:
            props[str(name)] = rendered
    return NotionRow(
        page_id=normalize_id(page.get("id")),
        database=database,
        title=title,
        url=str(page.get("url") or ""),
        created_at=parse_time(page.get("created_time")),
        edited_at=parse_time(page.get("last_edited_time")),
        properties=props,
        archived=bool(page.get("archived") or page.get("in_trash")),
    )


def row_line(row: NotionRow) -> str:
    """A row as one searchable line: title, then its properties."""
    bits = [f"{k}: {v}" for k, v in row.properties.items()]
    head = row.title or "(untitled)"
    return f"{head} | " + "; ".join(bits) if bits else head


def render_row(row: NotionRow) -> str:
    lines = [f"{row.title or '(untitled)'} [{row.page_id}]"]
    for key, value in row.properties.items():
        lines.append(f"  {key}: {value}")
    if row.url:
        lines.append(f"  link: {row.url}")
    return "\n".join(lines)


def render_block(block: Dict[str, Any]) -> str:
    """One block's text, with a light markdown prefix. "" for a block with no text."""
    kind = str(block.get("type") or "")
    body = block.get(kind) or {}
    text = plain(body.get("rich_text"))
    if kind == "heading_1":
        return f"# {text}" if text else ""
    if kind == "heading_2":
        return f"## {text}" if text else ""
    if kind == "heading_3":
        return f"### {text}" if text else ""
    if kind == "bulleted_list_item":
        return f"- {text}" if text else ""
    if kind == "numbered_list_item":
        return f"1. {text}" if text else ""
    if kind == "to_do":
        return f"[{'x' if body.get('checked') else ' '}] {text}" if text else ""
    if kind == "quote":
        return f"> {text}" if text else ""
    if kind == "callout":
        return text
    if kind == "code":
        return f"```\n{text}\n```" if text else ""
    if kind == "child_page":
        return f"(sub-page: {body.get('title') or ''})"
    if kind == "bookmark":
        return str(body.get("url") or "")
    return text


# ---------------------------------------------------------------------------
# Simple filters -> Notion's filter shape
# ---------------------------------------------------------------------------

TEXT_OPS = frozenset({"equals", "does_not_equal", "contains", "does_not_contain", "starts_with",
                      "ends_with", "is_empty", "is_not_empty"})
NUMBER_OPS = frozenset({"equals", "does_not_equal", "greater_than", "less_than",
                        "greater_than_or_equal_to", "less_than_or_equal_to", "is_empty",
                        "is_not_empty"})
OPS_BY_TYPE: Dict[str, frozenset] = {
    "title": TEXT_OPS, "rich_text": TEXT_OPS, "url": TEXT_OPS, "email": TEXT_OPS,
    "phone_number": TEXT_OPS,
    "number": NUMBER_OPS,
    "checkbox": frozenset({"equals", "does_not_equal"}),
    "select": frozenset({"equals", "does_not_equal", "is_empty", "is_not_empty"}),
    "status": frozenset({"equals", "does_not_equal", "is_empty", "is_not_empty"}),
    "multi_select": frozenset({"contains", "does_not_contain", "is_empty", "is_not_empty"}),
    "date": frozenset({"equals", "before", "after", "on_or_before", "on_or_after", "is_empty",
                       "is_not_empty"}),
    "people": frozenset({"contains", "does_not_contain", "is_empty", "is_not_empty"}),
    "relation": frozenset({"contains", "does_not_contain", "is_empty", "is_not_empty"}),
}
DEFAULT_OP_BY_TYPE: Dict[str, str] = {
    "title": "contains", "rich_text": "contains", "url": "contains", "email": "contains",
    "phone_number": "contains", "number": "equals", "checkbox": "equals", "select": "equals",
    "status": "equals", "multi_select": "contains", "date": "equals", "people": "contains",
    "relation": "contains",
}


def build_notion_filter(schema: Dict[str, Dict[str, Any]], simple: Dict[str, Any]) -> Dict[str, Any]:
    """A Notion filter object from ``{"Property": value | {"operator": value}}``.

    ``schema`` is ``{property name: {"type": ...}}``. A bare value uses the type's natural operator
    (``contains`` for text and multi-value properties, ``equals`` otherwise). Raises ``ValueError``
    naming the valid choices when a property or operator does not exist, which the adapter turns
    into an error Observation, so the planner can correct itself instead of getting an empty list.
    """
    by_lower = {str(name).lower(): str(name) for name in schema}
    clauses: List[Dict[str, Any]] = []
    for raw_name, cond in (simple or {}).items():
        name = by_lower.get(str(raw_name).lower())
        if name is None:
            raise ValueError(f"no property {raw_name!r}; properties are: {', '.join(sorted(schema))}")
        kind = str((schema[name] or {}).get("type") or "")
        allowed = OPS_BY_TYPE.get(kind)
        if allowed is None:
            raise ValueError(f"property {name!r} is a {kind or 'unknown'} property, which cannot be filtered")
        if isinstance(cond, dict):
            pairs = list(cond.items())
        else:
            pairs = [(DEFAULT_OP_BY_TYPE[kind], cond)]
        for op, value in pairs:
            if op not in allowed:
                raise ValueError(
                    f"operator {op!r} does not apply to {kind} property {name!r}; "
                    f"use one of: {', '.join(sorted(allowed))}")
            if op in ("is_empty", "is_not_empty"):
                value = True
            clauses.append({"property": name, kind: {op: value}})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"and": clauses}


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class NotionAdapter(RetrievalAdapterBase):
    """RetrievalAdapter over a configured set of Notion databases. Read-only; never raises."""

    # A learned Notion hit is a ``notion_page`` reference, re-fetched fresh through this adapter's
    # own read path (see ``resolve_reference``), exactly as ``chat_thread`` is for Google Chat.
    reference_type = "notion_page"

    def __init__(
        self,
        token_provider: Optional[TokenProvider] = None,
        *,
        database_ids: Optional[Dict[str, str]] = None,
        cache_ttl_seconds: float = 120.0,
        timeout_seconds: float = 20.0,
        max_rows_per_database: int = 200,
        max_page_chars: int = 12000,
        max_blocks: int = 200,
        api_base: str = NOTION_API_BASE,
        notion_version: str = NOTION_VERSION,
    ) -> None:
        """
        Args:
            token_provider: Callable returning the integration token. None leaves the adapter
                "unconfigured": every method returns a clean error Observation.
            database_ids: ``{alias: database id}``. The ONLY databases this adapter will read.
            cache_ttl_seconds: How long fetched rows and schemas are reused.
            timeout_seconds: Per-request HTTP timeout.
            max_rows_per_database: Cap on rows pulled for a grep or an unfiltered query.
            max_page_chars: Cap on the text returned for one page.
            max_blocks: Cap on the blocks read from one page.
            api_base / notion_version: Override only for tests or a proxy.
        """
        self.token_provider = token_provider
        self.databases: Dict[str, str] = {}
        for alias, database_id in (database_ids or {}).items():
            compact = normalize_id(database_id)
            if str(alias).strip() and compact:
                self.databases[str(alias).strip()] = compact
        self.cache_ttl = max(0.0, float(cache_ttl_seconds))
        self.timeout = float(timeout_seconds)
        self.max_rows = max(1, int(max_rows_per_database))
        self.max_page_chars = max(200, int(max_page_chars))
        self.max_blocks = max(1, int(max_blocks))
        self.api_base = api_base.rstrip("/")
        self.notion_version = notion_version
        self.rows_cache: Dict[str, Tuple[float, NotionRows]] = {}
        self.schema_cache: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
        self.page_database: Dict[str, str] = {}      # page id -> alias, from rows this adapter saw

    # ------------------------------------------------------------------
    # HTTP (the ONLY place a request is sent)
    # ------------------------------------------------------------------

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Send one Notion request and return the parsed JSON. Raises ``NotionError``.

        The read-only guarantee lives here: only ``GET`` on pages, databases and blocks, and the
        one ``POST`` that queries a database, are allowed. Anything else raises before a socket is
        opened.
        """
        bare = path.split("?", 1)[0]
        if method == "GET":
            if not bare.startswith(READ_PREFIXES):
                raise NotionError(f"refused: GET {bare} is not a read this adapter makes")
        elif method == "POST":
            if not QUERY_PATH.match(bare):
                raise NotionError("refused: this adapter is read-only; POST is allowed only to query a database")
        else:
            raise NotionError(f"refused: this adapter is read-only; {method} is not allowed")
        token = self.token_provider() if self.token_provider else None
        if not token:
            raise NotionError("notion is not configured: no token available")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.api_base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Notion-Version", self.notion_version)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raise NotionError(self.http_error_text(exc)) from None
        except urllib.error.URLError as exc:
            raise NotionError(f"could not reach Notion ({exc.reason})") from None
        except OSError as exc:
            raise NotionError(f"could not reach Notion ({type(exc).__name__})") from None
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            raise NotionError("Notion returned something that was not JSON") from None

    @staticmethod
    def http_error_text(exc: urllib.error.HTTPError) -> str:
        """Notion's own message when it sent one, so a permissions problem reads as one."""
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            message = str(payload.get("message") or "").strip()
            if message:
                return f"HTTP {exc.code}: {message}"
        except Exception:  # noqa: BLE001 -- the body is optional
            pass
        return f"HTTP {exc.code}"

    # ------------------------------------------------------------------
    # Databases: which are configured, their schema, their rows
    # ------------------------------------------------------------------

    def resolve_database(self, name: Any) -> Optional[str]:
        """The database id for a configured alias, or for a configured id. None when not configured."""
        text = str(name or "").strip()
        if not text:
            return None
        for alias, database_id in self.databases.items():
            if alias.lower() == text.lower():
                return database_id
        compact = normalize_id(text)
        if compact and compact in self.databases.values():
            return compact
        return None

    def alias_for(self, database_id: str) -> str:
        compact = normalize_id(database_id)
        for alias, known in self.databases.items():
            if known == compact:
                return alias
        return ""

    def require_database(self, name: Any) -> str:
        database_id = self.resolve_database(name)
        if not database_id:
            known = ", ".join(sorted(self.databases)) or "none"
            raise NotionError(f"database {str(name)!r} is not one of the configured databases (configured: {known})")
        return database_id

    def schema(self, database_id: str) -> Dict[str, Dict[str, Any]]:
        """``{property name: {"type": ...}}`` for one configured database, cached. Raises NotionError."""
        now = time.time()
        cached = self.schema_cache.get(database_id)
        if cached and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        payload = self.request("GET", f"/databases/{dashed(database_id)}")
        props = {str(name): {"type": str((p or {}).get("type") or ""), "id": (p or {}).get("id")}
                 for name, p in (payload.get("properties") or {}).items()}
        self.schema_cache[database_id] = (now, props)
        return props

    def fetch_rows(
        self,
        database: str,
        *,
        filter: Optional[Dict[str, Any]] = None,          # simple filter, see build_notion_filter
        raw_filter: Optional[Dict[str, Any]] = None,      # an already-shaped Notion filter
        edited_since: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> NotionRows:
        """Rows of one configured database, most recently edited first. Raises ``NotionError``.

        Follows Notion's cursor pagination (``next_cursor`` / ``has_more``, at most 100 per page)
        until ``limit`` rows are held, and reports ``truncated`` when more matched than that.
        """
        database_id = self.require_database(database)
        alias = self.alias_for(database_id)
        cap = max(1, int(limit or self.max_rows))
        clauses: List[Dict[str, Any]] = []
        if filter:
            try:
                built = build_notion_filter(self.schema(database_id), filter)
            except ValueError as exc:
                raise NotionError(str(exc)) from None
            if built:
                clauses.append(built)
        if raw_filter:
            clauses.append(raw_filter)
        if edited_since is not None:
            clauses.append({"timestamp": "last_edited_time",
                            "last_edited_time": {"on_or_after": minute_floor_iso(edited_since)}})
        body: Dict[str, Any] = {
            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        }
        if len(clauses) == 1:
            body["filter"] = clauses[0]
        elif clauses:
            body["filter"] = {"and": clauses}

        rows: List[NotionRow] = []
        cursor: Optional[str] = None
        truncated = False
        while True:
            page_body = dict(body)
            page_body["page_size"] = min(MAX_PAGE_SIZE, cap + 1 - len(rows))
            if cursor:
                page_body["start_cursor"] = cursor
            payload = self.request("POST", f"/databases/{dashed(database_id)}/query", page_body)
            for page in payload.get("results") or []:
                if page.get("object", "page") != "page":
                    continue
                row = parse_row(page, alias)
                if row.archived or not row.page_id:
                    continue
                self.page_database[row.page_id] = alias
                rows.append(row)
            cursor = payload.get("next_cursor") if payload.get("has_more") else None
            if len(rows) > cap:
                truncated = True
                rows = rows[:cap]
                break
            if not cursor:
                break
        return NotionRows(rows=rows, truncated=truncated)

    def cached_rows(self, alias: str) -> NotionRows:
        """All rows (up to the cap) of one database, reused for ``cache_ttl_seconds``."""
        now = time.time()
        cached = self.rows_cache.get(alias)
        if cached and (now - cached[0]) < self.cache_ttl:
            return cached[1]
        fetched = self.fetch_rows(alias)
        self.rows_cache[alias] = (now, fetched)
        return fetched

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def read_page(self, ident: Any, *, max_chars: Optional[int] = None) -> Tuple[NotionRow, str]:
        """``(row, text)`` for one page: properties, then block text. Raises ``NotionError``.

        Refuses a page whose parent is not one of the configured databases. The text is bounded
        by ``max_blocks`` blocks and ``max_chars`` characters, and says so when it was cut.
        """
        page_id = normalize_id(ident)
        if not page_id:
            raise NotionError(f"{str(ident)!r} is not a Notion page id or link")
        page = self.request("GET", f"/pages/{dashed(page_id)}")
        parent_id = normalize_id((page.get("parent") or {}).get("database_id"))
        alias = self.alias_for(parent_id) if parent_id else ""
        if not alias:
            raise NotionError("refused: that page is not in one of the configured databases")
        row = parse_row(page, alias)
        if row.archived:
            raise NotionError("that page is archived")
        budget = int(max_chars or self.max_page_chars)
        lines = [f"{row.title or '(untitled)'}  [{alias}]"]
        for key, value in row.properties.items():
            lines.append(f"{key}: {value}")
        if row.url:
            lines.append(f"link: {row.url}")
        lines.append("")
        blocks_read = 0
        cursor: Optional[str] = None
        while blocks_read < self.max_blocks:
            path = f"/blocks/{dashed(page_id)}/children?page_size={min(MAX_PAGE_SIZE, self.max_blocks - blocks_read)}"
            if cursor:
                path += "&start_cursor=" + urllib.parse.quote(cursor)
            payload = self.request("GET", path)
            for block in payload.get("results") or []:
                blocks_read += 1
                text = render_block(block)
                if text:
                    lines.append(text)
            cursor = payload.get("next_cursor") if payload.get("has_more") else None
            if not cursor:
                break
        more = bool(cursor)
        text = "\n".join(lines)
        if len(text) > budget:
            text = text[:budget].rsplit("\n", 1)[0] + "\n[truncated]"
        elif more:
            text += "\n[truncated: the page has more blocks than this read returns]"
        return row, text

    # ------------------------------------------------------------------
    # RetrievalAdapter interface
    # ------------------------------------------------------------------

    def unconfigured(self) -> str:
        if self.token_provider is None:
            return "notion not configured: no token_provider supplied"
        if not self.databases:
            return "notion not configured: no database_ids supplied"
        return ""

    def read_section(
        self,
        rel_path: str,
        *,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        heading: Optional[str] = None,
        max_bytes: Optional[int] = None,
    ) -> Observation:
        """Read one page (by id, link, or ``alias/id``): its properties and block text."""
        try:
            problem = self.unconfigured()
            if problem:
                return Observation(kind="error", rel_path=rel_path, error=problem)
            _row, text = self.read_page(rel_path, max_chars=max_bytes)
            if start_line or end_line:
                lines = text.split("\n")
                start = (start_line or 1) - 1
                end = end_line or len(lines)
                text = "\n".join(lines[max(0, start): min(len(lines), end)])
            return Observation(kind="read", rel_path=rel_path, text=text)
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", rel_path=rel_path, error=f"notion read error: {exc}")

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Search a regex across the rows of the configured databases. ``scope`` is one alias."""
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return Observation(kind="error", pattern=pattern, error=f"invalid regex: {exc}")
        try:
            problem = self.unconfigured()
            if problem:
                return Observation(kind="error", pattern=pattern, error=problem)
            aliases = list(self.databases)
            if scope:
                wanted = [a for a in aliases if a.lower() == str(scope).lower()]
                if not wanted:
                    return Observation(kind="error", pattern=pattern,
                                       error=f"scope {scope!r} is not a configured database ({', '.join(aliases)})")
                aliases = wanted
            hits: List[Dict[str, Any]] = []
            for alias in aliases:
                for number, row in enumerate(self.cached_rows(alias).rows, 1):
                    line = row_line(row)
                    if regex.search(line):
                        hits.append({"line": line, "line_number": number, "file": f"{alias}/{row.page_id}"})
                        if max_hits and len(hits) >= max_hits:
                            break
                if max_hits and len(hits) >= max_hits:
                    break
            if not hits:
                return Observation(kind="error", pattern=pattern, error=f"pattern not found: {pattern}")
            return Observation(kind="grep", pattern=pattern, hits=hits)
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", pattern=pattern, error=f"notion grep error: {exc}")

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Query one configured database.

        Spec keys:
          ``database``   alias (or configured id); optional when exactly one database is configured.
          ``filter``     ``{"Property": value | {"operator": value}}`` (see ``build_notion_filter``).
          ``raw_filter`` an already-shaped Notion filter object.
          ``query``/``q`` natural-language terms; rows are ranked by keyword overlap.
          ``limit``      rows to return (default 10, at most 50).
          ``page``       instead of a query: read this one page.
        """
        try:
            problem = self.unconfigured()
            if problem:
                return Observation(kind="error", error=problem)
            if spec.get("page"):
                _row, text = self.read_page(spec["page"])
                return Observation(kind="query", text=text, rel_path=f"notion:{normalize_id(spec['page'])}")
            name = spec.get("database") or spec.get("db")
            if not name:
                if len(self.databases) != 1:
                    return Observation(kind="error",
                                       error=f"name a database: one of {', '.join(sorted(self.databases))}")
                name = next(iter(self.databases))
            limit = max(1, min(50, int(spec.get("limit") or 10)))
            terms_text = str(spec.get("query") or spec.get("q") or "").strip()
            fetched = self.fetch_rows(
                str(name), filter=spec.get("filter") or None, raw_filter=spec.get("raw_filter") or None,
                limit=None if terms_text else limit)
            rows = fetched.rows
            if terms_text:
                terms = set(keywords_from_text(terms_text))
                scored = [(len(terms & set(keywords_from_text(row_line(r)))), r) for r in rows]
                rows = [r for score, r in sorted(scored, key=lambda s: -s[0]) if score > 0]
                if not rows:
                    return Observation(kind="error", error="no rows matched the query terms")
                fetched = NotionRows(rows=rows[:limit], truncated=fetched.truncated or len(rows) > limit)
                rows = fetched.rows
            if not rows:
                return Observation(kind="error", error="no rows matched")
            parts = [render_row(r) for r in rows]
            if fetched.truncated:
                parts.append(f"[more rows matched than the {len(rows)} shown]")
            return Observation(kind="query", text="\n\n".join(parts), rel_path=f"notion:{name}")
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"notion query error: {exc}")

    def make_locator(self, candidate: Any) -> Dict[str, Any]:
        """A ``notion_page`` locator ``{"page_id", "database"}`` for a page this adapter surfaced.

        ``candidate`` is a page id, a link, or a ``NotionRow``. ``{}`` when it holds no id. Never raises.
        """
        try:
            page_id = normalize_id(getattr(candidate, "page_id", None) or candidate)
            if not page_id:
                return {}
            return {"page_id": page_id, "database": self.page_database.get(page_id, "")}
        except Exception:  # noqa: BLE001
            return {}

    def resolve_reference(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> Optional[str]:
        """Re-fetch a learned ``notion_page`` reference FRESH through this adapter's own read path.

        Never a stale snapshot. ``None`` when the page is gone, outside the configured databases,
        or anything fails. Never raises.
        """
        try:
            page_id = normalize_id((locator or {}).get("page_id"))
            if not page_id:
                return None
            _row, text = self.read_page(page_id, max_chars=max_chars)
            return text or None
        except Exception:  # noqa: BLE001 -- a resolver must never raise
            return None

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        pass  # Read-only: rows and pages are authored in Notion, never by QAR.

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_sources(self) -> Observation:
        try:
            problem = self.unconfigured()
            if problem:
                return Observation(kind="query", locator="list_sources", text=problem)
            lines = [f"{alias}: Notion database ({database_id})"
                     for alias, database_id in sorted(self.databases.items())]
            return Observation(kind="query", locator="list_sources", text="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"notion list_sources error: {exc}")

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        try:
            database_id = self.require_database(name)
            props = self.schema(database_id)
            lines = [f"Notion database '{self.alias_for(database_id)}' has {len(props)} properties:"]
            lines.extend(f"  {key}: {meta.get('type')}" for key, meta in sorted(props.items()))
            return Observation(kind="query", locator=f"describe_source({name})", text="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"notion describe_source error: {exc}")

    def list_operations(self) -> Observation:
        return Observation(
            kind="query",
            locator="list_operations",
            text=(
                "notion_read: Read one page (properties and block text); pass its id or link as rel_path.\n"
                "notion_grep: Search a regex across the rows of the configured databases.\n"
                "notion_query: Filter or rank rows of one database; pass {\"database\": \"...\", \"filter\": {...}}."
            ),
        )

    def describe_operation(self, name: str) -> Observation:
        ops = {
            "notion_read": "notion_read: read_section(page id or link) -> properties and block text, bounded.",
            "notion_grep": "notion_grep: grep(pattern, scope=<database alias>, max_hits=N) -> matching rows.",
            "notion_query": ('notion_query: query({"database": "alias", "filter": {"Status": "In progress"}, '
                             '"query": "terms", "limit": 10}) -> matching rows.'),
        }
        text = ops.get((name or "").lower().replace("-", "_").replace(" ", "_"))
        if not text:
            return Observation(kind="error", error=f"NotionAdapter: unknown operation {name!r}.")
        return Observation(kind="query", locator=f"describe_operation({name})", text=text)
