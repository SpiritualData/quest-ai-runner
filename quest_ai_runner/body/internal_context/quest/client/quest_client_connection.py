"""Keep-alive HTTP(S) for the Quest client: one connection per (scheme, host, thread), reused across requests.

``urllib.request.urlopen`` opens a new connection per request, so every call paid a TLS handshake (about 10 KB both ways;
~67 a minute on a capped home line, 2026-11-20). ``open_url`` has urlopen's contract for the transport (a response with
``read``/``headers``/``status``, ``urllib.error.HTTPError`` for a 4xx/5xx, ``URLError`` when the host could not be reached,
a raw ``OSError``/``TimeoutError`` for a failure mid-exchange), so callers and their retry policy are unchanged.

A reused connection the server has closed meanwhile fails before any reply arrives; that one case is retried once on a
fresh connection. Connections are per thread (``http.client`` connections are not thread-safe) and never shared.
"""
from __future__ import annotations

import http.client
import io
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

_local = threading.local()

# A reply never arrived on a connection that was open and idle: the server closed it, the request was not processed.
_STALE = (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
          http.client.CannotSendRequest, http.client.ResponseNotReady, http.client.BadStatusLine)


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
    conn.request(req.get_method(), target, body=req.data, headers=dict(req.header_items()))
    resp = conn.getresponse()
    return resp, resp.read()


def open_url(req: urllib.request.Request, timeout: Optional[float] = None) -> Response:
    parts = urllib.parse.urlsplit(req.full_url)
    key = (parts.scheme, parts.netloc)
    target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    pool = _connections()
    for fresh in (False, True):
        conn = pool.get(key)
        reused = conn is not None and not fresh
        if conn is None or fresh:
            if conn is not None:
                conn.close()
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = pool[key] = cls(parts.netloc, timeout=timeout)
        try:
            resp, raw = _exchange(conn, req, target, timeout)
        except _STALE:
            pool.pop(key, None)
            conn.close()
            if reused:
                continue          # the idle connection had been closed by the server: once more on a fresh one
            raise
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
