"""The Quest client asks for gzip and reads a compressed reply (a list read was 11 MB plain; the server compresses it to a few percent)."""
import gzip
import io
import json
import urllib.request

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient


class _Resp(io.BytesIO):
    def __init__(self, raw, headers):
        super().__init__(raw)
        self.headers, self.status = headers, 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _client():
    return QuestClient("https://quest.example", "test-api-key", team_id="team_1")


def test_request_asks_for_gzip_and_decodes_a_gzip_reply(monkeypatch):
    seen = {}
    body = {"tasks": [{"id": "atask_1", "text": "x" * 5000}]}

    def fake(req, timeout=None):
        seen["accept"] = req.get_header("Accept-encoding")
        return _Resp(gzip.compress(json.dumps(body).encode()), {"Content-Encoding": "gzip"})

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert _client()._request("GET", "/api/assistant-tasks") == body
    assert seen["accept"] == "gzip"


def test_a_plain_reply_from_a_server_that_does_not_compress_still_reads(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp(b'{"ok": true}', {}))
    assert _client()._request("GET", "/api/quests/me") == {"ok": True}
