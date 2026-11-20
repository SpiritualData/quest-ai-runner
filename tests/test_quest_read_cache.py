"""A run reads the same quest several times; repeats are answered in process while the quest's sync token is unchanged.

Evidence 2026-11-20 (sd-shared lane, per 5 minutes): the goal list x12 = 1.27 MB, state x3-4, quest x3, spec x5, goal task list 1 MB.
"""
import json

from quest_ai_runner.body.internal_context.quest.client.quest_client import QuestClient
from quest_ai_runner.body.internal_context.quest.client import quest_client_read_cache as rc

Q = "quest_0123456789ab"
GOALS = f"/api/teams/team_1/quests/{Q}/goals"
STATE = f"/api/quests/{Q}/state"
SPEC = f"/api/intents/specs/quest/{Q}"
BIG = {"period_groups": [{"goals": [{"id": f"g{i}", "name": "x" * 400} for i in range(40)]}]}


class Resp:
    def __init__(self, payload):
        self.status, self.headers, self.raw = 200, {}, json.dumps(payload).encode()

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Backend:
    """Fake Quest: serves the quest reads, one token that changes on any write, counts requests and bytes."""

    def __init__(self, tokens=True):
        self.version, self.log, self.bytes, self.tokens = 1, [], 0, tokens
        self.task_rev = 1

    def __call__(self, req, timeout=None):
        path = req.full_url.split("quest.example", 1)[1]
        route = path.split("?")[0]
        self.log.append((req.get_method(), path))
        if req.get_method() != "GET" and route != "/api/quests/sync-tokens":
            self.version += 1
            return Resp({"ok": True})
        if route == "/api/quests/sync-tokens":
            if not self.tokens:
                import io, urllib.error
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
            return Resp({"tokens": {Q: f"t{self.version}"}})
        if route == "/api/assistant-tasks":
            if "if_none_match=r1" in path:
                return Resp({"tasks": [], "count": 0, "unchanged": True, "etag": "r1"})
            payload = {"tasks": [{"task_id": "t", "status": "done", "result": "r" * 2000}], "etag": "r1"}
        else:
            payload = dict(BIG, v=self.version)
        resp = Resp(payload)
        self.bytes += len(resp.raw)
        return resp

    def reads(self, needle):
        return sum(1 for m, p in self.log if m == "GET" and needle in p)


def make(monkeypatch, tokens=True):
    backend = Backend(tokens)
    monkeypatch.setattr("quest_ai_runner.body.internal_context.quest.client.quest_client_transport.open_url", backend)
    return QuestClient("https://quest.example", "k", team_id="team_1"), backend


def test_repeat_goal_reads_hit_the_wire_once(monkeypatch):
    client, backend = make(monkeypatch)
    first = client.list_quest_goals(Q)
    for _ in range(11):
        assert client.list_quest_goals(Q) == first
    assert backend.reads("/goals") == 1


def test_a_second_client_in_the_process_shares_the_cache(monkeypatch):
    client, backend = make(monkeypatch)
    client.list_quest_goals(Q)
    QuestClient("https://quest.example", "k", team_id="team_1").list_quest_goals(Q)
    assert backend.reads("/goals") == 1


def test_a_changed_token_refetches(monkeypatch):
    client, backend = make(monkeypatch)
    client.list_quest_goals(Q)
    backend.version += 1                           # someone else edited the quest
    rc.READ_CACHE.tokens.clear()                   # the token memo window has passed
    client.list_quest_goals(Q)
    assert backend.reads("/goals") == 2


def test_own_write_invalidates_at_once_even_inside_the_token_window(monkeypatch):
    client, backend = make(monkeypatch)
    client.list_quest_goals(Q)
    client._request("POST", f"/api/quests/{Q}/context-entries", body={"x": 1})
    client.list_quest_goals(Q)
    assert backend.reads("/goals") == 2


def test_a_write_naming_no_quest_drops_every_quest_entry(monkeypatch):
    client, backend = make(monkeypatch)
    client.list_quest_goals(Q)
    client._request("PUT", "/api/planning/goals/goal_1", body={"name": "n"})
    client.list_quest_goals(Q)
    assert backend.reads("/goals") == 2


def test_state_and_spec_and_quest_reads_repeat_once(monkeypatch):
    client, backend = make(monkeypatch)
    for _ in range(4):
        client._request("GET", STATE)
        client._request("GET", f"/api/teams/team_1/quests/{Q}")
        client._request("GET", SPEC)
    assert backend.reads("/state") == 1 and backend.reads(SPEC) == 1


def test_without_tokens_a_short_ttl_applies(monkeypatch):
    client, backend = make(monkeypatch, tokens=False)
    now = [1000.0]
    monkeypatch.setattr(rc.READ_CACHE, "clock", lambda: now[0])
    client.list_quest_goals(Q)
    client.list_quest_goals(Q)
    assert backend.reads("/goals") == 1
    now[0] += rc.TTL_SECONDS + 1
    client.list_quest_goals(Q)
    assert backend.reads("/goals") == 2


def test_caller_edits_never_reach_the_cache(monkeypatch):
    client, backend = make(monkeypatch)
    client.list_quest_goals(Q)["period_groups"].clear()
    assert client.list_quest_goals(Q)["period_groups"]


def test_goal_task_list_repeats_once_and_a_task_status_write_refreshes(monkeypatch):
    client, backend = make(monkeypatch)
    for _ in range(3):
        client.list_tasks(goal_id=Q, without_progress=True)
    assert backend.reads("/api/assistant-tasks") == 1
    client._request("PATCH", "/api/assistant-tasks/t1", body={"status": "done"})
    client.list_tasks(goal_id=Q, without_progress=True)
    assert backend.reads("/api/assistant-tasks") == 2          # the etag answers it: unchanged, no rows downloaded


def test_etag_memory_survives_a_new_client(monkeypatch):
    client, backend = make(monkeypatch)
    client.list_tasks(team_id="team_1", status="queued")
    QuestClient("https://quest.example", "k", team_id="team_1").list_tasks(team_id="team_1", status="queued")
    assert any("if_none_match=r1" in p for _, p in backend.log)


def test_projected_bytes_per_run(monkeypatch):
    """The call pattern of one autopilot run (12 goal reads, 4 state, 5 spec, 3 quest, 2 goal task lists)."""
    def run(enabled):
        monkeypatch.setenv(rc.ENV_VAR, "1" if enabled else "0")
        rc.READ_CACHE.clear()
        client, backend = make(monkeypatch)
        for _ in range(12):
            client.list_quest_goals(Q)
        for _ in range(4):
            client._request("GET", STATE)
        for _ in range(5):
            client._request("GET", SPEC)
        for _ in range(3):
            client._request("GET", f"/api/teams/team_1/quests/{Q}")
        for _ in range(2):
            client.list_tasks(goal_id=Q, without_progress=True)
        return sum(1 for m, p in backend.log if m == "GET" and "sync" not in p), backend.bytes
    before, after = run(False), run(True)
    print("reads/bytes before", before, "after", after)
    assert after[0] <= 5 and after[1] < before[1] / 3
