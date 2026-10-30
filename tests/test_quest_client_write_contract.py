"""One contract for every content write: return a typed ``WriteResult`` (status, id, detail) or raise a typed
``QuestWriteError``; never an ambiguous ``{}`` or ``[]``. A write whose request was sent but never answered is
``unknown``, not ``failed`` and not ``applied``. Reads keep their lenient return (absence is normal for them)."""
import urllib.error

import pytest

from quest_ai_runner.core.tools_status import ToolStatus
from quest_ai_runner.runner.quest_client import QuestApiError
from quest_ai_runner.runner.quest_client_support import QuestWriteError, WriteResult

from .fake_quest_transport import make_client


def raising(client, call):
    with pytest.raises(QuestWriteError) as caught:
        call(client)
    return caught.value


# every content write that used to return {} or [] on failure
CONTENT_WRITES = {
    "create_decision": lambda c: c.create_decision("Approve?", quest_id="q1", assignee_user_id="u1"),
    "resolve_decision": lambda c: c.resolve_decision("d1", "approved"),
    "record_ask": lambda c: c.record_ask("q1", "do the thing"),
    "update_ask": lambda c: c.update_ask("k1", "done", note="n"),
    "update_goal": lambda c: c.update_goal("g1", {"description": "d"}),
    "complete_goal": lambda c: c.complete_goal("g1"),
    "add_goal_update": lambda c: c.add_goal_update("g1", "note"),
    "edit_quest_field": lambda c: c.edit_quest_field("q1", {"outcome": "x"}),
    "add_quest_strategy": lambda c: c.add_quest_strategy("q1", "Title"),
    "add_quest_note": lambda c: c.add_quest_note("q1", "text"),
    "create_context_entry": lambda c: c.create_context_entry("q1", "name", "content"),
    "propose_metric_value": lambda c: c.propose_metric_value("c1", {"a": 1}, [{"s": "x"}]),
}


@pytest.mark.parametrize("name", sorted(CONTENT_WRITES))
def test_a_refused_write_raises_a_typed_error_with_the_reason(monkeypatch, name):
    client, _ = make_client([(403, {"detail": "You cannot edit this quest"})], monkeypatch)
    error = raising(client, CONTENT_WRITES[name])
    assert error.write_status is ToolStatus.REFUSED and error.status == 403
    assert "cannot edit this quest" in str(error)
    assert isinstance(error, QuestApiError)  # existing `except QuestApiError` callers still catch it


@pytest.mark.parametrize("name", sorted(CONTENT_WRITES))
def test_a_server_error_is_failed_and_a_timeout_after_sending_is_unknown(monkeypatch, name):
    failed, _ = make_client([(500, {"detail": "boom"})], monkeypatch)
    assert raising(failed, CONTENT_WRITES[name]).write_status is ToolStatus.FAILED
    unknown, _ = make_client([TimeoutError("The read operation timed out")], monkeypatch)
    error = raising(unknown, CONTENT_WRITES[name])
    assert error.write_status is ToolStatus.UNKNOWN and error.outcome_unknown is True
    assert "may or may not" in str(error)


def test_a_connection_that_never_opened_is_failed_because_nothing_was_sent(monkeypatch):
    client, _ = make_client([urllib.error.URLError(ConnectionRefusedError("refused"))], monkeypatch)
    error = raising(client, CONTENT_WRITES["update_goal"])
    assert error.write_status is ToolStatus.FAILED and error.outcome_unknown is False


def test_a_reset_after_the_request_was_sent_is_unknown(monkeypatch):
    client, _ = make_client([ConnectionResetError("reset by peer")], monkeypatch)
    assert raising(client, CONTENT_WRITES["add_quest_note"]).write_status is ToolStatus.UNKNOWN


def test_a_429_is_failed_not_refused_and_carries_retry_after(monkeypatch):
    client, _ = make_client([(429, {"detail": "slow down"}, {"Retry-After": "3600"})], monkeypatch)
    error = raising(client, CONTENT_WRITES["add_quest_note"])
    assert error.write_status is ToolStatus.FAILED and error.retry_after == 3600.0


