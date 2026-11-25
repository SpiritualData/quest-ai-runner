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
