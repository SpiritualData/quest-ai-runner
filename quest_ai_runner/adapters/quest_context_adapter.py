"""QuestContextAdapter -- HTTP client for the /api/quest-context/resolve hub endpoint.

Exposes the Quest context hub as:
  (a) a direct ``resolve(query, ...)`` call that returns the merged context string, and
  (b) a set of ``ReferenceResolver`` implementations for the ``collection`` and ``query``
      content-item types, assembled by ``build_quest_resolvers(adapter)``.  These let any
      out-of-process QAR (or a cockpit Orchestrator) resolve Quest collection and query
      references through the hub instead of falling back to unresolved-pointer placeholders.

Generic by construction: no org name, real ids, absolute paths, or PII are baked in.
All specifics (base URL, API key, user/team scope) come from constructor args or env vars.

Env vars (read by ``_get_base_url`` / ``_get_api_key``):
  QUEST_API_URL   -- preferred name (same as the Quest backend's env var)
  QUEST_BASE_URL  -- fallback alias accepted for compatibility
  QUEST_API_KEY   -- bearer token (a ``qsk_...`` issued by the Quest backend)

HTTP is stdlib-only (``urllib.request`` + ``json``); no third-party deps.
All I/O is synchronous and bounded by ``timeout`` (default 10 s).
``resolve()`` NEVER raises; it returns ``""`` on any failure so a bad pointer or
network hiccup can never break a context-assembly pass.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Env-var helpers (no org specifics; just read the two canonical var names)
# ---------------------------------------------------------------------------

def _get_base_url() -> str:
    """Return the Quest base URL, preferring QUEST_API_URL over QUEST_BASE_URL."""
    return (os.getenv("QUEST_API_URL") or os.getenv("QUEST_BASE_URL") or "").rstrip("/")


def _get_api_key() -> str:
    """Return the Quest API key from QUEST_API_KEY."""
    return os.getenv("QUEST_API_KEY") or ""


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class QuestContextAdapter:
    """HTTP client for ``POST /api/quest-context/resolve``.

    Parameterized by ``base_url`` + ``api_key`` plus optional default scope fields
    (``default_user_id``, ``default_quest_ids``, ``default_team_id``).  Per-call overrides
    take precedence over the defaults.

    Construction is cheap; build one instance per consumer and reuse it.
    Never raises from ``resolve()``; returns ``""`` on any error.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        default_user_id: Optional[str] = None,
        default_quest_ids: Optional[List[str]] = None,
        default_team_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._default_user_id = default_user_id
        self._default_quest_ids = list(default_quest_ids) if default_quest_ids else None
        self._default_team_id = default_team_id
        self._timeout = float(timeout)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(
        cls,
        *,
        default_user_id: Optional[str] = None,
        default_quest_ids: Optional[List[str]] = None,
        default_team_id: Optional[str] = None,
        timeout: float = 10.0,
    ) -> "QuestContextAdapter":
        """Build from QUEST_API_URL (or QUEST_BASE_URL) and QUEST_API_KEY env vars."""
        return cls(
            base_url=_get_base_url(),
            api_key=_get_api_key(),
            default_user_id=default_user_id,
            default_quest_ids=default_quest_ids,
            default_team_id=default_team_id,
            timeout=timeout,
        )

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        """True when both base_url and api_key are non-empty."""
        return bool(self._base_url and self._api_key)

    # ------------------------------------------------------------------
    # Core resolve
    # ------------------------------------------------------------------

    def resolve(
        self,
        query: str,
        *,
        user_id: Optional[str] = None,
        quest_ids: Optional[List[str]] = None,
        team_id: Optional[str] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        """POST to ``/api/quest-context/resolve`` and return the merged context string.

        Per-call args override the instance defaults.  Returns ``""`` when:
        - the adapter is not configured (no base_url or api_key),
        - the hub returns an error, or
        - any network / JSON error occurs.
        Never raises.
        """
        if not self.configured:
            return ""
        try:
            body: Dict[str, Any] = {"query": query or ""}
            uid = user_id or self._default_user_id
            if uid:
                body["user_id"] = uid
            qids = quest_ids if quest_ids is not None else self._default_quest_ids
            if qids:
                body["quest_ids"] = list(qids)
            tid = team_id or self._default_team_id
            if tid:
                body["team_id"] = tid
            if max_chars is not None:
                body["max_chars"] = int(max_chars)

            return self._post(body)
        except Exception:  # noqa: BLE001 -- resolve must never propagate
            return ""

    # ------------------------------------------------------------------
    # Internal HTTP
    # ------------------------------------------------------------------

    def _post(self, body: Dict[str, Any]) -> str:
        """POST ``body`` to the hub and return the ``context`` field (or ``""``)."""
        url = f"{self._base_url}/api/quest-context/resolve"
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {self._api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                result = json.loads(raw) if raw else {}
                return str(result.get("context") or "")
        except urllib.error.HTTPError as exc:
            # Swallow -- caller gets ""
            _ = exc.read()
            return ""
        except Exception:  # noqa: BLE001 -- timeout, URLError, JSON parse error, etc.
            return ""


# ---------------------------------------------------------------------------
# ReferenceResolver implementations (consumer-injected into RunnerConfig)
# ---------------------------------------------------------------------------

class _QuestCollectionResolver:
    """Resolve a ``collection`` content-item reference via the Quest context hub.

    The locator dict may carry ``name``/``collection`` (the collection name) and/or
    ``query`` (a topic query within that collection).  Both are combined into the hub
    query so the hub can route and filter appropriately.  Never raises.
    """

    def __init__(self, adapter: QuestContextAdapter) -> None:
        self._adapter = adapter

    def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
        try:
            loc = locator or {}
            name = str(loc.get("name") or loc.get("collection") or "").strip()
            query = str(loc.get("query") or loc.get("text") or "").strip()
            if not (name or query):
                return ""
            # Combine name + query into a single hub query.
            combined = f"collection:{name} {query}".strip() if name else query
            return self._adapter.resolve(combined, max_chars=max_chars) or ""
        except Exception:  # noqa: BLE001 -- resolvers must never raise
            return ""


class _QuestQueryResolver:
    """Resolve a ``query`` content-item reference via the Quest context hub.

    The locator dict carries ``query`` or ``text`` as the query string.  Never raises.
    """

    def __init__(self, adapter: QuestContextAdapter) -> None:
        self._adapter = adapter

    def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
        try:
            loc = locator or {}
            query = str(loc.get("query") or loc.get("text") or "").strip()
            if not query:
                return ""
            return self._adapter.resolve(query, max_chars=max_chars) or ""
        except Exception:  # noqa: BLE001 -- resolvers must never raise
            return ""


# ---------------------------------------------------------------------------
# Factory for RunnerConfig.reference_resolvers
# ---------------------------------------------------------------------------

def build_quest_resolvers(adapter: QuestContextAdapter) -> Dict[str, Any]:
    """Build a ``{type: ReferenceResolver}`` dict for wiring into ``RunnerConfig.reference_resolvers``.

    Returns resolvers for ``collection`` and ``query`` types that delegate to the Quest
    context hub via ``adapter``.  Conversation type is intentionally excluded; conversation
    history stays in the wired ``ConversationStore``.

    Usage::

        adapter = QuestContextAdapter.from_env()
        cfg = RunnerConfig(reference_resolvers=build_quest_resolvers(adapter), ...)

    Or merge with existing resolvers::

        existing = cfg.reference_resolvers or {}
        cfg.reference_resolvers = {**existing, **build_quest_resolvers(adapter)}
    """
    return {
        "collection": _QuestCollectionResolver(adapter),
        "query": _QuestQueryResolver(adapter),
    }
