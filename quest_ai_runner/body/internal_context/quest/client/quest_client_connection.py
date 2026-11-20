"""Keep-alive HTTP(S) for the Quest client: one connection per (scheme, host, thread), reused across requests.

``urllib.request.urlopen`` opens a new connection per request, so every call paid a TLS handshake (about 10 KB both ways;
~67 a minute on a capped home line, 2026-11-20). ``open_url`` has urlopen's contract for the transport (a response with
``read``/``headers``/``status``, ``urllib.error.HTTPError`` for a 4xx/5xx, ``URLError`` when the host could not be reached,
a raw ``OSError``/``TimeoutError`` for a failure mid-exchange), so callers and their retry policy are unchanged.

A pooled connection the server has already closed (it reads as readable while idle) is replaced before the request
goes out. A reused connection that still fails is sent again once on a fresh connection only when that cannot apply the
request twice: the method is idempotent (GET, HEAD, PUT, DELETE, OPTIONS), or the failure came while sending, before the
request was fully written (``CannotSendRequest``, ``BrokenPipeError``). A POST or PATCH whose reply was lost may already
have been applied (a message claim consumed, a task created), so that error is raised and the caller's own retry policy
(an Idempotency-Key, or none) decides. Connections are per thread (``http.client`` connections are not thread-safe) and
never shared.
"""
from __future__ import annotations

import http.client
import io
import select
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

_local = threading.local()

# Methods a second send cannot apply twice (RFC 9110 9.2.2).
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})
# Failed while SENDING on a reused connection: the request was not fully written, so the server cannot have processed it.
NOT_SENT_ERRORS = (http.client.CannotSendRequest, BrokenPipeError)
# Failed after the request was written (no reply, a reset, a garbled status line): the server may or may not have processed it.
NO_REPLY_ERRORS = (http.client.RemoteDisconnected, ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
             http.client.ResponseNotReady, http.client.BadStatusLine)


class SendFailed(Exception):
    """Wraps an error raised while writing the request, so it is told apart from one raised while reading the reply."""

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class Response(io.BytesIO):
    """A fully read reply, shaped like the object ``urlopen`` returns (what the transport reads)."""

    def __init__(self, raw: bytes, status: int, headers) -> None:
        super().__init__(raw)
        self.status, self.headers = status, headers

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _connections() -> Dict[Tuple[str, str], http.client.HTTPConnection]:
    pool = getattr(_local, "pool", None)
    if pool is None:
        pool = _local.pool = {}
    return pool


def close_all() -> None:
    """Close this thread's pooled connections."""
    for conn in _connections().values():
        conn.close()
    _connections().clear()


def _exchange(conn: http.client.HTTPConnection, req: urllib.request.Request, target: str, timeout: Optional[float]):
    if timeout is not None:
        conn.timeout = timeout
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
    try:
        conn.request(req.get_method(), target, body=req.data, headers=dict(req.header_items()))
    except NOT_SENT_ERRORS as e:
        raise SendFailed(e) from e
    resp = conn.getresponse()
    return resp, resp.read()


def connection_dropped(conn: http.client.HTTPConnection) -> bool:
    """True when an idle pooled connection is unusable: readable with no request out means the server closed it (or sent
    something unasked). Checked before reuse so a closed keep-alive never costs a write its only send."""
    sock = conn.sock
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


def as_os_error(error: BaseException) -> OSError:
    """A garbled or missing reply (``http.client.HTTPException``) as a ``ConnectionError``, which the transport reports
    like any other mid-exchange failure (``BadStatusLine`` is not an ``OSError`` and would otherwise escape it)."""
    return ConnectionError(f"{type(error).__name__}: {error}")


def open_url(req: urllib.request.Request, timeout: Optional[float] = None) -> Response:
    parts = urllib.parse.urlsplit(req.full_url)
    key = (parts.scheme, parts.netloc)
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    pool = _connections()
    idempotent = req.get_method().upper() in IDEMPOTENT_METHODS
    for fresh in (False, True):
        conn = pool.get(key)
        if conn is not None and not fresh and connection_dropped(conn):
            fresh = True          # the server closed it while idle: replace it before anything is sent
        reused = conn is not None and not fresh
        if conn is None or fresh:
            if conn is not None:
                conn.close()
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = pool[key] = cls(parts.netloc, timeout=timeout)
        try:
            resp, raw = _exchange(conn, req, target, timeout)
        except SendFailed as e:
            pool.pop(key, None)
            conn.close()
            if reused:
                continue          # not fully written, so not processed: once more on a fresh connection, any method
            raise e.error from None
        except NO_REPLY_ERRORS as e:
            pool.pop(key, None)
            conn.close()
            if reused and idempotent:
                continue          # a second send of an idempotent request is safe: once more on a fresh connection
            if isinstance(e, OSError):
                raise             # may have been applied: the caller's retry policy decides
            raise as_os_error(e) from e
        except (socket.gaierror, ConnectionRefusedError) as e:   # never reached the host: urlopen's URLError
            pool.pop(key, None)
            conn.close()
            raise urllib.error.URLError(e) from e
        except BaseException:
            pool.pop(key, None)
            conn.close()
            raise
        if resp.will_close:
            pool.pop(key, None)
            conn.close()
        if resp.status >= 400 or 300 <= resp.status < 400:
            raise urllib.error.HTTPError(req.full_url, resp.status, resp.reason, resp.msg, io.BytesIO(raw))
        return Response(raw, resp.status, resp.msg)
    raise AssertionError("unreachable")
