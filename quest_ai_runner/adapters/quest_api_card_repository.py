"""QuestApiCardRepository -- CardRepository backed by quest-backend's card API.

Use this when QAR is connected to Quest AI (QAR_QUEST_API_URL + QAR_QUEST_API_KEY
are set) so that durable context cards are persisted centrally in quest-backend's
Qdrant store rather than locally. Cards include an inline preview so Quest AI can
use them immediately without a round-trip to the local machine.

Env vars consumed by the factory (read in config.py, passed in at construction):
  QAR_QUEST_API_URL   -- Quest backend base URL (e.g. https://quest.example.com)
  QAR_QUEST_API_KEY   -- Bearer token for the quest-backend card API
  QAR_USER_ID         -- The Quest user ID (MongoDB ObjectId string) whose cards to manage
  QAR_RUNNER_ENV_ID   -- Identifies this local runner environment (default: hostname)
  QAR_TEAM_ID         -- Quest team this runner is registered on; enables hub fan-out for full context

All methods are best-effort and NEVER raise: a reader returns None / [] / False on
any failure so the FileContextStore degrades gracefully.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

_log = logging.getLogger("quest-ai-runner.quest_api_cards")

# Card content-type detection source-types.
_SOURCE_TURN = "turn_card"
_SOURCE_CONVO = "conversation_ref"
_SOURCE_FILE = "file_context"
_SOURCE_GENERIC = "context_card"


def _runner_env_id() -> str:
    """The runner env identifier: QAR_RUNNER_ENV_ID or the system hostname."""
    return (os.getenv("QAR_RUNNER_ENV_ID") or "").strip() or socket.gethostname()


def _detect_source_type(card: Dict[str, Any]) -> str:
    """Detect the source type of a card for the local_preview content item."""
    if card.get("user") and card.get("assistant_summary"):
        return _SOURCE_TURN
    items = card.get("content_items") or []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "conversation":
            return _SOURCE_CONVO
    if card.get("files_consulted"):
        return _SOURCE_FILE
    return _SOURCE_GENERIC


def _extract_preview(card: Dict[str, Any], max_chars: int = 3000) -> str:
    """Extract a useful preview string from a card dict."""
    # Turn cards: user question + assistant summary.
    user = str(card.get("user") or "")
    ai = str(card.get("assistant_summary") or "")
    if user or ai:
        parts = []
        if user:
            parts.append(f"User: {user}")
        if ai:
            parts.append(f"AI: {ai}")
        return "\n".join(parts)[:max_chars]
    # Generic cards: description, summary, or name.
    text = card.get("description") or card.get("summary") or card.get("name") or ""
    return str(text)[:max_chars]


def _runner_team_id() -> str:
    """The team this runner is registered on: QAR_TEAM_ID env var, or empty string."""
    return (os.getenv("QAR_TEAM_ID") or "").strip()


def _enrich_card(card: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Return a copy of card enriched with source metadata and a local_fetch content item.

    The local_fetch item carries:
    - An inline preview (immediate use, zero round-trip)
    - team_id + runner_env_id so Quest AI's LocalFetchReferenceResolver can fan out
      to this runner via the quest-context hub for full context when needed
    - query so the hub knows what to ask the runner to resolve
    """
    enriched = dict(card)
    env_id = _runner_env_id()
    team_id = _runner_team_id()
    source_type = _detect_source_type(card)
    preview = _extract_preview(card)
    # query: the user's original question is the most useful retrieval signal
    query = (
        str(card.get("user") or "")
        or str(card.get("name") or "")
        or str(card.get("description") or "")
    )[:500]

    enriched["source_environment"] = "local"
    enriched["runner_env_id"] = env_id
    if team_id:
        enriched["team_id"] = team_id

    # local_fetch: Quest AI's LocalFetchReferenceResolver tries the quest-context hub
    # first (fanning out a context-request task to this runner env), falling back to
    # the inline preview when the runner is offline or the hub times out.
    fetch_item: Dict[str, Any] = {
        "type": "local_fetch",
        "preview": preview,
        "source_type": source_type,
        "runner_env_id": env_id,
        "team_id": team_id,
        "query": query,
        "source_locator": {
            "card_id": card.get("id") or card.get("card_id") or "",
            "user_id": user_id,
        },
    }
    existing = list(card.get("content_items") or [])
    # Idempotent: replace any prior local_fetch / local_preview item on re-write.
    existing = [
        i for i in existing
        if isinstance(i, dict) and i.get("type") not in ("local_fetch", "local_preview")
    ]
    existing.append(fetch_item)
    enriched["content_items"] = existing
    return enriched


