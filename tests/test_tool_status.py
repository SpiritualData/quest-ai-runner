"""A tool result carries a typed status, and a write that waits for approval never reads as done.

The status is data (applied | held | noop | refused | failed | unknown) taken from the tool's own result or the
HTTP exchange. The answer step's "what they did IS done" exception keys on ``applied`` alone; nothing here scans
the words of a receipt or of model output (hard rule #3).
"""
import pytest

from hubofhubs.act.execution_record import ExecutionRecord

from quest_ai_runner.core.model_registry import ModelRegistry
from quest_ai_runner.core.orchestrator import Orchestrator
from quest_ai_runner.core.orchestrator_events import grounding_answer_tail, tool_receipts_that_ran
from quest_ai_runner.core.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.core.tools_quest_ops import (
    add_quest_note_spec,
    create_decision_spec,
    create_goal_spec,
    update_outcome_progress_spec,
)
from quest_ai_runner.core.tools_quest_standard import (
    add_measurable_outcome_spec,
    add_quest_strategy_spec,
    create_quest_goal_spec,
    file_email_draft_spec,
    quest_field_tool_spec,
)
from quest_ai_runner.core.tools_status import ToolStatus, classify_payload, record_worked, write_status_of

from .conftest import StubProvider, StubRetrieval


# --- the result type ------------------------------------------------------------------------------

def test_a_result_without_a_status_is_applied_when_ok_and_failed_when_not():
    assert ToolResult(ok=True, text="x").status is ToolStatus.APPLIED
    assert ToolResult(ok=False, text="x").status is ToolStatus.FAILED


@pytest.mark.parametrize("status,ok", [
    (ToolStatus.APPLIED, True), (ToolStatus.NOOP, True), (ToolStatus.HELD, False),
    (ToolStatus.REFUSED, False), (ToolStatus.FAILED, False), (ToolStatus.UNKNOWN, False)])
def test_ok_follows_the_status_so_only_a_held_end_state_is_never_ok(status, ok):
    assert ToolResult(ok=True, text="x", status=status).ok is ok
    assert ToolResult(ok=False, text="x", status=status).ok is ok


def test_a_status_may_be_given_as_its_string():
    assert ToolResult(ok=True, status="held").status is ToolStatus.HELD


def test_classify_payload_reads_the_server_flags_not_words():
    assert classify_payload({"applied": False, "reason": "needs approval"}) is ToolStatus.HELD
    assert classify_payload({"pending_approval": True, "decision_id": "d1"}) is ToolStatus.HELD
    assert classify_payload({"pending": True}) is ToolStatus.HELD
    assert classify_payload({"id": "g1"}, http_status=202) is ToolStatus.HELD
    assert classify_payload({"already_existed": True, "id": "g1"}) is ToolStatus.NOOP
    assert classify_payload({"duplicate": True}) is ToolStatus.NOOP
    assert classify_payload({"id": "g1", "message": "Not applied, held, waiting"}) is ToolStatus.APPLIED
    assert classify_payload([{"id": "n1"}]) is ToolStatus.APPLIED
    assert write_status_of({"applied": False}) is ToolStatus.HELD


def test_a_tool_that_raises_a_typed_write_error_keeps_its_status():
    class WriteError(RuntimeError):
        write_status = ToolStatus.UNKNOWN

    def boom(args, ctx):
        raise WriteError("outcome unknown: the request was sent")

    registry = ToolRegistry([ToolSpec(name="w", description="d", handler=boom, mutates=True)])
    result = registry.invoke("w", {}, ToolContext())
    assert result.status is ToolStatus.UNKNOWN and not result.ok


def test_an_untyped_exception_is_failed():
    def boom(args, ctx):
        raise RuntimeError("nope")

    registry = ToolRegistry([ToolSpec(name="w", description="d", handler=boom, mutates=True)])
    assert registry.invoke("w", {}, ToolContext()).status is ToolStatus.FAILED


def test_a_mutating_tool_that_times_out_is_unknown_and_a_read_is_failed():
    import time

    def slow(args, ctx):
        time.sleep(0.5)
        return "late"

    write = ToolSpec(name="w", description="d", handler=slow, mutates=True, timeout_seconds=0.05)
    read = ToolSpec(name="r", description="d", handler=slow, mutates=False, timeout_seconds=0.05)
    registry = ToolRegistry([write, read])
    assert registry.invoke("w", {}, ToolContext()).status is ToolStatus.UNKNOWN
    assert registry.invoke("r", {}, ToolContext()).status is ToolStatus.FAILED


# --- the receipt and the answer step --------------------------------------------------------------

class Silent:
    def status(self, *a, **k): pass
    def emit(self, *a, **k): pass


