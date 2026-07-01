"""GoogleChatAdapter -- RetrievalAdapter over Google Chat spaces, threads, and messages.

This lets the orchestrator brain ground answers in a workspace's Google Chat
conversations: it lists spaces, pulls recent messages, groups them into
conversations (by thread or by space), and exposes them through the same
``RetrievalAdapter`` surface every other source uses (read / grep / query +
discovery), plus the optional pre-flight ``assemble`` injection.

It is GENERIC by construction:

  * HTTP is stdlib-only (``urllib.request`` + ``json``) -- no third-party deps in
    the adapter itself, exactly like ``WebSearchAdapter``.
  * AUTH is injected. The adapter never knows how a bearer token is minted; it
    calls a ``token_provider()`` callable you supply. Ships two ready-made
    providers: ``static_token_provider`` (a token you already hold) and
    ``service_account_token_provider`` (a Google service-account JSON with
    domain-wide delegation, for a Google Workspace -- this one needs the optional
    ``[google]`` extra and is import-guarded so the module loads without it).
  * Every public method catches all exceptions and returns ``Observation(kind="error")``
    rather than raising, so a missing token, an unreachable endpoint, or a 403 from
    Chat never breaks the orchestrator loop.

Wire it into a CompositeRetrievalAdapter alongside the local corpus::

    from quest_ai_runner.adapters import (
        CompositeRetrievalAdapter, FilesAdapter, GoogleChatAdapter,
    )
    from quest_ai_runner.adapters.google_chat_adapter import service_account_token_provider

    chat = GoogleChatAdapter(
        token_provider=service_account_token_provider(
            service_account_file="/path/to/sa.json",
            subject="someone@your-workspace.org",   # domain-wide delegation
        ),
        lookback_days=14,
    )
    retrieval = CompositeRetrievalAdapter([FilesAdapter(corpus_root), chat])

Conversations are normalized into the SAME dict shape Claude transcripts use
(``{"messages": [{"role", "text"}], "updated_at": ...}``) so the shared
conversation-format and TF-DF-IDF helpers rank and render them with no special
casing.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ..core.adapters import AssembledContext, Observation, RetrievalAdapterBase
from .conversation_format import (
    conversation_digest,
    conversation_timestamp,
    conversation_to_text,
    resolve_conv_key,
)
from .tfdfidf_sampling import extract_terms, keywords_from_text, select_representatives

_log = logging.getLogger("quest-ai-runner.google-chat")

_CHAT_API_BASE = "https://chat.googleapis.com/v1"

# Read-only scopes sufficient to list spaces and read their messages.
DEFAULT_CHAT_SCOPES = (
    "https://www.googleapis.com/auth/chat.spaces.readonly",
    "https://www.googleapis.com/auth/chat.messages.readonly",
)

# A token provider returns a bearer access token string (or None when it cannot
# mint one -- the adapter then degrades to an error Observation rather than raising).
TokenProvider = Callable[[], Optional[str]]


# ---------------------------------------------------------------------------
# Token providers (auth is injected; the adapter stays dependency-free)
# ---------------------------------------------------------------------------

def static_token_provider(token: str) -> TokenProvider:
    """A token provider that always returns the same pre-minted bearer token.

    Useful when the host already holds a valid OAuth access token (e.g. it ran the
    user-consent flow itself, or pulls one from its own credential store).
    """
    def _provider() -> Optional[str]:
        return token or None
    return _provider


def service_account_token_provider(
    *,
    service_account_file: Optional[str] = None,
    service_account_info: Optional[Dict[str, Any]] = None,
    subject: Optional[str] = None,
    scopes: Optional[List[str]] = None,
) -> TokenProvider:
    """Build a token provider from a Google service-account credential.

    This is the Google Workspace path: a service-account JSON with **domain-wide
    delegation** enabled, impersonating ``subject`` (a Workspace user the delegation
    is authorized for) so the adapter can read the spaces that user can see.

    Requires the optional ``[google]`` extra (``google-auth``). The import is done
    lazily inside the returned provider so importing this module never needs it; a
    missing dependency surfaces as a logged warning and a ``None`` token (the
    adapter then reports a clean "not configured" error, never a crash).

    Args:
        service_account_file: Path to the service-account JSON key file.
        service_account_info: Already-parsed service-account dict (alternative to the file).
        subject: Workspace user to impersonate via domain-wide delegation.
        scopes: OAuth scopes to request (defaults to read-only spaces + messages).
    """
    use_scopes = list(scopes) if scopes else list(DEFAULT_CHAT_SCOPES)
    _state: Dict[str, Any] = {"creds": None}

    def _provider() -> Optional[str]:
        try:
            creds = _state["creds"]
            if creds is None:
                from google.oauth2 import service_account as _sa  # type: ignore

                if service_account_info is not None:
                    creds = _sa.Credentials.from_service_account_info(
                        service_account_info, scopes=use_scopes
                    )
                elif service_account_file:
                    creds = _sa.Credentials.from_service_account_file(
                        service_account_file, scopes=use_scopes
                    )
                else:
                    _log.warning(
                        "service_account_token_provider: neither service_account_file "
                        "nor service_account_info supplied; Google Chat disabled"
                    )
                    return None
                if subject:
                    creds = creds.with_subject(subject)
                _state["creds"] = creds

            # Refresh if needed (google-auth tracks expiry on the credential).
            if not creds.valid:
                from google.auth.transport.requests import Request as _Request  # type: ignore

                creds.refresh(_Request())
            return creds.token
        except ImportError:
            _log.warning(
                "Google Chat needs the [google] extra (google-auth) for a service "
                "account; install it or pass a static token_provider instead"
            )
            return None
        except Exception as exc:  # noqa: BLE001
            _log.debug("service-account token mint failed: %s", exc)
            return None

    return _provider


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class GoogleChatAdapter(RetrievalAdapterBase):
    """RetrievalAdapter over Google Chat conversations.

    Lists spaces (or a configured subset), pulls recent messages bounded by
    ``lookback_days`` / ``max_messages_per_space``, groups them into conversations
    (``group_by="thread"`` by default, or ``"space"``), and serves them through the
    standard retrieval surface. Fetched data is cached for ``cache_ttl_seconds`` so
    a single planner loop's repeated read/grep calls hit the network once.
    """

    def __init__(
        self,
        token_provider: Optional[TokenProvider] = None,
        *,
        space_names: Optional[List[str]] = None,
        group_by: str = "thread",
        lookback_days: Optional[int] = 30,
        max_spaces: int = 50,
        max_messages_per_space: int = 200,
        cache_ttl_seconds: float = 300.0,
        timeout_seconds: float = 20.0,
        api_base: str = _CHAT_API_BASE,
    ) -> None:
        """
        Args:
            token_provider: Callable returning a bearer access token (see the
                ``*_token_provider`` helpers). If None, the adapter is "unconfigured"
                and every method returns a clean error Observation.
            space_names: Explicit space resource names ("spaces/AAAA") to read. When
                None, the adapter lists spaces it can see via the API.
            group_by: "thread" (one conversation per Chat thread, the natural unit) or
                "space" (one conversation per space, all messages flattened).
            lookback_days: Only pull messages newer than this many days (None = no bound).
            max_spaces: Cap on spaces enumerated when ``space_names`` is None.
            max_messages_per_space: Cap on messages pulled per space.
            cache_ttl_seconds: How long a fetch is reused before re-hitting the API.
            timeout_seconds: Per-request HTTP timeout.
            api_base: Chat REST base URL (override only for testing / proxies).
        """
        self._token_provider = token_provider
        self._space_names = list(space_names) if space_names else None
        self._group_by = "space" if str(group_by).lower() == "space" else "thread"
        self._lookback_days = lookback_days
        self._max_spaces = max(1, int(max_spaces))
        self._max_messages_per_space = max(1, int(max_messages_per_space))
        self._cache_ttl = max(0.0, float(cache_ttl_seconds))
        self._timeout = float(timeout_seconds)
        self._api_base = api_base.rstrip("/")

        # Cache populated by _ensure_loaded().
        self._conversations: Dict[str, Dict[str, Any]] = {}
        self._conv_id_lookup: Dict[str, str] = {}
        self._loaded_at: float = 0.0

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get_json(self, url: str, token: str) -> Dict[str, Any]:
        """GET a Chat API URL with a bearer token; return the parsed JSON. May raise."""
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)

    def _build_url(self, path: str, params: Optional[Dict[str, Any]] = None) -> str:
        base = f"{self._api_base}/{path.lstrip('/')}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                return f"{base}?{urllib.parse.urlencode(clean)}"
        return base

    # ------------------------------------------------------------------
    # Fetch + normalize
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """Refresh the conversation cache if empty or past its TTL. Never raises."""
        fresh = self._loaded_at > 0 and (_time.time() - self._loaded_at) < self._cache_ttl
        if fresh and self._conversations:
            return
        try:
            self._load()
        except Exception as exc:  # noqa: BLE001
            _log.debug("google chat load failed: %s", exc)
            # Keep any previously good cache rather than wiping it on a transient error.
            if not self._conversations:
                self._conversations, self._conv_id_lookup = {}, {}
        self._loaded_at = _time.time()

    def _load(self) -> None:
        if self._token_provider is None:
            self._conversations, self._conv_id_lookup = {}, {}
            return
        token = self._token_provider()
        if not token:
            self._conversations, self._conv_id_lookup = {}, {}
            return

        space_names = self._space_names or self._list_space_names(token)
        cutoff = None
        if self._lookback_days is not None:
            cutoff = _time.time() - self._lookback_days * 86400.0

        # group_key -> conversation dict
        convs: Dict[str, Dict[str, Any]] = {}
        for space in space_names[: self._max_spaces]:
            space_display = self._space_display.get(space, space) if hasattr(self, "_space_display") else space
            for msg in self._list_messages(space, token):
                ts = _parse_rfc3339(msg.get("createTime"))
                if cutoff is not None and ts and ts < cutoff:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue  # skip attachment-only / system membership events with no body

                if self._group_by == "space":
                    key = space
                    title = space_display
                else:
                    thread = (msg.get("thread") or {}).get("name") or space
                    key = thread
                    title = space_display

                conv = convs.get(key)
                if conv is None:
                    conv = {
                        "id": key,
                        "space": space,
                        "rep_name": title,          # surfaces in conversation_metadata()
                        "messages": [],
                        "updated_at": 0.0,
                    }
                    convs[key] = conv

                sender = msg.get("sender") or {}
                role = sender.get("displayName") or sender.get("name") or sender.get("type") or "user"
                conv["messages"].append({"role": role, "text": text, "create_time": msg.get("createTime")})
                if ts and ts > conv["updated_at"]:
                    conv["updated_at"] = ts

        # Order each conversation's messages chronologically and stamp turn_count.
        for conv in convs.values():
            conv["messages"].sort(key=lambda m: _parse_rfc3339(m.get("create_time")) or 0.0)
            conv["turn_count"] = len(conv["messages"])

        # Drop any that ended up empty after filtering.
        convs = {k: c for k, c in convs.items() if c["messages"]}

        lookup: Dict[str, str] = {}
        for key in convs:
            lookup[key.split("/")[-1]] = key  # tail (thread/space id) -> full key

        self._conversations = convs
        self._conv_id_lookup = lookup

    def _list_space_names(self, token: str) -> List[str]:
        """List space resource names visible to the credential. Best-effort, paginated."""
        names: List[str] = []
        self._space_display: Dict[str, str] = {}
        page_token: Optional[str] = None
        while True:
            url = self._build_url("spaces", {"pageSize": 100, "pageToken": page_token})
            data = self._get_json(url, token)
            for sp in data.get("spaces", []) or []:
                name = sp.get("name")
                if not name:
                    continue
                names.append(name)
                self._space_display[name] = sp.get("displayName") or name
            page_token = data.get("nextPageToken")
            if not page_token or len(names) >= self._max_spaces:
                break
        return names

    def _list_messages(self, space: str, token: str) -> List[Dict[str, Any]]:
        """List recent messages for one space, newest first, bounded. Best-effort, paginated."""
        out: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        # Server-side time filter when we have a lookback window (RFC3339, UTC).
        msg_filter = None
        if self._lookback_days is not None:
            cutoff = _time.time() - self._lookback_days * 86400.0
            msg_filter = f'createTime > "{_to_rfc3339(cutoff)}"'
        while True:
            url = self._build_url(
                f"{space}/messages",
                {
                    "pageSize": 100,
                    "pageToken": page_token,
                    "orderBy": "createTime desc",
                    "filter": msg_filter,
                },
            )
            data = self._get_json(url, token)
            batch = data.get("messages", []) or []
            out.extend(batch)
            page_token = data.get("nextPageToken")
            if not page_token or len(out) >= self._max_messages_per_space:
                break
        return out[: self._max_messages_per_space]

    # ------------------------------------------------------------------
    # Ranking helpers (shared with the Claude-conversations adapter)
    # ------------------------------------------------------------------

    def _recency_boost(self, cid: str) -> float:
        """Multiplicative recency boost for a conversation. Half-life = 7 days."""
        import math
        ts = conversation_timestamp(self._conversations[cid])
        if ts <= 0:
            return 1.0
        days_old = (_time.time() - ts) / 86400.0
        return 1.0 + math.exp(-days_old * math.log(2) / 7.0)

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
        """Read one conversation transcript by id (thread/space resource name or its tail)."""
        try:
            self._ensure_loaded()
            if not self._conversations:
                return Observation(kind="error", rel_path=rel_path, error=self._unconfigured_or_empty())

            key = resolve_conv_key(rel_path, self._conversations, self._conv_id_lookup)
            if key is None:
                return Observation(kind="error", rel_path=rel_path, error=f"conversation not found: {rel_path}")

            text = conversation_to_text(self._conversations[key])
            if start_line or end_line:
                lines = text.split("\n")
                start = (start_line or 1) - 1
                end = end_line or len(lines)
                text = "\n".join(lines[max(0, start): min(len(lines), end)])
            if max_bytes and len(text) > max_bytes:
                text = text[:max_bytes].rsplit("\n", 1)[0] + "\n[truncated]"

            return Observation(kind="read", rel_path=rel_path, text=text)
        except Exception as exc:  # noqa: BLE001
            _log.debug("google chat read_section failed for %r: %s", rel_path, exc)
            return Observation(kind="error", rel_path=rel_path, error=f"google chat read error: {exc}")

    def grep(
        self, pattern: str, *, scope: Optional[str] = None, max_hits: Optional[int] = None
    ) -> Observation:
        """Search a regex across all loaded Chat conversations. ``scope`` (optional) limits to a
        conversation id / space whose key contains the scope string."""
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return Observation(kind="error", pattern=pattern, error=f"invalid regex: {e}")
        try:
            self._ensure_loaded()
            if not self._conversations:
                return Observation(kind="error", pattern=pattern, error=self._unconfigured_or_empty())

            hits: List[Dict[str, Any]] = []
            for cid, conv in self._conversations.items():
                if scope and scope not in cid and scope not in str(conv.get("space", "")):
                    continue
                for i, line in enumerate(conversation_to_text(conv).split("\n"), 1):
                    if regex.search(line):
                        hits.append({"line": line, "line_number": i, "file": cid})
                        if max_hits and len(hits) >= max_hits:
                            break
                if max_hits and len(hits) >= max_hits:
                    break

            if not hits:
                return Observation(kind="error", pattern=pattern, error=f"pattern not found: {pattern}")
            return Observation(kind="grep", pattern=pattern, hits=hits)
        except Exception as exc:  # noqa: BLE001
            _log.debug("google chat grep failed for %r: %s", pattern, exc)
            return Observation(kind="error", pattern=pattern, error=f"google chat grep error: {exc}")

    def query(self, spec: Dict[str, Any]) -> Observation:
        """Relevance-select conversations for a query and return their transcripts.

        Spec keys:
          ``query`` / ``q``      -- natural-language query (selects by keyword overlap; optional).
          ``samples``            -- how many conversations to return (default 4).
          ``space``              -- optional space resource name to restrict to.
        """
        try:
            self._ensure_loaded()
            if not self._conversations:
                return Observation(kind="error", error=self._unconfigured_or_empty())

            space = spec.get("space")
            cids = [
                c for c in self._conversations
                if not space or self._conversations[c].get("space") == space or space in c
            ]
            if not cids:
                return Observation(kind="error", error="no conversations match the query scope")

            samples = int(spec.get("samples", 4) or 4)
            query_text = str(spec.get("query") or spec.get("q") or "").strip()

            if query_text:
                query_terms = set(keywords_from_text(query_text))
                conv_kw = {c: set(keywords_from_text(conversation_digest(self._conversations[c]))) for c in cids}
                overlapping = [c for c in cids if conv_kw[c] & query_terms] or cids
                selected = select_representatives(
                    items=overlapping,
                    get_terms=lambda c: conv_kw.get(c) or extract_terms(conversation_digest(self._conversations[c])),
                    samples_per_group=samples,
                    get_score_boost=self._recency_boost,
                )
            else:
                selected = select_representatives(
                    items=cids,
                    get_terms=lambda c: extract_terms(conversation_digest(self._conversations[c])),
                    samples_per_group=samples,
                    get_score_boost=self._recency_boost,
                )

            if not selected:
                return Observation(kind="error", error="no conversations selected after sampling")

            parts = []
            for cid in selected[:samples]:
                parts.append(f"=== Conversation: {cid} ===\n{conversation_to_text(self._conversations[cid])}")
            return Observation(kind="query", text="\n\n".join(parts), rel_path=f"google_chat:{query_text[:60]}")
        except Exception as exc:  # noqa: BLE001
            _log.debug("google chat query failed: %s", exc)
            return Observation(kind="error", error=f"google chat query error: {exc}")

    # ------------------------------------------------------------------
    # Pre-flight ContextAssembler interface (optional, same as the Claude adapter)
    # ------------------------------------------------------------------

    def assemble(
        self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, on_event: Optional[Any] = None
    ) -> AssembledContext:
        """Inject digests of Chat conversations relevant to ``task_text``. Never raises."""
        try:
            self._ensure_loaded()
            if not self._conversations:
                return AssembledContext()

            query_terms = set(keywords_from_text(task_text))
            conv_kw = {
                cid: set(keywords_from_text(conversation_digest(conv)))
                for cid, conv in self._conversations.items()
            }
            overlapping = [cid for cid, kw in conv_kw.items() if kw & query_terms]
            if not overlapping:
                return AssembledContext()

            selected = select_representatives(
                items=overlapping,
                get_terms=lambda cid: conv_kw[cid],
                samples_per_group=4,
                get_score_boost=self._recency_boost,
            )
            if not selected:
                return AssembledContext()

            lines = ["--- RELEVANT GOOGLE CHAT CONVERSATIONS ---"]
            for cid in selected:
                conv = self._conversations[cid]
                label = conv.get("rep_name") or cid
                lines.append(f"[chat: {label} ({cid})]")
                lines.append(conversation_digest(conv))
            return AssembledContext(
                context_view="\n".join(lines),
                sources=[{"adapter": "google_chat", "label": "google chat", "items": list(selected)}],
            )
        except Exception:  # noqa: BLE001
            return AssembledContext()

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        pass  # Chat is read-only here; messages are authored in Google Chat, not by QAR.

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_sources(self) -> Observation:
        try:
            self._ensure_loaded()
            if not self._conversations:
                return Observation(kind="query", locator="list_sources", text=self._unconfigured_or_empty())
            lines = []
            for cid in sorted(self._conversations.keys()):
                conv = self._conversations[cid]
                lines.append(f"{cid}: Google Chat ({conv.get('rep_name', '')}, {conv.get('turn_count', 0)} msgs)")
            return Observation(kind="query", locator="list_sources", text="\n".join(lines))
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"google chat list_sources error: {exc}")

    def describe_source(self, name: str, *, path: Optional[str] = None) -> Observation:
        try:
            self._ensure_loaded()
            key = resolve_conv_key(name, self._conversations, self._conv_id_lookup)
            if key is None:
                return Observation(kind="error", error=f"conversation not found: {name}")
            conv = self._conversations[key]
            return Observation(
                kind="query",
                locator=f"describe_source({name})",
                text=(
                    f"Google Chat conversation '{key}' in space {conv.get('space')} "
                    f"({conv.get('rep_name')}) with {conv.get('turn_count', 0)} messages."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return Observation(kind="error", error=f"google chat describe_source error: {exc}")

    def list_operations(self) -> Observation:
        return Observation(
            kind="query",
            locator="list_operations",
            text=(
                "chat_read: Read one Google Chat conversation transcript by id (pass it as rel_path).\n"
                "chat_grep: Search a regex across all loaded Chat conversations.\n"
                "chat_query: Relevance-select conversations for a query; pass {\"query\": \"...\"}."
            ),
        )

    def describe_operation(self, name: str) -> Observation:
        ops = {
            "chat_read": "chat_read: read_section(conversation_id) -> transcript text.",
            "chat_grep": "chat_grep: grep(pattern, scope=<conv id or space>, max_hits=N) -> matching lines.",
            "chat_query": 'chat_query: query({"query": "terms", "samples": 4, "space": "spaces/AAA"}) -> selected transcripts.',
        }
        text = ops.get((name or "").lower().replace("-", "_").replace(" ", "_"))
        if not text:
            return Observation(kind="error", error=f"GoogleChatAdapter: unknown operation {name!r}.")
        return Observation(kind="query", locator=f"describe_operation({name})", text=text)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _unconfigured_or_empty(self) -> str:
        if self._token_provider is None:
            return "google chat not configured: no token_provider supplied"
        if self._token_provider() is None:
            return "google chat not configured: token_provider returned no token"
        return "no Google Chat conversations available in the configured window"


# ---------------------------------------------------------------------------
# Small time helpers (RFC3339 <-> epoch), stdlib-only
# ---------------------------------------------------------------------------

def _parse_rfc3339(value: Any) -> float:
    """Parse an RFC3339 timestamp (e.g. '2026-06-20T12:34:56.789Z') to epoch seconds, or 0.0."""
    if not value or not isinstance(value, str):
        return 0.0
    try:
        from datetime import datetime, timezone

        s = value.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return 0.0


def _to_rfc3339(epoch: float) -> str:
    """Format epoch seconds as an RFC3339 UTC timestamp ('...Z') for the Chat API filter."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