def _cards_by_id(payload: Any) -> Dict[str, Dict[str, Any]]:
    """Normalize a ``cards`` reply into ``{card_id: card_dict}``.

    The API answers with a LIST of cards; older deployments answered with the
    repository's native mapping. Both are accepted so this adapter works against a
    backend of either vintage, and anything else (a null, a string) reads as no
    cards rather than raising into the context store.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if isinstance(payload, dict):
        items = payload.items()
    elif isinstance(payload, list):
        items = [(None, card) for card in payload]
    else:
        return out
    for card_id, card in items:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("id") or card_id or "").strip()
        if not cid:
            continue
        if not card.get("id"):
            card = {**card, "id": cid}
        out[cid] = card
    return out


# How long a cards listing may be reused. revision() and load_all() run back to back
# on every context-store read, so this mostly exists to make that ONE request rather
# than two; it is deliberately short, since another process may write at any time.
_LISTING_TTL_S = 5.0

# The card API HIDES auto-maintained cards (anything carrying ``managed_by``, e.g. the one card
# per quest) from its list and search replies unless the caller opts in, because those cards would
# clutter the user-facing Topics list with entries nobody made and cannot usefully edit. A runner
# is not that surface: it is a GROUNDING consumer, and the per-quest cards are exactly the context
# it needs, so it asks for the full store on every read. Without this the keyword arm and the
# vector arm both come back with zero quest cards the moment a backend with that default deploys,
# which looks identical to the per-quest cards never having worked at all.
_INCLUDE_MANAGED = {"include_managed": "true"}


class QuestApiCardRepository:
    """CardRepository backed by quest-backend's /api/cards HTTP API.

    Implements the same protocol as FilesystemCardRepository and QdrantCardRepository:
      write(card_id, card) -> bool
      read(card_id) -> Optional[dict]
      load_all() -> Dict[str, dict]
      delete(card_id) -> bool
      search_cards(query, *, limit=10) -> Optional[Dict[str, dict]]

    All methods catch every exception and return a safe default so the FileContextStore
    degrades gracefully on network failures.
    """

    def __init__(self, base_url: str, api_key: str, user_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        # Bumped on every write/delete by THIS process, so our own change is visible
        # to the context store immediately rather than after the listing TTL.
        self._local_rev = 0
        self._listing: Optional[Dict[str, Dict[str, Any]]] = None
        self._listing_at = 0.0

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "X-User-Id": self.user_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """Make an HTTP request; return the parsed JSON body or None on failure."""
        url = self._url(path)
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data: Optional[bytes] = None
        if body is not None:
            data = json.dumps(body, default=str).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            body_text = ""
            try:
                body_text = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            _log.debug(
                "QuestApiCardRepository: HTTP %s %s %s -> %d %s",
                method, path, params or "", exc.code, body_text,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository: request failed %s %s: %s", method, path, exc)
            return None

    def write(self, card_id: str, card: Dict[str, Any]) -> bool:
        """Upsert a card to the quest-backend card API. Returns True on success."""
        try:
            enriched = _enrich_card(card, self.user_id)
            result = self._request("PUT", f"/api/cards/{urllib.parse.quote(card_id, safe='')}", body=enriched)
            self._invalidate_listing()
            if result is None:
                return False
            return bool(result.get("ok", False))
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository.write failed for %s: %s", card_id, exc)
            return False

    def read(self, card_id: str) -> Optional[Dict[str, Any]]:
        """Read a card by id. Returns the card dict or None if not found."""
        try:
            result = self._request("GET", f"/api/cards/{urllib.parse.quote(card_id, safe='')}")
            return result if isinstance(result, dict) else None
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository.read failed for %s: %s", card_id, exc)
            return None

    def _fetch_all(self, max_age_s: float = 0.0) -> Dict[str, Dict[str, Any]]:
        """The user's cards as ``{card_id: card}``, reusing a listing younger than ``max_age_s``.

        ``revision()`` and ``load_all()`` run back to back on every store read, and
        both need the same listing, so the second one reuses the first one's reply.
        """
        if (
            self._listing is not None
            and max_age_s > 0
            and (time.monotonic() - self._listing_at) < max_age_s
        ):
            return self._listing
        result = self._request("GET", "/api/cards", params=dict(_INCLUDE_MANAGED))
        cards = _cards_by_id(result.get("cards")) if isinstance(result, dict) else {}
        self._listing = cards
        self._listing_at = time.monotonic()
        return cards

    def _invalidate_listing(self) -> None:
        """Drop the cached listing and bump the local stamp after our own write."""
        self._listing = None
        self._local_rev += 1

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Load all cards for the user as ``{card_id: card_dict}``; ``{}`` on any error.

        The MAPPING is the CardRepository protocol (see ``card_repository.py``), and
        it is what ``FileContextStore`` calls ``.values()`` on. This used to return a
        LIST, which broke the store outright, and worse: the API's own reply carried
        the cards under a ``cards`` key that was itself a mapping at the time, so
        ``list(result["cards"])`` yielded card IDS rather than cards. Every read
        through this repository was therefore either an exception the store swallowed
        or a list of strings, which is why cards written here never came back.
        Both reply shapes are accepted, since a deployment can be of either vintage.
        """
        try:
            return self._fetch_all(max_age_s=_LISTING_TTL_S)
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository.load_all failed: %s", exc)
            return {}

    def delete(self, card_id: str) -> bool:
        """Delete a card by id. Returns True on success."""
        try:
            result = self._request("DELETE", f"/api/cards/{urllib.parse.quote(card_id, safe='')}")
            self._invalidate_listing()
            if result is None:
                return False
            return bool(result.get("ok", False))
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository.delete failed for %s: %s", card_id, exc)
            return False

    def search_cards(
        self,
        query: str,
        *,
        limit: int = 10,
        scope: Optional[Any] = None,
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Native text search as ``{card_id: card_dict}``, or ``None`` to fall back to in-app IDF.

        Signature and return type both follow the optional-capability contract in
        ``card_repository.py``, which ``FileContextStore`` calls as
        ``search(task_text, limit=...)`` and then type-checks with
        ``isinstance(result, dict)``. This previously took ``max_results`` positionally
        and returned a list, so the call raised TypeError, was swallowed, and the
        native arm silently never ran. The query parameter is ``max_results``, which is
        what the endpoint reads; the old ``max`` was ignored.
        """
        try:
            result = self._request(
                "GET",
                "/api/cards/search",
                params={"q": query, "max_results": limit, **_INCLUDE_MANAGED},
            )
            if isinstance(result, dict):
                return _cards_by_id(result.get("cards"))
            return None
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository.search_cards failed: %s", exc)
            return None

    # -- CardRepository protocol stubs (FileContextStore also calls these) --

    def exists(self, card_id: str) -> bool:
        """True if the card exists (read-then-check; best-effort)."""
        try:
            return self.read(card_id) is not None
        except Exception:  # noqa: BLE001
            return False

    def revision(self) -> Any:
        """A cheap STORE-WIDE change-stamp: ``(local_write_counter, card_count, max_updated_at)``.

        ``FileContextStore._load_all`` calls this with NO arguments on every read to
        decide whether its in-memory cache is stale. This used to be declared as
        ``revision(card_id)``, a per-CARD stamp, so that call raised TypeError and the
        store could never load a single card through this repository: the Quest-API
        card backend was unusable end to end, which is why cards written by a runner
        stayed in its local ``.quest-context`` instead of becoming visible in Quest.

        Bumps on any write by this process, and reflects another process's writes via
        the card count and the newest ``updated_at`` the API reports. The listing it
        needs is cached briefly and reused by the ``load_all`` that follows, so a read
        costs ONE request rather than two. Returns a stable sentinel on any error, so
        a temporary outage does not thrash the store's cache.
        """
        try:
            cards = self._fetch_all(max_age_s=_LISTING_TTL_S)
            newest = ""
            for card in cards.values():
                stamp = str(card.get("updated_at") or card.get("modified_at") or "")
                if stamp > newest:
                    newest = stamp
            return (self._local_rev, len(cards), newest)
        except Exception:  # noqa: BLE001
            return (self._local_rev, -1, "")
