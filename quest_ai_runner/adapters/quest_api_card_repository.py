"""QuestApiCardRepository -- CardRepository backed by the Quest backend's card API.

The Quest backend is the DEFAULT source of truth for context cards and their vectors whenever a lane
has Quest credentials (see ``docs/context-card-stores.md``). ``QAR_CARDS_BACKEND=local`` is the opt-out.
Cards are embedded ONCE, server-side, and searched server-side, so a lane under this backend never
embeds a card and never builds a local vector index of cards.

Environment (read by ``quest_card_settings_from_env`` / the config factory, passed in at construction):
  QAR_QUEST_API_URL  or QUEST_BASE_URL  -- Quest backend base URL
  QAR_QUEST_API_KEY  or QUEST_API_KEY   -- Bearer key (the key's own user is the acting user)
  QAR_TEAM_ID        or QUEST_TEAM_ID   -- team this lane writes cards to (omit for private cards)
  QAR_CARDS_SCOPE                       -- card scope of the lane: ``org:<id>`` | ``team:<id>`` |
                                           ``private`` | ``org`` (org from QAR_ORG_ID / QUEST_ORG_ID,
                                           else the lane's team's org); unset = the lane's team
  QAR_ORG_ID         or QUEST_ORG_ID    -- the org for ``QAR_CARDS_SCOPE=org``
  QAR_RUNNER_ENV_ID  or QAR_ENV_ID      -- this runner environment's id (default: hostname)
  QAR_USER_ID                           -- optional hint; the key's identity (whoami) always wins

ACCESS MODEL (enforced server-side; this class only chooses the scope of each call)
  * A card has an owner user and exactly one scope: private, a team, or an org.
  * Readable by the owner and by members of its team or org; writable by the owner, or by a member
    writing the team's or org's card. Listing and search return everything the key's user can read.
  * Card identity is (scope, card_id): the same id may exist privately, in a team and in an org. This class keeps
    a ``{card_id: card}`` mapping (the CardRepository protocol); on an id collision the card in this
    repository's WRITE scope wins, then the private card.

LOUD FAILURES. A backend or transport error is logged at ERROR with the method, path, status and the
backend's own reason, counted (``error_count``), kept (``last_error``), reported through the optional
``on_error`` callback, and exposed by ``health()``. ``FileContextStore.assemble`` adds a
"CARD BACKEND ERROR" source to the run's context sources while ``health()['degraded']`` is true, which
the orchestrator emits as a status event. A failed read serves the last good listing (or nothing if
there never was one) but is never silent: the lane keeps running and every failure is visible.

OFFLINE CACHE SEAM. ``CardCache`` is the interface; ``NullCardCache`` stores nothing and ``FileCardCache``
(``card_cache_file.py``) is the durable one. On a cold start with the backend down the repository serves
the cache's stored listing. A cache is passed as ``cache=``. See ``CardCache``.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple
from esoi import implementation
from hubofhubs.recall import card_origin

_log = logging.getLogger("quest-ai-runner.quest_api_cards")

# Card content-type detection source-types.
_SOURCE_TURN = "turn_card"
_SOURCE_CONVO = "conversation_ref"
_SOURCE_FILE = "file_context"
_SOURCE_GENERIC = "context_card"

# Fields the backend owns. Never sent on write (a read-modify-write must not echo them back).
SERVER_OWNED_FIELDS = ("updated_at", "revision", "owner_user_id", "user_id", "card_id")

# Server-side limits / client paging.
BULK_MAX_CARDS = 100
LIST_PAGE_SIZE = 500
# A listing/stamp may be reused this long. revision() and load_all() run back to back on every
# context-store read, so this makes that ONE round trip; short because another process may write.
_LISTING_TTL_S = 5.0
# The card API hides auto-maintained cards (``managed_by``) unless asked. A runner is a GROUNDING
# consumer: it needs them, so every read opts in.
_INCLUDE_MANAGED = {"include_managed": "true"}


# ---------------------------------------------------------------------------
# Environment helpers (shared by config.py and the ``context upload`` command)
# ---------------------------------------------------------------------------

def first_env(*names: str, env: Optional[Dict[str, str]] = None) -> str:
    """The first non-empty value among ``names`` (the earlier name wins), stripped; ``""`` if none."""
    source = os.environ if env is None else env
    for name in names:
        value = (source.get(name) or "").strip()
        if value:
            return value
    return ""


def runner_env_id() -> str:
    """This runner environment's id: QAR_RUNNER_ENV_ID, else QAR_ENV_ID, else the hostname."""
    return first_env("QAR_RUNNER_ENV_ID", "QAR_ENV_ID") or socket.gethostname()


