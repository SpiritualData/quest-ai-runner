"""The Quest client's transport: one HTTP exchange, the retry policy around it, the typed write path and the
backend capability probe. ``QuestClient`` inherits it; every API-area mixin sends through ``_request`` and every
content write through ``_write``.

Retry policy (bounded, exponential backoff with jitter, ``Retry-After`` honoured):

* A GET is retried on a transport error, a 429 or a 5xx, up to ``read_retries`` times. A 4xx other than 429 is a final
  answer and is never retried. The long-poll channel (``timeout_override``) and the capability probe are never retried.
* A write is retried ONLY when it carries an ``Idempotency-Key`` (one stable key per logical write, reused by every
  attempt) AND write retry is on. Write retry is ``QAR_QUEST_WRITE_RETRY``: ``on`` forces it, ``off`` forbids it, and
  ``auto`` (the default) turns it on only if the backend says it honours the key (``capabilities`` on ``GET
  /api/health`` lists ``idempotency-key``). A backend that ignores the header would create a second record for every
  retry, so with no evidence the retry stays off and the write is attempted once.
* A ``Retry-After`` longer than ``retry_max_delay`` is reported to the caller (``QuestApiError.retry_after``), not waited out.

Settings (environment, read when the client is built; a constructor argument wins): ``QAR_QUEST_TIMEOUT_SECONDS``
(default 30), ``QAR_QUEST_READ_RETRIES`` (default 2, at most 5), ``QAR_QUEST_WRITE_RETRY`` (``auto``, ``on``, ``off``) and
``QAR_QUEST_RETRY_MAX_DELAY_SECONDS`` (default 30).
"""
from __future__ import annotations

import errno
import json
import os
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, FrozenSet, Mapping, Optional

from ..core.tools_status import ToolStatus
from .quest_client_support import (
    QuestApiError,
    QuestNotConfigured,
    QuestWriteError,
    WriteResult,
    log,
    parse_retry_after,
)

QAR_QUEST_TIMEOUT_ENV_VAR = "QAR_QUEST_TIMEOUT_SECONDS"
QAR_QUEST_READ_RETRIES_ENV_VAR = "QAR_QUEST_READ_RETRIES"
QAR_QUEST_WRITE_RETRY_ENV_VAR = "QAR_QUEST_WRITE_RETRY"
QAR_QUEST_RETRY_MAX_DELAY_ENV_VAR = "QAR_QUEST_RETRY_MAX_DELAY_SECONDS"

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_READ_RETRIES = 2
MAX_RETRIES = 5
DEFAULT_WRITE_RETRIES = 2
DEFAULT_RETRY_MAX_DELAY_SECONDS = 30.0
BACKOFF_BASE_SECONDS = 0.5
WRITE_RETRY_MODES = ("auto", "on", "off")
IDEMPOTENCY_HEADER = "Idempotency-Key"
#: The capability a backend lists on its health route when it honours ``Idempotency-Key`` on creates.
IDEMPOTENCY_CAPABILITY = "idempotency-key"
HEALTH_PATH = "/api/health"


def new_idempotency_key() -> str:
    """A fresh key for one logical write (a UUID per call; every retry of that call reuses it)."""
    return uuid.uuid4().hex


def env_number(env: Mapping[str, str], name: str, default: float, *, minimum: float, maximum: float) -> float:
    """``env[name]`` as a number within ``[minimum, maximum]``; the default when unset, malformed or out of range."""
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    if not minimum <= value <= maximum:
        log.warning("%s=%r is outside %s..%s; using %s", name, raw, minimum, maximum, default)
        return default
    return value


def never_reached(error: BaseException) -> bool:
    """True when the failure proves the request was not delivered (refused connection, unknown host, no route)."""
    if isinstance(error, urllib.error.URLError):
        error = error.reason if isinstance(error.reason, BaseException) else error
    return isinstance(error, (ConnectionRefusedError, socket.gaierror)) or (
        isinstance(error, OSError) and error.errno in (errno.ECONNREFUSED, errno.ENETUNREACH, errno.EHOSTUNREACH))


def parse_capabilities(payload: Any) -> FrozenSet[str]:
    """The capability names a health answer lists (``capabilities`` at the top or inside ``data``), else none."""
    for holder in (payload, (payload or {}).get("data") if isinstance(payload, dict) else None):
        if isinstance(holder, dict) and holder.get("capabilities"):
            raw = holder["capabilities"]
            names = [k for k, v in raw.items() if v] if isinstance(raw, dict) else list(raw)
            return frozenset(str(n).strip().lower() for n in names if str(n).strip())
    return frozenset()


class ExchangeState(threading.local):
    """Per-thread facts about the request in flight: the idempotency key a write is sending, the HTTP status of the
    last answer, and whether the capability probe is running. Thread-local so concurrent tool calls never share a key."""

    def __init__(self) -> None:
        self.write_key: Optional[str] = None
        self.last_status: Optional[int] = None
        self.probing = False