def run_one(result, mutates=True, signatures=None):
    spec = ToolSpec(name="do_it", description="d", mutates=mutates, handler=lambda args, ctx: result)
    orch = Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]),
                        registry=ModelRegistry(StubProvider([])), tools=ToolRegistry([spec]))
    gathered, record = [], ExecutionRecord()
    sigs = {} if signatures is None else signatures
    orch.run_tool_calls([{"name": "do_it", "args": {"a": 1}}], gathered=gathered, exec_record=record,
                        tool_ok_signatures=sigs, emit=Silent(), steps=1, ctx=ToolContext())
    return gathered, record, sigs


@pytest.mark.parametrize("status,word", [
    (ToolStatus.APPLIED, "SUCCEEDED"), (ToolStatus.NOOP, "NO CHANGE NEEDED"),
    (ToolStatus.HELD, "HELD FOR APPROVAL"), (ToolStatus.REFUSED, "REFUSED"),
    (ToolStatus.FAILED, "FAILED"), (ToolStatus.UNKNOWN, "OUTCOME UNKNOWN")])
def test_the_receipt_names_the_status_and_carries_it_as_data(status, word):
    gathered, record, _ = run_one(ToolResult(ok=True, text="x", status=status))
    assert gathered[0]["text"].startswith(f"TOOL do_it {word} (")
    assert gathered[0]["tool_status"] == status.value
    assert record.facts[0].succeeded is (status in (ToolStatus.APPLIED, ToolStatus.NOOP))


def test_a_held_write_is_neither_a_success_nor_a_failure_and_counts_as_work_done():
    """The request was made and a person's yes is outstanding: not succeeded, not failed (a failure stamps the run
    "nothing done" and tells a deep run its command broke), phase held, and the run is still recorded as having worked."""
    _, record, _ = run_one(ToolResult(ok=True, text="waiting", status=ToolStatus.HELD))
    fact = record.facts[0]
    assert fact.succeeded is False and fact.failed is False and fact.phases[-1] == "held" and not fact.error
    assert not record.any_success and not record.any_failure and record_worked(record)


def test_a_failed_or_refused_write_is_not_work_done():
    for status in (ToolStatus.FAILED, ToolStatus.REFUSED, ToolStatus.UNKNOWN):
        _, record, _ = run_one(ToolResult(ok=False, text="no", status=status))
        assert record.facts[0].failed is True and not record_worked(record)


def test_a_mutating_call_with_an_unknown_outcome_is_never_repeated_in_the_turn():
    """It was sent and not answered, so it MAY have happened: a re-plan that proposes the identical call does not send it again."""
    runs = []
    spec = ToolSpec(name="do_it", description="d", mutates=True,
                    handler=lambda args, ctx: runs.append(1) or ToolResult(ok=False, text="timed out", status=ToolStatus.UNKNOWN))
    orch = Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]),
                        registry=ModelRegistry(StubProvider([])), tools=ToolRegistry([spec]))
    gathered, sigs = [], {}
    for _ in range(3):
        orch.run_tool_calls([{"name": "do_it", "args": {"a": 1}}], gathered=gathered, exec_record=ExecutionRecord(),
                            tool_ok_signatures=sigs, emit=Silent(), steps=1, ctx=ToolContext())
    assert len(runs) == 1
    assert [g["tool_status"] for g in gathered] == ["unknown"] * 3 and "OUTCOME UNKNOWN" in gathered[2]["text"]


def test_a_read_with_a_failed_or_unknown_result_may_be_run_again():
    runs = []
    spec = ToolSpec(name="look", description="d", mutates=False,
                    handler=lambda args, ctx: runs.append(1) or ToolResult(ok=False, text="slow", status=ToolStatus.UNKNOWN))
    orch = Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]),
                        registry=ModelRegistry(StubProvider([])), tools=ToolRegistry([spec]))
    sigs = {}
    for _ in range(2):
        orch.run_tool_calls([{"name": "look", "args": {}}], gathered=[], exec_record=ExecutionRecord(),
                            tool_ok_signatures=sigs, emit=Silent(), steps=1, ctx=ToolContext())
    assert len(runs) == 2


def test_an_identical_call_that_was_held_or_unchanged_is_not_run_again():
    runs = []
    spec = ToolSpec(name="do_it", description="d", mutates=True,
                    handler=lambda args, ctx: runs.append(1) or ToolResult(
                        ok=False, text="waiting", status=ToolStatus.HELD))
    orch = Orchestrator(retrieval=StubRetrieval(), provider=StubProvider([]),
                        registry=ModelRegistry(StubProvider([])), tools=ToolRegistry([spec]))
    gathered, sigs = [], {}
    for _ in range(2):
        orch.run_tool_calls([{"name": "do_it", "args": {"a": 1}}], gathered=gathered,
                            exec_record=ExecutionRecord(), tool_ok_signatures=sigs, emit=Silent(),
                            steps=1, ctx=ToolContext())
    assert len(runs) == 1
    assert gathered[1]["tool_status"] == "held" and "HELD" in gathered[1]["text"]


