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
