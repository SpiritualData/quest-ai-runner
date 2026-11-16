"""Writes the quest creation flow needs from the client: a refused specification write is unmistakable, the strategy-map routes and the habit
update, resume and disable routes have methods, and a habit is created with no strategy. Scripted transport, no network."""
import pytest

from quest_ai_runner.body.internal_context.quest.client.quest_client_support import QuestWriteError, WriteResult
from quest_ai_runner.consciousness.terminal.cli_quest import _quest_method_is_read_only
from quest_ai_runner.world.tools.tools_status import ToolStatus

from .fake_quest_transport import make_client

REFUSAL = {"detail": {"code": "FORBIDDEN", "message": "The AI does not archive a record a person wrote"}}
SPEC_WRITES = {
    "create_spec_record": lambda c: c.create_spec_record("quest", "q1", "solution", {"name": "Practice"}),
    "edit_spec_record": lambda c: c.edit_spec_record("esr_1", {"purpose": "x"}, 1),
    "archive_spec_record": lambda c: c.archive_spec_record("esr_1"),
    "reorder_solutions": lambda c: c.reorder_solutions("quest", "q1", ["esr_1"]),
    "refresh_spec": lambda c: c.refresh_spec("quest", "q1"),
    "place_quest": lambda c: c.place_quest("q1", "int_a"),
}


@pytest.mark.parametrize("name", sorted(SPEC_WRITES))
def test_a_refused_spec_write_is_a_falsy_typed_result_that_still_reads_like_the_refusal(monkeypatch, name):
    client, _ = make_client([(403, REFUSAL)], monkeypatch)
    result = SPEC_WRITES[name](client)
    assert isinstance(result, WriteResult) and not result and result.status is ToolStatus.REFUSED
    assert "person wrote" in result.detail and result.get("refused") is True and result["code"] == "FORBIDDEN" and result["status"] == 403
    assert not result.get("recordId")


def test_spec_write_statuses_applied_held_failed_and_unknown(monkeypatch):
    applied, _ = make_client([(201, {"recordId": "esr_9", "revision": 1})], monkeypatch)
    made = applied.create_spec_record("quest", "q1", "solution", {"name": "Practice"})
    assert made and made.status is ToolStatus.APPLIED and made.id == "esr_9" and made["recordId"] == "esr_9"

    held, _ = make_client([(202, {"pending": True, "decisionId": "dec_1"})], monkeypatch)
    waiting = held.update_intent("int_a", 1, name="New name")
    assert waiting.status is ToolStatus.HELD and waiting.id == "dec_1" and waiting.get("pending") is True

    failed, _ = make_client([(503, {"detail": "unavailable"})], monkeypatch)
    assert failed.edit_spec_record("esr_1", {"purpose": "x"}, 1).status is ToolStatus.FAILED
    unknown, _ = make_client([TimeoutError("The read operation timed out")], monkeypatch)
    lost = unknown.edit_spec_record("esr_1", {"purpose": "x"}, 1)
    assert lost.status is ToolStatus.UNKNOWN and not lost and lost["outcomeUnknown"] is True


def test_the_strategy_map_methods_call_their_routes(monkeypatch):
    client, transport = make_client([(200, {"solutions": []})], monkeypatch)
    assert client.get_strategy_map("q1") == {"solutions": []}
    client.set_solution_answer("q1", "s1", "Morning sessions", "decided")
    client.set_solution_picks("q1", "s1", ["Zener cards"])
    client.set_solution_reviewed("q1", "s1")
    client.create_map_strategy("q1", "s1", "Daily session", ideal_ids=["i1"])
    client.update_map_strategy("q1", "st1", name="Morning session")
    client.delete_map_strategy("q1", "st1")
    base = "/api/quests/q1/strategy-map"
    assert [(r[0], r[1], r[3]) for r in transport.requests] == [
        ("GET", base, None),
        ("PUT", f"{base}/solutions/s1/answer", {"text": "Morning sessions", "certainty": "decided", "draftedBy": "person"}),
        ("PUT", f"{base}/solutions/s1/picks", {"names": ["Zener cards"]}),
        ("PUT", f"{base}/solutions/s1/reviewed", {"reviewed": True}),
        ("POST", f"{base}/strategies", {"solutionId": "s1", "name": "Daily session", "description": "", "idealIds": ["i1"], "usesSolutionIds": []}),
        ("PATCH", f"{base}/strategies/st1", {"name": "Morning session"}),
        ("DELETE", f"{base}/strategies/st1", None),
    ]
    assert _quest_method_is_read_only("get_strategy_map")
    for name in ("set_solution_answer", "set_solution_picks", "set_solution_reviewed", "create_map_strategy", "update_map_strategy", "delete_map_strategy",
                 "update_habit", "resume_habit", "disable_habit"):
        assert not _quest_method_is_read_only(name) and len((getattr(type(client), name).__doc__ or "").strip().splitlines()) == 1, name


def test_habit_update_resume_disable_and_a_habit_with_no_strategy(monkeypatch):
    client, transport = make_client([(200, {"habit": {"id": "coll_1"}})], monkeypatch)
    client.update_habit("coll_1", time="05:15")
    client.resume_habit("coll_1")
    client.disable_habit("coll_1")
    client.create_habit("q1", None, "Evening review", start_date="2026-11-15")
    client.create_habit("q1", "st1", "Morning run", start_date="2026-11-15")
    sent = [(r[0], r[1], r[3]) for r in transport.requests]
    assert sent[:3] == [("PUT", "/api/planning/habits/coll_1", {"time": "05:15"}), ("POST", "/api/planning/habits/coll_1/resume", None),
                        ("PUT", "/api/planning/habits/coll_1", {"enabled": False})]
    assert "strategy_id" not in sent[3][2] and sent[4][2]["strategy_id"] == "st1"

    refusing, _ = make_client([(403, {"detail": "Not your habit"})], monkeypatch)
    with pytest.raises(QuestWriteError) as caught:
        refusing.disable_habit("coll_1")
    assert caught.value.write_status is ToolStatus.REFUSED
