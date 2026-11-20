"""One per-process cache for the quest-scoped Quest reads an autopilot run repeats (home data cap, 2026-11-20).

One run read the same quest's goal list 4 to 12 times per 5 minutes (about 0.1 MB each), its state 3 to 4 times, its spec 5 times and
its task list twice, from separate clients that each kept their own copy. This cache lives in the process, so every client shares it.

Validity, by endpoint kind:

* ``token``: the quest's goal list and state. Valid while the quest's sync token (``POST /api/quests/sync-tokens``, a hash over the
  quest's state, notes, goals and check-ins) is unchanged. A token is trusted for ``TOKEN_FRESH_SECONDS``; after that one call asks for
  the tokens of every quest held here at once. A backend without the route (token unavailable) makes the entry a ``ttl`` one.
* ``ttl``: the team's quest read, the quest's spec and goal-scoped task lists, which the token does not cover. Valid for ``TTL_SECONDS``.

Every entry is dropped the moment this process writes to what it describes (``note_write``): a write whose path names a quest drops that
quest's entries; a write that names none (a goal id, an intent record) drops every quest entry, because it may have touched any;
a task create or status change drops the task lists only; a progress-only task patch drops nothing. Answers are deep-copied out, so a
caller that edits one never edits the cache. ``QAR_QUEST_READ_CACHE=0`` turns the cache off.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

TOKEN_FRESH_SECONDS = 5.0
TTL_SECONDS = 60.0
#: A token-validated entry is dropped after this long even when the token still matches (the server may render time-relative fields).
TOKEN_ENTRY_MAX_AGE_SECONDS = 600.0
MAX_ENTRIES = 48
ENV_VAR = "QAR_QUEST_READ_CACHE"

_QUEST_ID = re.compile(r"quest_[0-9a-f]{6,}")
_TOKEN_PATHS = (re.compile(r"^/api/teams/[^/]+/quests/(quest_[^/]+)/goals$"), re.compile(r"^/api/quests/(quest_[^/]+)/state$"))
_TTL_QUEST_PATHS = (re.compile(r"^/api/teams/[^/]+/quests/(quest_[^/]+)$"), re.compile(r"^/api/intents/specs/quest/(quest_[^/]+)$"))
#: POSTs that only read, and chatter that changes nothing a cached read describes.
_NEUTRAL_WRITES = ("/api/quests/sync-tokens", "/api/quests/autopilot-schedules", "/api/quest-ai/conversations/")
TASKS_PATH = "/api/assistant-tasks"


def enabled() -> bool:
    return str(os.environ.get(ENV_VAR, "1")).strip().lower() not in ("0", "false", "off", "no")


def _clean(params: Optional[Mapping[str, Any]]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in (params or {}).items() if v is not None))


class _Entry:
    __slots__ = ("payload", "kind", "quests", "token", "stored_at")

    def __init__(self, payload: Any, kind: str, quests: frozenset, token: Optional[str], stored_at: float) -> None:
        self.payload, self.kind, self.quests, self.token, self.stored_at = payload, kind, quests, token, stored_at


class ReadCache:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.RLock()
        self.entries: "OrderedDict[tuple, _Entry]" = OrderedDict()
        self.tokens: Dict[tuple, Tuple[str, float]] = {}      # (scope, quest) -> (token, checked_at)
        self.etags: "OrderedDict[tuple, tuple]" = OrderedDict()  # task-list etags shared by every client in the process
        self.hits = 0
        self.misses = 0

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()
            self.tokens.clear()
            self.etags.clear()
            self.hits = self.misses = 0

    @staticmethod
    def scope_of(client: Any) -> str:
        secret = str(getattr(client, "api_key", "") or "")
        return str(getattr(client, "base_url", "")) + "|" + hashlib.sha1(secret.encode()).hexdigest()[:8]

    # --- what is cacheable -------------------------------------------------------------------------------------------
    @staticmethod
    def classify(path: str, params: Optional[Mapping[str, Any]]) -> Optional[Tuple[str, str]]:
        """``(kind, quest_id)`` for a cacheable GET, else None."""
        for rx in _TOKEN_PATHS:
            m = rx.match(path)
            if m:
                return "token", m.group(1)
        for rx in _TTL_QUEST_PATHS:
            m = rx.match(path)
            if m:
                return "ttl", m.group(1)
        return None

    # --- reads -------------------------------------------------------------------------------------------------------
    def tokens_for(self, client: Any, scope: str, quest_ids: list) -> Dict[str, str]:
        """Current tokens, from memory when checked within ``TOKEN_FRESH_SECONDS``, else one call for all the ids; {} when unavailable."""
        now = self.clock()
        with self.lock:
            stale = [q for q in quest_ids if q not in {k[1] for k in self.tokens if k[0] == scope and now - self.tokens[k][1] < TOKEN_FRESH_SECONDS}]
            if not stale:
                return {q: self.tokens[(scope, q)][0] for q in quest_ids if (scope, q) in self.tokens}
            held = {q for e in self.entries.values() if e.kind == "token" for q in e.quests}
        ask = sorted(set(stale) | held)
        try:
            got = client.get_quest_sync_tokens(ask)
        except Exception:  # noqa: BLE001 -- a token is an optimisation, never a failure
            got = None
        with self.lock:
            if got is None:
                for q in ask:
                    self.tokens.pop((scope, q), None)
                return {}
            now = self.clock()
            for q in ask:
                if q in got:
                    self.tokens[(scope, q)] = (got[q], now)
                else:
                    self.tokens.pop((scope, q), None)
            return {q: self.tokens[(scope, q)][0] for q in quest_ids if (scope, q) in self.tokens}

    def lookup(self, client: Any, key: tuple, kind: str, quest_id: str) -> Tuple[bool, Any, Optional[str]]:
        """``(hit, payload, token_read_before)``: on a miss the token read now is returned so the stored answer carries it."""
        scope = key[0]
        with self.lock:
            entry = self.entries.get(key)
        token = None
        if kind == "token":
            token = self.tokens_for(client, scope, [quest_id]).get(quest_id)
        with self.lock:
            entry = self.entries.get(key)
            if entry is not None:
                age = self.clock() - entry.stored_at
                if entry.token is not None and token is not None:
                    valid = entry.token == token and age < TOKEN_ENTRY_MAX_AGE_SECONDS
                else:
                    valid = age < TTL_SECONDS
                if valid:
                    self.entries.move_to_end(key)
                    self.hits += 1
                    return True, copy.deepcopy(entry.payload), token
                self.entries.pop(key, None)
            self.misses += 1
        return False, None, token

    def store(self, key: tuple, kind: str, quests: frozenset, payload: Any, token: Optional[str]) -> None:
        with self.lock:
            self.entries[key] = _Entry(copy.deepcopy(payload), kind, quests, token, self.clock())
            self.entries.move_to_end(key)
            while len(self.entries) > MAX_ENTRIES:
                self.entries.popitem(last=False)

    # --- task lists (goal-scoped reads, plus the shared etag memory) -----------------------------------------------------
    def task_key(self, scope: str, params: Mapping[str, Any]) -> tuple:
        return (scope, TASKS_PATH, _clean({k: v for k, v in params.items() if k != "due_before"}))

    def get_etag(self, scope: str, key: tuple) -> Optional[tuple]:
        with self.lock:
            return self.etags.get((scope, key))

    def set_etag(self, scope: str, key: tuple, etag: Optional[str], tasks: Any) -> None:
        with self.lock:
            if etag:
                self.etags[(scope, key)] = (etag, copy.deepcopy(tasks))
                self.etags.move_to_end((scope, key))
                while len(self.etags) > MAX_ENTRIES:
                    self.etags.popitem(last=False)
            else:
                self.etags.pop((scope, key), None)

    # --- writes ------------------------------------------------------------------------------------------------------
    def note_write(self, method: str, path: str, body: Optional[Mapping[str, Any]] = None) -> None:
        """Drop what a write by this process may have changed (see the module doc). Called before and after the write."""
        if method == "GET" or any(path.startswith(p) for p in _NEUTRAL_WRITES):
            return
        with self.lock:
            if path.startswith(TASKS_PATH):
                changes_list = method == "POST" or (isinstance(body, Mapping) and bool({"status", "result", "goal_id"} & set(body)))
                if changes_list:
                    self._drop(lambda k, e: e.kind == "tasks")
                return
            ids = set(_QUEST_ID.findall(path))
            if isinstance(body, Mapping):
                for name in ("quest_id", "questId", "goal_id"):
                    if isinstance(body.get(name), str) and _QUEST_ID.fullmatch(body[name]):
                        ids.add(body[name])
            if ids:
                self._drop(lambda k, e: bool(e.quests & ids))
                for k in [k for k in self.tokens if k[1] in ids]:
                    self.tokens.pop(k, None)
            else:
                self._drop(lambda k, e: e.kind != "tasks")
                self.tokens.clear()

    def _drop(self, match: Callable[[tuple, _Entry], bool]) -> None:
        for k in [k for k, e in self.entries.items() if match(k, e)]:
            self.entries.pop(k, None)


#: The process-wide cache (tests clear it with ``READ_CACHE.clear()``).
READ_CACHE = ReadCache()
