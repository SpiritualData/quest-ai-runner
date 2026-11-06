"""A world quest's field the AI changed must be restorable: an API key's PATCH is only parked as a proposal."""
import json
import subprocess

import world as W


def test_restore_writes_in_process_then_busts_the_server_cache(monkeypatch):
    calls = {"plan": None, "api": []}

    def fake_run(argv, **kwargs):
        calls["plan"] = json.load(open(argv[-1]))
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(W, "api", lambda method, path, body=None, **kw: calls["api"].append((method, path)) or (200, []))
    monkeypatch.setattr(W, "notes_of", lambda qid: [{"id": "n1", "text": f"{W.TAG} cache bust"}])
    W.restore_quest_fields("research", "quest_x")
    fields = calls["plan"]["quest_x"]
    assert fields["current_state"] == W.QUESTS["research"]["current_state"]
    assert fields["acceptance_criteria"].endswith(f"({W.TAG})")
    assert ("PATCH", "/api/quests/quest_x/field") not in calls["api"]
    assert ("POST", "/api/quests/quest_x/notes") in calls["api"]
    assert ("DELETE", "/api/quests/quest_x/notes/n1") in calls["api"]
