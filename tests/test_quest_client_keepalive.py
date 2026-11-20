"""The Quest client reuses one keep-alive connection per host and thread: a TLS handshake per request cost ~10 KB each,
about 67 a minute on a capped home line (2026-11-20)."""
import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestApiError, QuestClient


class _Server:
    def __init__(self):
        outer = self
        self.connections = 0
        self.requests = []
        self.drop_after_reply = False      # close the socket after replying WITHOUT telling the client (a stale keep-alive)
        self.close_header = False          # tell the client "Connection: close"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                outer.connections += 1

            def log_message(self, *a):
                pass

            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                if self.path.startswith("/missing"):
                    code, payload = 404, {"detail": "nope"}
                else:
                    code, payload = 200, {"ok": True, "n": len(outer.requests)}
                raw = gzip.compress(json.dumps(payload).encode())
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(raw)))
                if outer.close_header:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)
                if outer.drop_after_reply:
                    self.close_connection = True

            do_GET = do_POST = do_PATCH = _reply

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.stop()


def client_for(server):
    return QuestClient(server.url, "test-api-key", team_id="team_1")


def test_sequential_requests_share_one_connection(server):
    client = client_for(server)
    for _ in range(5):
        assert client._request("GET", "/api/x")["ok"] is True
    assert server.connections == 1


def test_gzip_is_still_asked_for_and_auth_is_sent(server):
    client_for(server)._request("GET", "/api/x")
    headers = {k.lower(): v for k, v in server.requests[0][2].items()}
    assert headers["accept-encoding"] == "gzip" and headers["authorization"] == "Bearer test-api-key"


def test_a_post_body_and_an_error_status_ride_the_same_connection(server):
    client = client_for(server)
    client._request("POST", "/api/y", body={"a": 1})
    with pytest.raises(QuestApiError) as err:
        client._request("GET", "/missing")
    assert err.value.status == 404 and "nope" in str(err.value)
    client._request("GET", "/api/x")
    assert server.requests[0][3] == b'{"a": 1}' and server.connections == 1


def test_a_connection_the_server_closed_behind_our_back_is_replaced_without_failing(server):
    server.drop_after_reply = True
    client = client_for(server)
    assert client._request("GET", "/api/x")["n"] == 1
    assert client._request("GET", "/api/x")["n"] == 2
    assert server.connections == 2


def test_a_connection_close_reply_is_not_reused(server):
    server.close_header = True
    client = client_for(server)
    client._request("GET", "/api/x")
    client._request("GET", "/api/x")
    assert server.connections == 2


def test_each_thread_has_its_own_connection(server):
    client = client_for(server)
    client._request("GET", "/api/x")
    t = threading.Thread(target=lambda: client._request("GET", "/api/x"))
    t.start()
    t.join()
    client._request("GET", "/api/x")
    assert server.connections == 2


def test_an_unreachable_host_is_still_a_quest_api_error():
    client = QuestClient("http://127.0.0.1:1", "k", team_id="t", read_retries=0)
    with pytest.raises(QuestApiError):
        client._request("GET", "/api/x")
