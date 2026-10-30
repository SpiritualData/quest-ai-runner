"""A scripted stand-in for the network, so client tests never touch a socket or sleep.

``Transport([step, ...])`` is installed as the client's ``urlopen``. Each step is one answer, used in order (the
last repeats): ``(status, body_dict_or_list, headers)``, or an exception instance to raise. A step for an error
status (>= 400) raises the ``HTTPError`` urllib would. ``requests`` records every request as ``(method, path, headers,
body, timeout)``.
"""
import io
import json
import urllib.error
import urllib.request

from quest_ai_runner.runner.quest_client import QuestClient


class FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self.raw = b"" if payload is None else json.dumps(payload).encode()

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Transport:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, req, timeout=None):
        path = req.full_url.split("quest.example", 1)[1]
        self.requests.append((req.get_method(), path, {k.lower(): v for k, v in req.header_items()},
                              json.loads(req.data) if req.data else None, timeout))
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        status, payload, *rest = step
        headers = rest[0] if rest else {}
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "error", headers,
                                         io.BytesIO(json.dumps(payload).encode()))
        return FakeResponse(status, payload, headers)

    def paths(self):
        return [(m, p) for m, p, *_ in self.requests]


def make_client(steps, monkeypatch, **settings):
    """A client wired to a scripted transport and a recording sleep (no real waiting, no random jitter)."""
    transport = Transport(steps)
    monkeypatch.setattr(urllib.request, "urlopen", transport)
    client = QuestClient("https://quest.example", "test-api-key", team_id="team_1", **settings)
    client.sleeps = []
    client.sleep = client.sleeps.append
    client.jitter = lambda low, high: high
    return client, transport