def obs(name, status, text=None):
    return {"kind": "query", "locator": f"tool:{name}", "tool_status": status,
            "text": text or f"TOOL {name} SUCCEEDED (args {{}}):\nok"}


def test_only_an_applied_receipt_counts_as_done():
    gathered = [obs("a", "applied"), obs("b", "held"), obs("c", "noop"), obs("d", "refused"),
                obs("e", "failed"), obs("f", "unknown")]
    assert tool_receipts_that_ran(gathered) == ["a"]


def test_the_status_is_data_so_a_receipt_with_only_matching_words_is_not_done():
    words_only = {"kind": "query", "locator": "tool:a", "text": "TOOL a SUCCEEDED (args {}):\nok"}
    assert tool_receipts_that_ran([words_only]) == []
    held_with_done_words = obs("a", "held", "TOOL a SUCCEEDED (args {}):\nit says succeeded")
    assert tool_receipts_that_ran([held_with_done_words]) == []


def test_the_answer_step_calls_a_held_write_waiting_not_done():
    tail = grounding_answer_tail([obs("create_quest_goal", "held",
                                      "TOOL create_quest_goal HELD FOR APPROVAL (args {}):\nwaiting")],
                                 partial=False)
    assert "EXCEPTION to the rule above" not in tail
    assert "HELD FOR APPROVAL (create_quest_goal)" in tail
    assert "waiting for a person to approve" in tail and "never say it is done" in tail


def test_the_answer_step_explains_each_other_status_once_it_is_present():
    gathered = [obs("a", "applied"), obs("b", "held"), obs("c", "noop"), obs("d", "refused"),
                obs("e", "failed"), obs("f", "unknown")]
    tail = grounding_answer_tail(gathered, partial=False)
    assert "SUCCEEDED (a)" in tail and "EXCEPTION to the rule above" in tail
    for fragment in ("HELD FOR APPROVAL (b)", "NO CHANGE NEEDED (c)", "already existed",
                     "REFUSED (d)", "say why", "FAILED (e)", "did not happen",
                     "OUTCOME UNKNOWN (f)", "may or may not"):
        assert fragment in tail, fragment


def test_no_receipt_no_status_text():
    tail = grounding_answer_tail([], partial=False)
    assert "HELD FOR APPROVAL" not in tail and "OUTCOME UNKNOWN" not in tail


# --- the standard tools: held, noop, refused and unknown are never "done" ------------------------

class Client:
    """A fake Quest client whose write returns whatever the test says (a server payload) or raises."""

    def __init__(self, write=None, error=None, quest=None):
        self.write, self.error, self.quest = write, error, quest or {}

    def respond(self):
        if self.error:
            raise self.error
        return self.write

    def edit_quest_field(self, *a, **k): return self.respond()
    def add_quest_strategy(self, *a, **k): return self.respond()
    def create_goal(self, *a, **k): return self.respond()
    def create_decision(self, *a, **k): return self.respond()
    def add_quest_note(self, *a, **k): return self.respond()
    def update_outcome_progress(self, *a, **k): return self.respond()
    def create_email_draft(self, *a, **k): return self.respond()
    def get_my_quest(self, quest_id): return {"state": self.quest}
    def list_open_decisions_for_quest(self, quest_id): return []


class WriteError(RuntimeError):
    def __init__(self, message, status):
        super().__init__(message)
        self.write_status = status


def with_client(client):
    return lambda: client


def run_field(client):
    spec = quest_field_tool_spec("outcome", {}, client_factory=with_client(client))
    return spec.handler({"quest_id": "q1", "value": "new", "user_asked_for_this_field": True}, ToolContext())


def test_a_field_write_held_for_approval_is_held_not_ok():
    result = run_field(Client({"applied": False, "reason": "autopilot_off", "decision_id": "d1"}))
    assert result.status is ToolStatus.HELD and not result.ok
    assert "NOT applied" in result.text and "autopilot_off" in result.text


def test_a_field_write_that_applied_is_applied():
    assert run_field(Client({"quest_id": "q1"})).status is ToolStatus.APPLIED


def test_a_field_write_refused_or_unknown_keeps_that_status():
    refused = run_field(Client(error=WriteError("refused: not yours", ToolStatus.REFUSED)))
    unknown = run_field(Client(error=WriteError("outcome unknown", ToolStatus.UNKNOWN)))
    assert refused.status is ToolStatus.REFUSED and "not yours" in refused.text
    assert unknown.status is ToolStatus.UNKNOWN and "unknown" in unknown.text