def quest_card_settings_from_env(env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """``{base_url, api_key, team_id, user_hint}`` from the environment; the ``QAR_*`` names win over
    the lanes' ``QUEST_*`` names. Missing values are ``""``. Never raises."""
    return {
        "base_url": first_env("QAR_QUEST_API_URL", "QUEST_BASE_URL", env=env),
        "api_key": first_env("QAR_QUEST_API_KEY", "QUEST_API_KEY", env=env),
        "team_id": first_env("QAR_TEAM_ID", "QUEST_TEAM_ID", env=env),
        "user_hint": first_env("QAR_USER_ID", env=env),
        "org_id": first_env("QAR_ORG_ID", "QUEST_ORG_ID", env=env),
        "cards_scope": first_env("QAR_CARDS_SCOPE", env=env),
    }


class CardScopeError(ValueError):
    """The lane's card scope setting cannot be turned into a scope (fail at startup, never guess)."""


def select_card_scope(
    settings: Dict[str, str],
    org_of_team: Optional[Callable[[str], str]] = None,
) -> Dict[str, str]:
    """``{team_id, org_id}`` for a lane from ``quest_card_settings_from_env`` output.

    ``QAR_CARDS_SCOPE`` is ``org:<id>``, ``team:<id>``, ``private``, or a bare ``org`` (the org is
    ``QAR_ORG_ID``/``QUEST_ORG_ID``, else ``org_of_team(lane team)``). Unset keeps the default: the
    lane's team (private when it has none). An id inside the scope value wins over ``QAR_ORG_ID`` and
    the team variables. For an org scope ``team_id`` is kept only as provenance (the lane's team);
    writes go to the org. Raises ``CardScopeError`` for an unknown form or an unresolvable org.
    """
    raw = (settings.get("cards_scope") or "").strip()
    team = (settings.get("team_id") or "").strip()
    org = (settings.get("org_id") or "").strip()
    if not raw:
        return {"team_id": team, "org_id": ""}
    kind, _, ident_part = raw.partition(":")
    kind = kind.strip().lower()
    ident_part = ident_part.strip()
    if kind == "private" and not ident_part:
        return {"team_id": "", "org_id": ""}
    if kind == "team":
        chosen = ident_part or team
        if not chosen:
            raise CardScopeError("QAR_CARDS_SCOPE=team needs a team id (team:<id> or QAR_TEAM_ID)")
        return {"team_id": chosen, "org_id": ""}
    if kind == "org":
        chosen = ident_part or org
        if not chosen and team and org_of_team is not None:
            try:
                chosen = str(org_of_team(team) or "").strip()
            except Exception as exc:  # noqa: BLE001
                raise CardScopeError(
                    f"QAR_CARDS_SCOPE=org: could not resolve the org of the lane's team: {exc}") from exc
        if not chosen:
            raise CardScopeError(
                "QAR_CARDS_SCOPE=org needs an org: use org:<id>, QAR_ORG_ID/QUEST_ORG_ID, or a lane team "
                "that belongs to an org")
        return {"team_id": team, "org_id": chosen}
    raise CardScopeError(
        f"QAR_CARDS_SCOPE={raw!r} is not one of org:<id>, team:<id>, private, org")


def _detect_source_type(card: Dict[str, Any]) -> str:
    """Detect the source type of a card for the local_fetch content item."""
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
    user = str(card.get("user") or "")
    ai = str(card.get("assistant_summary") or "")
    if user or ai:
        parts = []
        if user:
            parts.append(f"User: {user}")
        if ai:
            parts.append(f"AI: {ai}")
        return "\n".join(parts)[:max_chars]
    text = card.get("description") or card.get("summary") or card.get("name") or ""
    return str(text)[:max_chars]


def strip_server_owned(card: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of ``card`` without the fields only the backend may set."""
    return {k: v for k, v in card.items() if k not in SERVER_OWNED_FIELDS}


def _enrich_card(card: Dict[str, Any], user_id: str, team_id: str = "") -> Dict[str, Any]:
    """A copy of ``card`` with source metadata and a ``local_fetch`` content item.

    ``team_id`` is the lane's team (provenance for the quest-context hub fan-out). Whether the card is
    WRITTEN to that team is decided by the caller through the request's ``team_id`` query parameter.
    """
    enriched = strip_server_owned(card)
    env_id = runner_env_id()
    source_type = _detect_source_type(card)
    preview = _extract_preview(card)
    query = (
        str(card.get("user") or "")
        or str(card.get("name") or "")
        or str(card.get("description") or "")
    )[:500]

    enriched["source_environment"] = "local"
    enriched["runner_env_id"] = env_id
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
    existing = [
        i for i in existing
        if isinstance(i, dict) and i.get("type") not in ("local_fetch", "local_preview")
    ]
    existing.append(fetch_item)
    enriched["content_items"] = existing
    return enriched


def card_pairs(payload: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """Normalize a ``cards`` reply (a list, or the older ``{id: card}`` mapping) into ordered
    ``(id, card)`` pairs, keeping every card: two scopes may share an id. Anything else reads as none."""
    out: List[Tuple[str, Dict[str, Any]]] = []
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
        out.append((cid, card))
    return out


def _cards_by_id(payload: Any) -> Dict[str, Dict[str, Any]]:
    """``card_pairs`` as ``{id: card}``; on an id shared by several scopes the last one wins."""
    return dict(card_pairs(payload))


# ---------------------------------------------------------------------------
# Errors and the offline-cache seam
# ---------------------------------------------------------------------------

class QuestCardApiError(Exception):
    """A card API call failed. ``status`` is the HTTP status, 0 for a transport failure."""

    def __init__(self, message: str, *, status: int = 0, method: str = "", path: str = "",
                 detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        self.detail = detail


class CardCache(Protocol):
    """INTERFACE SEAM for an offline card cache and write outbox (``FileCardCache`` is the durable one).

    An implementation lets a lane keep working when the backend is unreachable: ``load_listing``
    seeds a cold start, ``store_listing`` is called after every successful full or delta refresh,
    and ``enqueue_write`` is offered every write that FAILED so it can be replayed later
    (``pending_writes``). The repository never depends on a cache for correctness: with the default
    ``NullCardCache`` every method is a no-op and failures stay loud.
    """

    def load_listing(self) -> Optional[Tuple[Dict[str, Dict[str, Any]], float]]:
        """``(cards_by_id, max_updated_at)`` from the last stored listing, or None."""

    def store_listing(self, cards: Dict[str, Dict[str, Any]], max_updated_at: float) -> None:
        """Persist the latest known listing."""

    def enqueue_write(self, card_id: str, card: Dict[str, Any], team_id: str) -> bool:
        """Offer a write that could not reach the backend. Return True if it was queued."""

    def pending_writes(self) -> List[Tuple[str, Dict[str, Any], str]]:
        """Queued ``(card_id, card, team_id)`` writes, oldest first."""


@implementation("cache-cards", "null")
class NullCardCache:
    """Stores nothing, queues nothing. ``directory`` is accepted (and ignored) so it is built the same way
    as ``FileCardCache``."""

    def __init__(self, directory: str = "") -> None:
        self.directory = directory

    def load_listing(self) -> Optional[Tuple[Dict[str, Dict[str, Any]], float]]:
        return None

    def store_listing(self, cards: Dict[str, Dict[str, Any]], max_updated_at: float) -> None:
        return None

    def enqueue_write(self, card_id: str, card: Dict[str, Any], team_id: str) -> bool:
        return False

    def pending_writes(self) -> List[Tuple[str, Dict[str, Any], str]]:
        return []


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------

@implementation("persist-cards", "quest_api")
class QuestApiCardRepository:
    """CardRepository backed by quest-backend's ``/api/cards`` HTTP API (see the module docstring).

    Parameters
    ----------
    base_url, api_key:
        The Quest backend and the Bearer key. The key's user is the acting user.
    user_id:
        Optional acting-user id. Empty (the default) resolves it from the key via
        ``GET /api/teams/whoami`` on first use. Whatever whoami returns wins over a supplied value
        (a mismatch is logged as a warning).
    team_id:
        The team this lane writes cards to. Empty means a lane writes private cards.
    store_id:
        Opaque store identity (card identity is ``(store_id, card_id)``); a remote store is never
        mounted.
    org_id:
        The org this lane writes cards to. When set, writes go to the org (``team_id`` then stays
        only as the lane's provenance for the ``local_fetch`` item).
    read_team_ids, read_org_ids:
        Optional restriction of the team / org cards listed/searched (default: everything the user
        can read: private, direct teams and the orgs they belong to).
    on_error:
        Optional ``callable(message)`` invoked (de-duplicated) when a call fails, so the host can
        surface it in its own status channel.
    cache:
        A ``CardCache`` (default ``NullCardCache``; ``FileCardCache`` is durable).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        user_id: str = "",
        store_id: str = "",
        *,
        team_id: str = "",
        read_team_ids: Optional[Iterable[str]] = None,
        org_id: str = "",
        read_org_ids: Optional[Iterable[str]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        cache: Optional[CardCache] = None,
        timeout: float = 15.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.user_id = user_id
        self.store_id = store_id
        self.team_id = team_id
        self.read_team_ids: List[str] = [t for t in (read_team_ids or []) if t]
        self.org_id = org_id
        self.read_org_ids: List[str] = [o for o in (read_org_ids or []) if o]
        self._on_error = on_error
        self._cache: CardCache = cache or NullCardCache()
        self._timeout = timeout
        self._lock = threading.RLock()
        self._local_rev = 0
        self._resolved_user = False
        # Listing state.
        self._cards: Optional[Dict[str, Dict[str, Any]]] = None
        self._max_updated = 0.0
        self._count_offset = 0
        self._warm_only = False
        self._listing_at = 0.0
        self._stamp: Optional[Dict[str, Any]] = None
        self._stamp_at = 0.0
        self._scope_of: Dict[str, Tuple[str, str]] = {}
        # Health.
        self.last_error: Optional[Dict[str, Any]] = None
        self.error_count = 0
        self._degraded = False
        self._last_notified = ""

    # -- health ------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """``{ok, degraded, error_count, last_error}``. ``degraded`` clears on the next success."""
        return {
            "ok": not self._degraded,
            "degraded": self._degraded,
            "error_count": self.error_count,
            "last_error": dict(self.last_error) if self.last_error else None,
        }

    def _fail(self, method: str, path: str, status: int, detail: str) -> QuestCardApiError:
        message = f"Quest card backend {method} {path} failed (status {status or 'transport'}): {detail}"
        self.error_count += 1
        self._degraded = True
        self.last_error = {"method": method, "path": path, "status": status, "detail": detail,
                           "at": time.time()}
        _log.error(message)
        if self._on_error is not None and message != self._last_notified:
            self._last_notified = message
            try:
                self._on_error(message)
            except Exception:  # noqa: BLE001
                _log.debug("on_error callback raised", exc_info=True)
        return QuestCardApiError(message, status=status, method=method, path=path, detail=detail)

    # -- transport ---------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.user_id:
            headers["X-User-Id"] = self.user_id
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Optional[Any]:
        """One HTTP call; the parsed JSON body. Raises ``QuestCardApiError`` (already logged at ERROR
        and counted) on any non-2xx or transport failure. A 404 raises with ``status == 404``."""
        url = self._url(path)
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body, default=str).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                reply = json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                text = exc.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(text)
                    detail = str(parsed.get("detail", text)) if isinstance(parsed, dict) else text
                except ValueError:
                    detail = text
            except Exception:  # noqa: BLE001
                pass
            if exc.code == 404:
                # Not an outage: the card is simply absent. Not counted as a failure.
                raise QuestCardApiError(f"{method} {path}: not found", status=404, method=method,
                                        path=path, detail=detail[:300]) from exc
            raise self._fail(method, path, exc.code, detail[:300]) from exc
        except Exception as exc:  # noqa: BLE001
            raise self._fail(method, path, 0, f"{exc.__class__.__name__}: {exc}") from exc
        self._degraded = False
        return reply

    def resolve_user_id(self) -> str:
        """The acting user's id, resolved ONCE from the key via ``GET /api/teams/whoami``.

        The key's identity wins over any id supplied at construction (a difference is logged as a
        warning). A failure is loud (ERROR, counted) and returns whatever id is known, possibly ``""``;
        it is retried on the next call.
        """
        with self._lock:
            if self._resolved_user:
                return self.user_id
            supplied = self.user_id
            self.user_id = ""  # do not send a stale hint while asking who we are
            try:
                reply = self._request("GET", "/api/teams/whoami")
            except QuestCardApiError:
                self.user_id = supplied
                return supplied
            resolved = str((reply or {}).get("user_id") or "").strip() if isinstance(reply, dict) else ""
            if not resolved:
                self.user_id = supplied
                self._fail("GET", "/api/teams/whoami", 200, "reply carried no user_id")
                return supplied
            if supplied and supplied != resolved:
                _log.warning("QAR_USER_ID names a different user than the Quest key authenticates; "
                             "using the key's user (cards are read and written as that user)")
            self.user_id = resolved
            self._resolved_user = True
            return resolved

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
              params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        if not self._resolved_user:
            self.resolve_user_id()
        return self._request(method, path, body=body, params=params)

    # -- scope -------------------------------------------------------------

    def default_scope(self) -> Tuple[str, str]:
        """``(team_id, org_id)`` this lane writes to: its org when it has one, else its team."""
        return ("", self.org_id) if self.org_id else (self.team_id, "")

    def _write_scope(self, card_id: str, card: Dict[str, Any]) -> Tuple[str, str]:
        """``(team_id, org_id)`` a write goes to (both empty = private): the card's own team or org if
        it carries one, else where the last listing saw this id, else this lane's scope."""
        own = str(card.get("team_id") or "").strip()
        if own:
            return (own, "")
        own_org = str(card.get("org_id") or "").strip()
        if own_org:
            return ("", own_org)
        if card_id in self._scope_of:
            return self._scope_of[card_id]
        return self.default_scope()

    @staticmethod
    def scope_params(scope: Tuple[str, str]) -> Optional[Dict[str, str]]:
        team, org = scope
        if org:
            return {"org_id": org}
        return {"team_id": team} if team else None

    def _list_params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = dict(_INCLUDE_MANAGED)
        params.update(self.read_filter_params())
        if extra:
            params.update(extra)
        return params

    def read_filter_params(self) -> Dict[str, str]:
        params: Dict[str, str] = {}
        if self.read_team_ids:
            params["team_ids"] = ",".join(self.read_team_ids)
        if self.read_org_ids:
            params["org_ids"] = ",".join(self.read_org_ids)
        if params:
            params["include_private"] = "true"
        return params

    # -- CardRepository protocol -------------------------------------------

    def write(self, card_id: str, card: Dict[str, Any]) -> bool:
        """Upsert a card (to its team scope, or privately). True on success; False, loudly, on failure."""
        if card_origin.refuse(card_id):
            return False
        scope = self._write_scope(card_id, card)
        try:
            enriched = _enrich_card(card, self.user_id, self.team_id or scope[0])
            enriched.pop("org_id", None)
            if scope[0]:
                enriched["team_id"] = scope[0]
            else:
                enriched.pop("team_id", None)
            params = self.scope_params(scope)
            result = self._call("PUT", f"/api/cards/{urllib.parse.quote(card_id, safe='')}",
                                body=enriched, params=params)
        except QuestCardApiError:
            self._cache.enqueue_write(card_id, card, scope[0])
            return False
        finally:
            self._invalidate_listing()
        self._scope_of[card_id] = scope
        return bool(isinstance(result, dict) and result.get("ok", False))

    def read(self, card_id: str) -> Optional[Dict[str, Any]]:
        """A card by id, or None when it does not exist. A backend failure is loud and returns None."""
        params = self.scope_params(self._scope_of.get(card_id, ("", "")))
        try:
            result = self._call("GET", f"/api/cards/{urllib.parse.quote(card_id, safe='')}",
                                params=params)
        except QuestCardApiError as exc:
            if exc.status != 404:
                _log.error("Quest card backend: read of %r failed; returning no card", card_id)
            return None
        return result if isinstance(result, dict) else None

    def delete(self, card_id: str) -> bool:
        params = self.scope_params(self._scope_of.get(card_id, ("", "")))
        try:
            result = self._call("DELETE", f"/api/cards/{urllib.parse.quote(card_id, safe='')}",
                                params=params)
        except QuestCardApiError:
            return False
        finally:
            self._invalidate_listing()
        self._scope_of.pop(card_id, None)
        return bool(isinstance(result, dict) and result.get("ok", False))

    def exists(self, card_id: str) -> bool:
        return self.read(card_id) is not None

    # -- listing: stamp, paging, delta ---------------------------------------

    def _invalidate_listing(self) -> None:
        """Our own write/delete: force the next read to re-check the stamp, and bump the local stamp."""
        self._listing_at = 0.0
        self._stamp_at = 0.0
        self._local_rev += 1

    def _fetch_stamp(self, max_age_s: float = 0.0) -> Dict[str, Any]:
        if self._stamp is not None and max_age_s > 0 and (time.monotonic() - self._stamp_at) < max_age_s:
            return self._stamp
        params = self.read_filter_params()
        reply = self._call("GET", "/api/cards/stamp", params=params or None)
        stamp = reply if isinstance(reply, dict) else {}
        self._stamp = stamp
        self._stamp_at = time.monotonic()
        return stamp

    def _fetch_pages(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
        """Every page of ``GET /api/cards`` as ``{card_id: card}``; raises on any failed page."""
        out: Dict[str, Dict[str, Any]] = {}
        cursor = ""
        for _ in range(10_000):
            params = self._list_params({"limit": LIST_PAGE_SIZE, **(extra or {})})
            if cursor:
                params["cursor"] = cursor
            reply = self._call("GET", "/api/cards", params=params)
            if not isinstance(reply, dict):
                raise self._fail("GET", "/api/cards", 200, "reply was not a JSON object")
            for cid, card in card_pairs(reply.get("cards")):
                self._merge_one(out, cid, card)
            cursor = str(reply.get("next_cursor") or "")
            if not cursor:
                return out
        raise self._fail("GET", "/api/cards", 200, "paging did not terminate")

    def _merge_one(self, into: Dict[str, Dict[str, Any]], cid: str, card: Dict[str, Any]) -> None:
        """Insert ``card``; on an id collision the card in this lane's write scope wins, then private.
        The scope of a listed card is ``(team_id, org_id)``; both empty is private."""
        scope = (str(card.get("team_id") or ""), str(card.get("org_id") or ""))
        mine = self.default_scope()
        if cid in into:
            held = (str(into[cid].get("team_id") or ""), str(into[cid].get("org_id") or ""))
            if held == mine and scope != mine:
                return
            if held == ("", "") and scope != mine:
                return
        into[cid] = card
        self._scope_of[cid] = scope

    def _full_refresh(self, stamp: Dict[str, Any]) -> None:
        self._scope_of = {}
        cards = self._fetch_pages()
        self._cards = cards
        self._max_updated = float(stamp.get("max_updated_at") or 0.0)
        count = stamp.get("count")
        self._count_offset = (len(cards) - int(count)) if isinstance(count, int) else 0
        self._cache.store_listing(cards, self._max_updated)

    def _refresh(self) -> Dict[str, Dict[str, Any]]:
        """Bring the in-memory listing up to date: first load pages everything; later refreshes fetch
        only cards written since the last ``max_updated_at``, then fall back to ONE full refresh if
        the count says something was deleted. Raises ``QuestCardApiError`` (already logged)."""
        if (self._cards is not None and self._listing_at
                and (time.monotonic() - self._listing_at) < _LISTING_TTL_S):
            return self._cards
        if self._cards is None:
            # Cold start. If the backend cannot be reached, serve the stored listing (still loud: the
            # error was logged and counted) and force a full refresh on the next successful call.
            try:
                self._full_refresh(self._fetch_stamp(max_age_s=_LISTING_TTL_S))
            except QuestCardApiError:
                warm = self._cache.load_listing()
                if warm:
                    self._cards, self._max_updated = dict(warm[0]), warm[1]
                    self._scope_of = {cid: (str(c.get("team_id") or ""), str(c.get("org_id") or ""))
                                      for cid, c in self._cards.items()}
                    self._warm_only = True
                raise
        else:
            stamp = self._fetch_stamp(max_age_s=_LISTING_TTL_S)
            if self._warm_only:
                self._full_refresh(stamp)
                self._warm_only = False
            elif float(stamp.get("max_updated_at") or 0.0) != self._max_updated or (
                    isinstance(stamp.get("count"), int)
                    and len(self._cards) - self._count_offset != stamp["count"]):
                self._delta_refresh(stamp)
        self._listing_at = time.monotonic()
        return self._cards

    def _delta_refresh(self, stamp: Dict[str, Any]) -> None:
        """Merge the cards written since the last ``max_updated_at``; one full refresh if the count
        still disagrees (something was deleted)."""
        delta = self._fetch_pages({"updated_after": repr(self._max_updated)})
        for cid, card in delta.items():
            self._cards[cid] = card
        self._max_updated = float(stamp.get("max_updated_at") or self._max_updated)
        if isinstance(stamp.get("count"), int) and (
                len(self._cards) - self._count_offset != stamp["count"]):
            self._full_refresh(stamp)
        else:
            self._cache.store_listing(self._cards, self._max_updated)

    def load_all(self) -> Dict[str, Dict[str, Any]]:
        """Every readable card as ``{card_id: card}``. On a backend failure the error is logged and
        counted, and the last good listing (or ``{}`` if there never was one) is returned."""
        with self._lock:
            try:
                return dict(self._refresh())
            except QuestCardApiError:
                return dict(self._cards or {})

    def revision(self) -> Any:
        """A cheap change-stamp ``(local_write_counter, count, max_updated_at)`` from ``/api/cards/stamp``.
        On failure the error is loud and the last known stamp is returned so the store's cache holds."""
        with self._lock:
            try:
                stamp = self._fetch_stamp(max_age_s=_LISTING_TTL_S)
            except QuestCardApiError:
                stamp = self._stamp or {}
            return (self._local_rev, stamp.get("count", -1), stamp.get("max_updated_at", 0.0))

    # -- search -------------------------------------------------------------

    def search_cards(
        self,
        query: str,
        *,
        limit: int = 10,
        scope: Optional[Any] = None,
    ) -> Optional[Dict[str, Dict[str, Any]]]:
        """Server-side semantic + keyword search as an ORDERED ``{card_id: card}`` (best first), or
        ``None`` (loudly) when the backend failed so the store falls back to in-app scoring."""
        try:
            params = {"q": query, "max_results": limit, **_INCLUDE_MANAGED}
            params.update(self.read_filter_params())
            result = self._call("GET", "/api/cards/search", params=params)
        except QuestCardApiError:
            return None
        if isinstance(result, dict):
            return _cards_by_id(result.get("cards"))
        return None

    # -- embedder + bulk ------------------------------------------------------

    def org_of_team(self, team_id: str) -> str:
        """The ``org_id`` of a team (``GET /api/teams/{team_id}``), ``""`` for a standalone team."""
        reply = self._call("GET", f"/api/teams/{urllib.parse.quote(team_id, safe='')}")
        return str((reply or {}).get("org_id") or "") if isinstance(reply, dict) else ""

    def embedder_info(self) -> Dict[str, Any]:
        """``{embedder_id, dimension}`` of the backend's embedder (raises ``QuestCardApiError``)."""
        reply = self._call("GET", "/api/cards/embedder")
        return reply if isinstance(reply, dict) else {}

    def bulk_write(
        self,
        cards: Iterable[Tuple[str, Dict[str, Any]]],
        vectors: Optional[Dict[str, List[float]]] = None,
        *,
        team_id: Optional[str] = None,
        org_id: Optional[str] = None,
        embedder_id: str = "",
        dimension: int = 0,
        on_batch: Optional[Callable[[int, Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Upsert many ``(card_id, card)`` pairs through ``POST /api/cards/bulk`` in batches of at most
        100. ``vectors`` ({card_id: vector}) are offered with ``embedder_id`` and ``dimension``; the
        backend accepts them only when both match its own embedder, otherwise it embeds server-side.
        Cards are sent as given (minus server-owned fields), without the local_fetch preview. The
        scope is ``org_id`` if given, else ``team_id`` (empty = private); with neither, the lane's own.

        Returns ``{"batches": n, "counts": {...}, "failed": [ids], "errors": [...]}``. A failed batch
        is logged at ERROR and its ids are listed in ``failed``; the remaining batches still run.
        """
        if org_id is None and team_id is None:
            team_scope, org_scope = self.default_scope()
        else:
            org_scope = org_id or ""
            team_scope = "" if org_scope else (self.team_id if team_id is None else team_id)
        cards = list(cards)
        refused = [str(cid) for cid, _ in cards if card_origin.refuse(str(cid))]
        pairs = [(str(cid), strip_server_owned(c)) for cid, c in cards if str(cid) not in refused]
        totals = {"written": 0, "embedded": 0, "vectors_accepted": 0, "vectors_rejected": 0,
                  "failed": len(refused)}
        failed: List[str] = list(refused)
        errors: List[str] = []
        batches = 0
        for start in range(0, len(pairs), BULK_MAX_CARDS):
            chunk = pairs[start:start + BULK_MAX_CARDS]
            items = []
            for cid, card in chunk:
                item: Dict[str, Any] = {"id": cid, "card": card}
                if vectors and cid in vectors:
                    item["vector"] = list(vectors[cid])
                items.append(item)
            body: Dict[str, Any] = {"cards": items}
            if org_scope:
                body["org_id"] = org_scope
            elif team_scope:
                body["team_id"] = team_scope
            if vectors:
                body["embedder_id"] = embedder_id
                body["dimension"] = int(dimension)
            batches += 1
            try:
                reply = self._call("POST", "/api/cards/bulk", body=body)
            except QuestCardApiError as exc:
                failed.extend(cid for cid, _ in chunk)
                errors.append(str(exc))
                totals["failed"] += len(chunk)
                if on_batch:
                    on_batch(batches, {"error": str(exc), "ids": [cid for cid, _ in chunk]})
                continue
            reply = reply if isinstance(reply, dict) else {}
            for key, value in (reply.get("counts") or {}).items():
                if key in totals and isinstance(value, int):
                    totals[key] += value
            for result in reply.get("results") or []:
                if isinstance(result, dict) and not result.get("ok", True):
                    failed.append(str(result.get("id")))
                    errors.append(str(result.get("error") or "rejected"))
            if on_batch:
                on_batch(batches, reply)
        self._invalidate_listing()
        return {"batches": batches, "counts": totals, "failed": failed, "errors": errors}


# ---------------------------------------------------------------------------
# Server-backed vector arm
# ---------------------------------------------------------------------------

from hubofhubs.ports.context import VectorHit  # noqa: E402
from ..core.adapters_bases import VectorStoreBase  # noqa: E402


class QuestApiCardVectorStore(VectorStoreBase):
    """Query-only ``VectorStore`` whose search runs ENTIRELY on the Quest backend.

    Under the Quest card backend a lane embeds nothing and keeps no local card index: the backend
    embedded each card once at write time and answers ``search`` with its own semantic + keyword
    ranking. This adapter hands that ranking to ``VectorContextAssembler`` as ``VectorHit`` objects
    (best first, with a descending rank score, since the API returns order rather than a similarity),
    so the server's ranking is not lost behind the keyword arm's own IDF gate. ``upsert``/``sync`` are
    deliberate no-ops (cards are never seeded from here); a backend failure is loud in the repository
    and yields no hits.
    """

    def __init__(self, repository: QuestApiCardRepository) -> None:
        self._repo = repository

    def search(self, query: str, *, scope: Optional[Dict[str, Any]] = None, top_k: int = 8) -> List[VectorHit]:
        found = self._repo.search_cards(query, limit=max(1, int(top_k or 8)))
        hits: List[VectorHit] = []
        for rank, (card_id, card) in enumerate((found or {}).items()):
            paths = [fe.get("path", "") for fe in (card.get("files") or [])
                     if isinstance(fe, dict) and fe.get("path")]
            hits.append(VectorHit(
                id=f"card:{card_id}",
                score=max(0.0, 1.0 - 0.01 * rank),
                text=str(card.get("description") or card.get("summary") or card.get("name") or ""),
                payload={
                    "paths": paths,
                    "summary": card.get("summary", "") or card.get("name", ""),
                    "kind": "bootstrap",
                    "content": card.get("content") or [],
                    "card_id": card_id,
                    "scope_tags": card.get("scope_tags") or [],
                    "vector_min_score": card.get("vector_min_score"),
                },
            ))
        return hits

    def upsert(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> None:
        return None

    def sync(self, items: List[Dict[str, Any]], *, scope: Optional[Dict[str, Any]] = None) -> int:
        return 0
