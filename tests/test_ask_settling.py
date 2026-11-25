"""Answered asks leave the waiting queue by every path that acts on them (exact decision id only)."""
from quest_ai_runner.body.intake_sources import ask_settling as a
from quest_ai_runner.body.intake_sources.context_update_bundle import ContextUpdates
from quest_ai_runner.body.internal_context.quest.client.quest_client_support import WriteResult, ToolStatus


class Client:
    def __init__(self, pending=(), tasks=(), notes=(), fail=0):
        self.pending, self.tasks, self.notes, self.fail = list(pending), list(tasks), list(notes), fail
        self.marked = []

    def list_pending_answers(self):
        return self.pending

    def list_tasks(self, **kw):
        return self.tasks

    def list_quest_notes(self, q):
        return self.notes

    def mark_decisions_acted(self, ids, task_id=None):
        self.marked.append(list(ids))
        if self.fail:
            self.fail -= 1
            return WriteResult({}, ToolStatus.FAILED)
        return WriteResult({"acted": list(ids)})


def _bundle(client, q="q1"):
    return ContextUpdates(card_id=q, relay_client=client)


def setup_function(_):
    a.UNSETTLED.clear()


def test_ids_the_run_names_are_settled_when_pending_on_the_quest_only():
    c = Client(pending=[{"decision_id": "teamdec_a", "quest_id": "q1"}, {"decision_id": "teamdec_b", "quest_id": "q2"}])
    sent = _bundle(c).settle_answered_asks("t", "Did teamdec_a and teamdec_b and teamdec_zzz.")
    assert sent == ["teamdec_a"] and c.marked == [["teamdec_a"]]


def test_no_action_line_and_executed_parked_are_settled():
    c = Client(pending=[{"decision_id": "teamdec_n", "quest_id": "q1"},
                        {"decision_id": "teamdec_p", "quest_id": "q1", "ai_action_parked": True, "executed": True},
                        {"decision_id": "teamdec_w", "quest_id": "q1", "ai_action_parked": True, "executed": False}])
    sent = _bundle(c).settle_answered_asks("t", "NO ACTION NEEDED: teamdec_n")
    assert sorted(sent) == ["teamdec_n", "teamdec_p"]


def test_failed_settle_is_retried_on_the_next_one():
    c = Client(pending=[{"decision_id": "teamdec_a", "quest_id": "q1"}], fail=1)
    assert _bundle(c).settle_answered_asks("t", "teamdec_a") == []
    assert a.UNSETTLED == {"teamdec_a"}
    assert _bundle(c).settle_answered_asks("t2", "") == ["teamdec_a"]
    assert a.UNSETTLED == set()


def test_reconcile_clears_only_asks_a_later_result_or_note_names():
    c = Client(pending=[{"decision_id": "teamdec_a", "quest_id": "q1", "resolved_at": "2026-11-10T00:00:00Z"},
                        {"decision_id": "teamdec_b", "quest_id": "q1", "resolved_at": "2026-11-10T00:00:00Z"},
                        {"decision_id": "teamdec_c", "quest_id": "q1", "resolved_at": "2026-11-10T00:00:00Z"}],
               tasks=[{"completed_at": "2026-11-11T00:00:00Z", "result": "handled teamdec_a"},
                      {"completed_at": "2026-11-01T00:00:00Z", "result": "teamdec_c earlier"}],
               notes=[{"created_at": "2026-11-12T00:00:00Z", "text": "teamdec_b done"}])
    cleared, rest = a.reconcile_pending_asks(c)
    assert sorted(cleared) == ["teamdec_a", "teamdec_b"]
    assert [r["decision_id"] for r in rest] == ["teamdec_c"]
    assert len(c.marked) == 1
    c2 = Client(pending=c.pending, tasks=c.tasks, notes=c.notes)
    assert a.reconcile_pending_asks(c2, apply=False)[0] and c2.marked == []


class PerQuestClient(Client):
    """list_pending_answers sees nothing (other team); the per-quest route has the rows."""
    ROWS = {"q1": [{"decision_id": "teamdec_a", "status": "resolved", "ai_action_status": "pending"},
                   {"decision_id": "teamdec_done", "status": "resolved", "ai_action_status": "acted"},
                   {"decision_id": "teamdec_open", "status": "open", "ai_action_status": "pending"}],
            "q2": [{"decision_id": "teamdec_b", "quest_id": "q2", "status": "resolved", "ai_action_status": "pending"}]}

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    def list_my_quests(self):
        return [{"quest_id": "q1"}, {"quest_id": "q2"}]

    def list_decisions_for_quest(self, q):
        self.calls.append(q)
        return self.ROWS.get(q, [])


def test_pending_ids_read_the_per_quest_route_when_the_team_list_is_empty():
    c = PerQuestClient()
    assert sorted(a.pending_ids(c)) == ["teamdec_a", "teamdec_b"]
    assert a.pending_ids(c)["teamdec_a"]["quest_id"] == "q1"
    c.calls.clear()
    assert list(a.pending_ids(c, "q2")) == ["teamdec_b"] and c.calls == ["q2"]


def test_unverified_when_no_quests_come_back():
    class Empty(PerQuestClient):
        def list_my_quests(self):
            return []
    assert a.read_pending(Empty()) == ({}, False)


def test_brief_section_and_cli_and_reconcile_see_per_quest_rows(capsys, monkeypatch):
    from quest_ai_runner.intelligence.proactivity import quest_asks_brief
    from quest_ai_runner.consciousness.terminal import cli_context
    import quest_ai_runner
    c = PerQuestClient()
    assert "teamdec_a" in quest_asks_brief.answered_asks_section(c, "q1")
    c.list_autopilots = lambda q: {}
    assert cli_context.answered_asks_without_autopilot_report(c)[0][0] == "q1"
    monkeypatch.setattr(quest_ai_runner, "load_client", lambda path: c)
    assert cli_context._print_answered_asks_without_autopilot("x") == 0
    assert "q1  1 waiting" in capsys.readouterr().out
    c.notes = [{"created_at": "2026-12-01T00:00:00Z", "text": "did teamdec_a"}]
    c.ROWS = {"q1": [dict(PerQuestClient.ROWS["q1"][0], resolved_at="2026-11-01T00:00:00Z")]}
    cli_context._print_reconciled_asks("x")
    out = capsys.readouterr().out
    assert "cleared  teamdec_a" in out and c.marked == [["teamdec_a"]]


def test_cli_does_not_claim_nothing_waiting_on_an_unverified_read(capsys, monkeypatch):
    from quest_ai_runner.consciousness.terminal import cli_context
    import quest_ai_runner
    c = PerQuestClient()
    c.list_my_quests = lambda: []
    c.list_autopilots = lambda q: {}
    monkeypatch.setattr(quest_ai_runner, "load_client", lambda path: c)
    assert cli_context._print_answered_asks_without_autopilot("x") == 1
    assert "none are waiting" not in capsys.readouterr().out


def test_all_quest_read_is_bounded_and_reuse_caches(monkeypatch):
    monkeypatch.setattr(a, "MAX_QUESTS_PER_READ", 3)
    a.read_cache.clear()

    class Many(PerQuestClient):
        def list_my_quests(self):
            return [{"quest_id": f"q{i}"} for i in range(10)]
    c = Many()
    rows, verified = a.read_pending(c)
    assert len(c.calls) == 3 and verified is False
    c.calls.clear()
    a.read_pending(c, reuse=True)
    a.read_pending(c, reuse=True)
    assert c.calls == []  # reuse calls are served from the earlier read