def test_a_note_that_landed_is_applied_whatever_shape_the_body_has(monkeypatch):
    client, _ = make_client([(201, [{"id": "n1", "text": "t"}])], monkeypatch)
    result = client.add_quest_note("q1", "t")
    assert isinstance(result, WriteResult) and result.status is ToolStatus.APPLIED
    assert result.data == [{"id": "n1", "text": "t"}] and result
    odd, _ = make_client([(201, {"ok": True})], monkeypatch)
    assert odd.add_quest_note("q1", "t").status is ToolStatus.APPLIED  # was: [] ("not saved")


def test_a_202_is_held_with_the_decision_as_its_id(monkeypatch):
    client, _ = make_client([(202, {"decision_id": "d7", "message": "waiting"})], monkeypatch)
    result = client.edit_quest_field("q1", {"outcome": "x"})
    assert result.status is ToolStatus.HELD and result.id == "d7" and result.detail == "waiting"


def test_an_applied_false_body_is_held_even_on_a_200(monkeypatch):
    client, _ = make_client([(200, {"applied": False, "reason": "autopilot_off", "decision_id": "d2"})],
                            monkeypatch)
    result = client.edit_quest_field("q1", {"outcome": "x"})
    assert result.status is ToolStatus.HELD and result.get("applied") is False and result.id == "d2"
    strategy, _ = make_client([(202, {"applied": False, "reason": "gated"})], monkeypatch)
    assert strategy.add_quest_strategy("q1", "T").status is ToolStatus.HELD


def test_a_goal_the_server_says_already_existed_is_a_noop(monkeypatch):
    client, _ = make_client([(200, {"id": "g1", "already_existed": True})], monkeypatch)
    result = client.create_goal("Run", period="2026-08-18", quest_id="q1")
    assert result.status is ToolStatus.NOOP and result.id == "g1"


def test_a_decision_the_person_already_answered_is_a_refusal_with_the_servers_words(monkeypatch):
    client, _ = make_client([(409, {"detail": "They declined this on Monday"})], monkeypatch)
    error = raising(client, CONTENT_WRITES["create_decision"])
    assert error.write_status is ToolStatus.REFUSED and "declined this on Monday" in str(error)


def test_a_created_decision_is_applied_with_its_id(monkeypatch):
    client, _ = make_client([(200, {"decision_id": "d1"})], monkeypatch)
    result = client.create_decision("Approve?", quest_id="q1", assignee_user_id="u1")
    assert result.status is ToolStatus.APPLIED and result.id == "d1" and result.get("decision_id") == "d1"


def test_an_unconfigured_client_raises_a_typed_failure(monkeypatch):
    from quest_ai_runner.runner.quest_client import QuestClient, QuestNotConfigured
    with pytest.raises(QuestNotConfigured) as caught:
        QuestClient("", "").add_quest_note("q1", "t")
    assert caught.value.write_status is ToolStatus.FAILED


def test_a_write_result_is_never_an_empty_value():
    result = WriteResult(None, ToolStatus.APPLIED)
    assert result and result.data is None and result.get("x") is None
    assert WriteResult({"a": 1}, ToolStatus.APPLIED) == {"a": 1}


# --- the lifecycle reports keep their "never raises" rule, but the answer is typed, not {} -----------

def test_a_terminal_report_that_fails_returns_a_falsy_typed_result_and_never_raises(monkeypatch):
    client, _ = make_client([(500, {"detail": "boom"})], monkeypatch)
    result = client.report_done("t1", "all done")
    assert isinstance(result, WriteResult) and not result and result.status is ToolStatus.FAILED
    unknown, _ = make_client([TimeoutError("slow")], monkeypatch)
    assert unknown.report_failed("t1", "x").status is ToolStatus.UNKNOWN
    ok, _ = make_client([(200, {"id": "t1", "status": "done"})], monkeypatch)
    assert ok.report_done("t1", "all done").status is ToolStatus.APPLIED


# --- reads stay lenient, on purpose ----------------------------------------------------------------

def test_reads_where_absence_is_normal_keep_returning_an_empty_value(monkeypatch):
    client, _ = make_client([(500, {"detail": "boom"})], monkeypatch)
    assert client.list_quests() == [] and client.get_my_quest("q1") == {}
    assert list(client.list_quest_notes("q1")) == []
