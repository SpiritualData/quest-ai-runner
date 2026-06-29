"""QuestApiCardRepository -- CardRepository backed by quest-backend's card API.

Use this when QAR is connected to Quest AI (QAR_QUEST_API_URL + QAR_QUEST_API_KEY
are set) so that durable context cards are persisted centrally in quest-backend's
Qdrant store rather than locally. Cards include an inline preview so Quest AI can
use them immediately without a round-trip to the local machine.

Env vars consumed by the factory (read in config.py, passed in at construction):
  QAR_QUEST_API_URL   -- Quest backend base URL (e.g. https://api.prod.spiritualdata.org)
  QAR_QUEST_API_KEY   -- Bearer token for the quest-backend card API
  QAR_USER_ID         -- The Quest user ID (MongoDB ObjectId string) whose cards to manage
  QAR_RUNNER_ENV_ID   -- Identifies this local runner environment (default: hostname)

All methods are best-effort and NEVER raise: a reader returns None / [] / False on
any failure so the FileContextStore degrades gracefully.
"""
from __future__ import annotations

import json
import logging
import os
import socket
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


def _enrich_card(card: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    """Return a copy of card enriched with source metadata and a local_preview content item."""
    enriched = dict(card)
    env_id = _runner_env_id()
    source_type = _detect_source_type(card)
    preview = _extract_preview(card)

    # Attach runner-environment provenance.
    enriched["source_environment"] = "local"
    enriched["runner_env_id"] = env_id

    # Build or extend content_items with a local_preview item so Quest AI can use
    # this card immediately from the inline preview without fetching the local machine.
    preview_item: Dict[str, Any] = {
        "type": "local_preview",
        "preview": preview,
        "source_type": source_type,
        "runner_env_id": env_id,
        # source_locator carries enough for a future local_fetch escalation.
        "source_locator": {
            "card_id": card.get("id") or card.get("card_id") or "",
            "user_id": user_id,
        },
    }
    existing = list(card.get("content_items") or [])
    # Replace any existing local_preview item (idempotent on re-write).
    existing = [i for i in existing if not (isinstance(i, dict) and i.get("type") == "local_preview")]
    existing.append(preview_item)
    enriched["content_items"] = existing
    return enriched


class QuestApiCardRepository:
    """CardRepository backed by quest-backend's /api/cards HTTP API.

    Implements the same protocol as FilesystemCardRepository and QdrantCardRepository:
      write(card_id, card) -> bool
      read(card_id) -> Optional[dict]
      load_all() -> List[dict]
      delete(card_id) -> bool
      search_cards(query, scope=None, max_results=10) -> Optional[List[dict]]

    All methods catch every exception and return a safe default so the FileContextStore
    degrades gracefully on network failures.
    """

    def __init__(self, base_url: str, api_key: str, user_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id

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

    def load_all(self) -> List[Dict[str, Any]]:
        """Load all cards for the user. Returns a list (possibly empty)."""
        try:
            result = self._request("GET", "/api/cards")
            if isinstance(result, dict):
                return list(result.get("cards") or [])
            return []
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository.load_all failed: %s", exc)
            return []

    def delete(self, card_id: str) -> bool:
        """Delete a card by id. Returns True on success."""
        try:
            result = self._request("DELETE", f"/api/cards/{urllib.parse.quote(card_id, safe='')}")
            if result is None:
                return False
            return bool(result.get("ok", False))
        except Exception as exc:  # noqa: BLE001
            _log.debug("QuestApiCardRepository.delete failed for %s: %s", card_id, exc)
            return False

    def search_cards(
        self,
        query: str,
        scope: Optional[Any] = None,
        max_results: int = 10,
    ) -> Optional[List[Dict[str, Any]]]:
        """Keyword search. Returns a list of matching cards, or None on failure (triggers fallback)."""
        try:
            result = self._request(
                "GET",
                "/api/cards/search",
                params={"q": query, "max": max_results},
            )
            if isinstance(result, dict):
                return list(result.get("cards") or [])
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

    def revision(self, card_id: str) -> Optional[str]:
        """Return an opaque change-stamp for staleness detection, or None."""
        try:
            card = self.read(card_id)
            if card is None:
                return None
            # Use updated_at if the backend echoes it; fall back to a content hash.
            ts = card.get("updated_at") or card.get("modified_at")
            if ts:
                return str(ts)
            return None
        except Exception:  # noqa: BLE001
            return None