def test_a_legacy_empty_write_is_failed():
    assert run_field(Client({})).status is ToolStatus.FAILED


def test_a_measurable_outcome_that_already_exists_is_a_noop_and_a_held_one_is_held():
    quest = {"measurable_outcomes": [{"text": "Publish 40 videos"}]}
    noop = add_measurable_outcome_spec({}, client_factory=with_client(Client(quest=quest))).handler(
        {"quest_id": "q1", "text": "publish 40 videos"}, ToolContext())
    assert noop.status is ToolStatus.NOOP and noop.ok
    held = add_measurable_outcome_spec({}, client_factory=with_client(
        Client({"applied": False, "reason": "x"}))).handler(
        {"quest_id": "q1", "text": "Run daily"}, ToolContext())
    assert held.status is ToolStatus.HELD


def test_a_strategy_held_for_approval_is_held():
    result = add_quest_strategy_spec({}, client_factory=with_client(
        Client({"applied": False, "reason": "not_user_requested"}))).handler(
        {"quest_id": "q1", "title": "Progressive long run"}, ToolContext())
    assert result.status is ToolStatus.HELD


def test_a_goal_that_already_existed_is_a_noop_and_a_refused_one_says_so():
    args = {"quest_id": "q1", "title": "Run", "period": "2026_W41", "user_asked_for_this_field": True}
    noop = create_quest_goal_spec({}, client_factory=with_client(
        Client({"id": "g1", "already_existed": True}))).handler(dict(args), ToolContext())
    assert noop.status is ToolStatus.NOOP
    refused = create_quest_goal_spec({}, client_factory=with_client(
        Client(error=WriteError("Quest refused: you cannot edit this quest", ToolStatus.REFUSED)))).handler(
        dict(args), ToolContext())
    assert refused.status is ToolStatus.REFUSED and "cannot edit" in refused.text
    unknown = create_quest_goal_spec({}, client_factory=with_client(
        Client(error=WriteError("outcome unknown: may or may not", ToolStatus.UNKNOWN)))).handler(
        dict(args), ToolContext())
    assert unknown.status is ToolStatus.UNKNOWN


def test_an_email_draft_waits_for_approval_so_it_is_held():
    result = file_email_draft_spec({}, client_factory=with_client(Client({"id": "dr1"}))).handler(
        {"to": "a@example.org", "subject": "s", "body": "b"}, ToolContext())
    assert result.status is ToolStatus.HELD and "NOT sent" in result.text


def test_progress_waiting_for_approval_is_held():
    spec = update_outcome_progress_spec({}, client_factory=with_client(
        Client({"pending_approval": True, "decision_id": "d9"})))
    result = spec.handler({"quest_id": "q1", "outcome_id": "m1", "progress_pct": 40, "note": "n"}, ToolContext())
    assert result.status is ToolStatus.HELD and not result.ok


def test_a_decision_that_is_already_open_is_a_noop_and_one_already_answered_is_refused():
    spec = create_decision_spec({}, client_factory=with_client(
        Client({"duplicate": True, "decision_id": "d1"})))
    noop = spec.handler({"summary": "Approve?", "quest_id": "q1"}, ToolContext())
    assert noop.status is ToolStatus.NOOP
    refused = create_decision_spec({}, client_factory=with_client(
        Client(error=WriteError("Quest refused: the person already declined this", ToolStatus.REFUSED)))).handler(
        {"summary": "Approve?", "quest_id": "q1"}, ToolContext())
    assert refused.status is ToolStatus.REFUSED and "declined" in refused.text


def test_a_note_that_failed_is_failed_and_one_that_landed_is_applied():
    spec = add_quest_note_spec({}, client_factory=with_client(Client([{"id": "n1"}])))
    assert spec.handler({"quest_id": "q1", "text": "t"}, ToolContext()).status is ToolStatus.APPLIED
    failing = add_quest_note_spec({}, client_factory=with_client(
        Client(error=WriteError("did not happen: Quest is unreachable", ToolStatus.FAILED))))
    assert failing.handler({"quest_id": "q1", "text": "t"}, ToolContext()).status is ToolStatus.FAILED


def test_create_goal_tool_in_ops_maps_noop_and_unknown():
    noop = create_goal_spec({}, client_factory=with_client(Client({"id": "g1", "already_existed": True}))).handler(
        {"quest_id": "q1", "title": "Run", "period": "2026_W41"}, ToolContext())
    assert noop.status is ToolStatus.NOOP
