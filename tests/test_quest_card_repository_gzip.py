"""The card store's reads ask for a compressed reply and read it.

Evidence 2026-11-20: the card repository called ``urllib.request.urlopen`` with no ``Accept-Encoding``, so every
``/api/cards`` read came down plain (Quest's nginx compresses JSON only when the client asks). The transport
(quest_client_transport) already asks for gzip; this repository did not. Offline: a local HTTP server stands in
for Quest and counts the bytes it sends.
"""
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from quest_ai_runner.body.internal_context.card_store.quest_api_card_repository import QuestApiCardRepository


def _payload():
    cards = [{"id": f"card_{i}", "title": "a card about the person's week", "body": "word " * 60} for i in range(200)]
    return json.dumps({"cards": cards, "data": {"cards": cards}}).encode()


@pytest.fixture()
def server():
    body = _payload()
    seen = {"accept_encoding": [], "sent": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            accept = self.headers.get("Accept-Encoding", "")
            seen["accept_encoding"].append(accept)
            out, headers = body, {"Content-Type": "application/json"}
            if "gzip" in accept:
                out = gzip.compress(body)
                headers["Content-Encoding"] = "gzip"
            self.send_response(200)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            seen["sent"] += len(out)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", seen, body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_card_read_asks_for_gzip_and_gets_the_same_json(server):
    base, seen, body = server
    repo = QuestApiCardRepository(base, "test-key", user_id="u1", store_id="s1")
    reply = repo._request("GET", "/api/cards/search")
    assert seen["accept_encoding"] == ["gzip"]
    assert reply == json.loads(body)
    assert seen["sent"] < len(body) / 3


def _serve(handler_get):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            handler_get(self)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _reply(handler, status, body, *, gzipped):
    out = gzip.compress(body) if gzipped else body
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    if gzipped:
        handler.send_header("Content-Encoding", "gzip")
    handler.send_header("Content-Length", str(len(out)))
    handler.end_headers()
    handler.wfile.write(out)


def test_plain_uncompressed_reply_is_read():
    httpd, base = _serve(lambda h: _reply(h, 200, b'{"cards": [{"id": "c1"}]}', gzipped=False))
    try:
        repo = QuestApiCardRepository(base, "test-key", user_id="u1", store_id="s1")
        assert repo._request("GET", "/api/cards/search") == {"cards": [{"id": "c1"}]}
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("gzipped", [True, False])
def test_error_detail_is_readable_whether_or_not_the_error_body_is_gzipped(gzipped):
    httpd, base = _serve(lambda h: _reply(h, 422, json.dumps({"detail": "card body too long"}).encode(), gzipped=gzipped))
    try:
        repo = QuestApiCardRepository(base, "test-key", user_id="u1", store_id="s1")
        with pytest.raises(Exception) as caught:
            repo._request("GET", "/api/cards/search")
        assert "card body too long" in str(caught.value) + str(getattr(caught.value, "detail", ""))
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_shared_decoder_handles_gzip_plain_and_fake_gzip():
    from quest_ai_runner.body.internal_context.quest.client.quest_client_support import decode_gzip_body
    assert decode_gzip_body(gzip.compress(b"hello"), {"Content-Encoding": "GZip"}) == b"hello"
    assert decode_gzip_body(b"hello", {}) == b"hello"
    assert decode_gzip_body(b"not gzip", {"Content-Encoding": "gzip"}) == b"not gzip"
    assert decode_gzip_body(b"x", None) == b"x"