class TransportMixin:
    """HTTP exchange, retry, typed writes and the capability probe for ``QuestClient``."""

    def init_transport(self, *, timeout: Optional[float], read_retries: Optional[int],
                       write_retry: Optional[str], retry_max_delay: Optional[float],
                       env: Optional[Mapping[str, str]] = None) -> None:
        env = os.environ if env is None else env
        self.timeout = float(timeout) if timeout else env_number(
            env, QAR_QUEST_TIMEOUT_ENV_VAR, DEFAULT_TIMEOUT_SECONDS, minimum=0.1, maximum=3600)
        self.read_retries = int(read_retries) if read_retries is not None else int(env_number(
            env, QAR_QUEST_READ_RETRIES_ENV_VAR, DEFAULT_READ_RETRIES, minimum=0, maximum=MAX_RETRIES))
        self.write_retries = DEFAULT_WRITE_RETRIES
        self.retry_max_delay = float(retry_max_delay) if retry_max_delay else env_number(
            env, QAR_QUEST_RETRY_MAX_DELAY_ENV_VAR, DEFAULT_RETRY_MAX_DELAY_SECONDS, minimum=0.0, maximum=3600)
        mode = str(write_retry or env.get(QAR_QUEST_WRITE_RETRY_ENV_VAR) or "auto").strip().lower()
        if mode not in WRITE_RETRY_MODES:
            log.warning("%s=%r is not one of %s; using auto", QAR_QUEST_WRITE_RETRY_ENV_VAR, mode,
                        "/".join(WRITE_RETRY_MODES))
            mode = "auto"
        self.write_retry = mode
        self.capabilities: Optional[FrozenSet[str]] = None
        # Seams for tests: the pause between attempts, and the jitter source.
        self.sleep = time.sleep
        self.jitter = random.uniform

    @property
    def exchange_state(self) -> ExchangeState:
        """The per-thread exchange facts, created on first use (a client built without ``__init__`` still works)."""
        holder = self.__dict__.get("exchange_holder")
        if holder is None:
            holder = self.__dict__["exchange_holder"] = ExchangeState()
        return holder

    # --- capability probe ---------------------------------------------------------------------

    def backend_capabilities(self) -> FrozenSet[str]:
        """What the backend says it supports, read once from ``GET /api/health`` and cached.

        Quest's health route is public and read-only. A backend that lists no ``capabilities``, answers with an error or
        does not answer at all is treated as supporting nothing optional: the client logs that once and leaves
        write retry off. It is never an error, so an older or newer backend keeps working."""
        if self.__dict__.get("capabilities") is not None:
            return self.capabilities
        state = self.exchange_state
        saved = (state.write_key, state.last_status, state.probing)
        state.write_key, state.probing = None, True
        reason = ""
        try:
            found = parse_capabilities(self._request("GET", HEALTH_PATH))
        except (QuestApiError, QuestNotConfigured) as e:
            found, reason = frozenset(), f" (the health check failed: {e})"
        finally:
            state.write_key, state.last_status, state.probing = saved
        self.capabilities = found
        if IDEMPOTENCY_CAPABILITY not in found:
            log.info("Quest backend does not advertise idempotent writes%s; write retry is off, so a failed write is "
                     "attempted once. Set %s=on to retry anyway.", reason, QAR_QUEST_WRITE_RETRY_ENV_VAR)
        return found

    def write_retry_enabled(self) -> bool:
        """Whether a write that carries an Idempotency-Key may be retried (see the module doc)."""
        if self.write_retry == "on":
            return True
        if self.write_retry == "off" or self.exchange_state.probing:
            return False
        return IDEMPOTENCY_CAPABILITY in self.backend_capabilities()

    # --- one exchange, with retry ---------------------------------------------------------------

    def attempt(self, method: str, path: str, *, params: Optional[Dict[str, Any]], body: Optional[Dict[str, Any]],
                timeout: float, key: Optional[str]) -> "tuple[int, Any]":
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        if key:
            req.add_header(IDEMPOTENCY_HEADER, key)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                try:
                    payload = json.loads(raw) if raw else None
                except ValueError:
                    payload = None  # a 2xx whose body is not JSON: the request itself was answered
                return int(getattr(resp, "status", 200) or 200), payload
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            retry_after = parse_retry_after((e.headers or {}).get("Retry-After") if e.headers is not None else None)
            raise QuestApiError(f"Quest API {method} {path} -> {e.code}: {detail}", status=e.code,
                                retry_after=retry_after, transient=e.code == 429 or e.code >= 500) from e
        except urllib.error.URLError as e:
            raise QuestApiError(f"Quest API {method} {path} unreachable: {e.reason}", transient=True,
                                outcome_unknown=not never_reached(e)) from e
        except (TimeoutError, OSError) as e:
            # A socket-level timeout/reset during the read escapes urlopen as a raw TimeoutError/OSError, NOT a
            # URLError. Wrapped here so every "never raises" caller contract holds. The request was sent, so a
            # write that ends this way may have been applied.
            raise QuestApiError(f"Quest API {method} {path} transport error: {e}", transient=True,
                                outcome_unknown=not never_reached(e)) from e

    def retries_for(self, method: str, key: Optional[str], timeout_override: Optional[float]) -> int:
        if timeout_override or self.exchange_state.probing:
            return 0
        if method == "GET":
            return self.read_retries
        if key and self.write_retry_enabled():
            return self.write_retries
        return 0

    def backoff(self, attempt: int, error: QuestApiError) -> Optional[float]:
        """Seconds to wait before retry number ``attempt + 1``, or None to stop (a Retry-After beyond the cap)."""
        if error.retry_after is not None:
            return error.retry_after if error.retry_after <= self.retry_max_delay else None
        ceiling = min(self.retry_max_delay, BACKOFF_BASE_SECONDS * (2 ** attempt))
        return ceiling / 2 + self.jitter(0, ceiling / 2)

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None, timeout_override: Optional[float] = None) -> Any:
        self._require()
        state = self.exchange_state
        key = state.write_key
        retries = self.retries_for(method, key, timeout_override)
        attempt = 0
        while True:
            try:
                status, payload = self.attempt(method, path, params=params, body=body,
                                               timeout=timeout_override or self.timeout, key=key)
            except QuestApiError as e:
                wait = self.backoff(attempt, e) if (e.transient and attempt < retries) else None
                if wait is None:
                    raise
                log.info("Quest API %s %s failed (%s); retry %d of %d in %.1fs", method, path,
                         e.status or "transport", attempt + 1, retries, wait)
                self.sleep(wait)
                attempt += 1
                continue
            state.last_status = status
            return payload

    # --- the typed write path -------------------------------------------------------------------

    def write_error(self, label: str, error: QuestApiError, *, not_done: bool = False) -> QuestWriteError:
        """Classify a failed write: 4xx (except 429) is ``refused``; 429 and 503 are ``failed`` (the request was not
        processed); any other 5xx (a 500, 502 or 504 may follow a commit) and a request that was sent and never answered are
        ``unknown``; one that never left is ``failed``."""
        status = error.status
        detail = self.error_detail(error)
        if status is not None and 400 <= status < 500 and status != 429:
            kind = ToolStatus.REFUSED
            message = f"Not done: {detail}" if not_done else f"{label} was refused by Quest ({status}): {detail}"
        elif status is not None and status >= 500 and status != 503:
            # A write the server (or a gateway in front of it) answered 500, 502 or 504 to may have been applied before the
            # error: a proxy times out while the backend commits, a handler fails after its insert. Only 503 (unavailable)
            # says the request was not processed. A write is never a GET, so this is the write path's reading of a 5xx.
            kind = ToolStatus.UNKNOWN
            message = (f"{label}: OUTCOME UNKNOWN. Quest answered {status} ({detail}), which can come back after the write was "
                       f"applied, so it may or may not have happened. Look before trying again.")
            return QuestWriteError(message, write_status=kind, status=status, retry_after=error.retry_after,
                                   outcome_unknown=True, detail=detail)
        elif status is not None:
            kind = ToolStatus.FAILED
            wait = f" Quest asked to wait {error.retry_after:.0f}s before trying again." if error.retry_after else ""
            message = f"{label} did not happen: Quest answered {status} ({detail}).{wait}"
        elif error.outcome_unknown:
            kind = ToolStatus.UNKNOWN
            message = (f"{label}: OUTCOME UNKNOWN. The request was sent but Quest did not answer ({detail}), so it "
                       f"may or may not have been applied. Look before trying again.")
        else:
            kind = ToolStatus.FAILED
            message = f"{label} did not happen: Quest could not be reached ({detail})."
        return QuestWriteError(message, write_status=kind, status=status, retry_after=error.retry_after,
                               outcome_unknown=error.outcome_unknown, detail=detail)

    def _write(self, method: str, path: str, *, label: str, body: Optional[Dict[str, Any]] = None,
               params: Optional[Dict[str, Any]] = None, idempotency_key: Optional[str] = None,
               not_done: bool = False) -> WriteResult:
        """One content write: a ``WriteResult`` (applied, held or noop) or a raised ``QuestWriteError``; never an empty
        value. ``idempotency_key`` is sent as the ``Idempotency-Key`` header (and is what makes a retry safe)."""
        state = self.exchange_state
        state.write_key, state.last_status = idempotency_key or None, None
        try:
            payload = self._request(method, path, params=params, body=body)
        except QuestApiError as e:
            if isinstance(e, QuestWriteError) or getattr(e, "write_status", None) is not None:
                raise
            raise self.write_error(label, e, not_done=not_done) from e
        finally:
            state.write_key = None
        return WriteResult.from_payload({} if payload is None else payload, state.last_status)
