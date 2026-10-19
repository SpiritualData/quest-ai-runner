"""Life-intent tools: the five operations, the client calls under them, and the no-churn rules.

The stub backend models what the real one does (the user-edit lock, the ask for a user-placed move,
the digest gate, revisions), not just its reply shape, so a tool that printed a refusal as success
would fail here. No network and no model.
"""
import json

import pytest

from quest_ai_runner.core import tools_quest_intents as intents
from quest_ai_runner.core.tools import ToolContext, ToolRegistry
from quest_ai_runner.runner.quest_client import QuestClient
from quest_ai_runner.runner.quest_client_intents import refusal_of
from quest_ai_runner.runner.quest_client_support import QuestApiError

CREDS = {"QUEST_BASE_URL": "https://quest.example", "QUEST_API_KEY": "key"}


class StubIntentsBackend:
    """Same method names as the real client; answers the way the server does."""

    def __init__(self):
        self.intents = [{"intentId": "int_a", "name": "Health", "visionStatement": "I am strong.",
                         "questCount": 1, "quests": [{"questId": "q_placed"}]}]
        self.unsorted = ["q_new"]
        self.placed_by_user = {"q_placed"}
        self.records = {"esr_1": {"recordId": "esr_1", "type": "solution", "revision": 3,
                                  "fields": {"name": "Run weekly", "goals": "Run 3 times"},
                                  "fieldMeta": {"goals": {"by": "user", "editedByUser": True}}}}
        self.digest = "d1"
        self.last_digest_seen = None
        self.writes = 0
        self.model_calls = 0
        self.down = False

    def list_intents(self, **kw):
        self.last_owner = kw
        return None if self.down else {"intents": self.intents, "unsorted": self.unsorted}

    def get_spec(self, subject_type, subject_id):
        return None if self.down else {"records": list(self.records.values()), "digest": self.digest}

    def refresh_spec(self, subject_type, subject_id, *, reason="manual", force=False):
        if self.last_digest_seen == self.digest and not force:
            return {"changed": False, "skipped": "digest_unchanged", "applied": [], "asks": [], "digest": self.digest}
        self.last_digest_seen = self.digest
        self.model_calls += 1
        self.writes += 1
        return {"changed": True, "applied": [{"recordId": "esr_2", "action": "created", "fields": ["name"]}],
                "asks": [{"decisionId": "dec_9", "kind": "intent_rename", "subjectId": "int_a"}],
                "digest": self.digest}

    def edit_spec_record(self, record_id, fields, expected_revision, *, status=None):
        row = self.records[record_id]
        locked = [k for k in fields if (row["fieldMeta"].get(k) or {}).get("editedByUser")]
        if locked:
            return {"refused": True, "status": 409, "code": "FIELD_LOCKED", "message": "locked", "lockedFields": locked}
        if expected_revision != row["revision"]:
            return {"refused": True, "status": 409, "code": "REVISION_CONFLICT", "message": "stale", "lockedFields": []}
        row["fields"].update(fields)
        row["revision"] += 1
        self.writes += 1
        return dict(row)

    def place_quest(self, quest_id, intent_id, **kw):
        if quest_id in self.placed_by_user:
            return {"applied": [], "errors": [],
                    "pending": [{"questId": quest_id, "decisionId": "dec_7"}]}
        if intent_id == "int_missing":
            return {"applied": [], "errors": [{"questId": quest_id, "code": "NOT_FOUND", "message": "no such intent"}],
                    "pending": []}
        self.writes += 1
        return {"applied": [{"questId": quest_id, "intentId": intent_id}], "errors": [], "pending": []}


def invoke(builder, backend, args, quest_id="q_new"):
    spec = builder(CREDS, client_factory=lambda: backend)
    return ToolRegistry([spec]).invoke(spec.name, args, ToolContext(quest_id=quest_id))


# --- reads ---------------------------------------------------------------------------------------

def test_list_intents_shows_ids_and_counts():
    result = invoke(intents.list_intents_spec, StubIntentsBackend(), {})
    assert result.ok and "int_a" in result.text and "1 unsorted" in result.text


def test_a_failed_read_is_not_reported_as_no_intents():
    backend = StubIntentsBackend()
    backend.down = True
    for builder, args in ((intents.list_intents_spec, {}), (intents.get_spec_spec, {})):
        result = invoke(builder, backend, args)
        assert not result.ok and "Do not assume" in result.text


def test_get_spec_marks_fields_the_person_edited_and_defaults_to_this_quest():
    result = invoke(intents.get_spec_spec, StubIntentsBackend(), {})
    assert result.ok and "esr_1" in result.text and "person edited: goals" in result.text


# --- refresh: the digest gate and the churn rule -------------------------------------------------

def test_refresh_reports_writes_and_the_ask_it_raised():
    result = invoke(intents.refresh_spec_spec, StubIntentsBackend(), {"reason": "autopilot"})
    assert result.ok and "1 records written, 1 asks raised" in result.text and "dec_9" in result.text


def test_unchanged_inputs_write_nothing_and_call_no_model_on_the_second_refresh():
    backend = StubIntentsBackend()
    invoke(intents.refresh_spec_spec, backend, {})
    writes, calls = backend.writes, backend.model_calls
    again = invoke(intents.refresh_spec_spec, backend, {})
    assert again.ok and "Nothing changed" in again.text
    assert (backend.writes, backend.model_calls) == (writes, calls)


def test_five_turns_over_an_unchanged_spec_write_once():
    backend = StubIntentsBackend()
    for _ in range(5):
        invoke(intents.refresh_spec_spec, backend, {"reason": "chat"})
    assert backend.writes == 1


def test_refresh_rejects_an_unknown_reason_without_calling_the_backend():
    backend = StubIntentsBackend()
    assert not invoke(intents.refresh_spec_spec, backend, {"reason": "whenever"}).ok
    assert backend.writes == 0


# --- edits: the user-edit lock and revisions -----------------------------------------------------

def test_a_field_the_person_edited_is_refused_not_reported_as_changed():
    backend = StubIntentsBackend()
    result = invoke(intents.edit_spec_record_spec, backend,
                    {"record_id": "esr_1", "fields": {"goals": "Run daily"}, "expected_revision": 3})
    assert not result.ok and "person edited goals" in result.text and "may not write it" in result.text
    assert backend.records["esr_1"]["fields"]["goals"] == "Run 3 times" and backend.writes == 0


def test_an_edit_reads_the_revision_when_the_subject_is_named():
    backend = StubIntentsBackend()
    result = invoke(intents.edit_spec_record_spec, backend,
                    {"record_id": "esr_1", "fields": {"name": "Run twice weekly"}})
    assert result.ok and backend.records["esr_1"]["revision"] == 4


def test_a_stale_revision_asks_for_a_re_read():
    result = invoke(intents.edit_spec_record_spec, StubIntentsBackend(),
                    {"record_id": "esr_1", "fields": {"name": "x"}, "expected_revision": 1})
    assert not result.ok and "get_spec" in result.text


# --- placement: the ask path ---------------------------------------------------------------------

def test_placing_an_unsorted_quest_lands():
    backend = StubIntentsBackend()
    result = invoke(intents.place_quest_spec, backend, {"intent_id": "int_a"})
    assert result.ok and "placed under intent int_a" in result.text and backend.writes == 1


def test_moving_a_quest_the_person_placed_is_held_as_an_ask_not_done():
    backend = StubIntentsBackend()
    result = invoke(intents.place_quest_spec, backend, {"quest_id": "q_placed", "intent_id": "int_a"})
    assert result.ok and "NOT done" in result.text and "dec_7" in result.text and backend.writes == 0


def test_a_per_move_error_is_a_failure():
    result = invoke(intents.place_quest_spec, StubIntentsBackend(), {"intent_id": "int_missing"})
    assert not result.ok and "NOT_FOUND" in result.text


def test_a_202_ask_from_the_server_is_surfaced():
    class Asking(StubIntentsBackend):
        def refresh_spec(self, *a, **kw):
            return {"pending": True, "decisionId": "dec_3"}

    result = invoke(intents.refresh_spec_spec, Asking(), {})
    assert result.ok and "NOT done" in result.text and "dec_3" in result.text


# --- the client: paths, wire shape and refusals --------------------------------------------------

class Recorder(QuestClient):
    def __init__(self, reply=None, error=None):
        super().__init__("https://quest.example", "key")
        self.sent = []
        self.reply, self.error = reply, error

    def _request(self, method, path, *, params=None, body=None, timeout_override=None):
        self.sent.append((method, path, params, body))
        if self.error:
            raise self.error
        return self.reply


def test_client_calls_the_contract_paths_in_camel_case_and_never_sends_an_actor():
    client = Recorder(reply={})
    client.list_intents(owner_type="team", owner_id="t1")
    client.get_spec("quest", "q1")
    client.refresh_spec("intent", "int_a", reason="autopilot", force=True)
    client.edit_spec_record("esr_1", {"name": "x"}, 2)
    client.place_quest("q1", None, owner_type="user")
    assert [(m, p) for m, p, _, _ in client.sent] == [
        ("GET", "/api/intents/overview"), ("GET", "/api/intents/specs/quest/q1"),
        ("POST", "/api/intents/specs/intent/int_a/refresh"), ("PATCH", "/api/intents/records/esr_1"),
        ("PUT", "/api/intents/placements")]
    assert client.sent[0][2] == {"ownerType": "team", "ownerId": "t1", "includeArchived": None}
    assert client.sent[2][3] == {"reason": "autopilot", "force": True}
    assert client.sent[3][3] == {"expectedRevision": 2, "fields": {"name": "x"}}
    assert client.sent[4][3] == {"ownerType": "user", "moves": [{"questId": "q1", "intentId": None}]}
    assert all("actor" not in json.dumps(body or {}).lower() for _, _, _, body in client.sent)


def test_a_409_becomes_an_explicit_refusal_with_the_locked_fields():
    detail = json.dumps({"detail": {"code": "FIELD_LOCKED", "message": "edited", "lockedFields": ["goals"]}})
    client = Recorder(error=QuestApiError(f"Quest API PATCH /x -> 409: {detail}", status=409))
    result = client.edit_spec_record("esr_1", {"goals": "x"}, 1)
    assert result["refused"] and result["code"] == "FIELD_LOCKED" and result["lockedFields"] == ["goals"]


def test_a_failed_write_is_never_an_empty_dict_and_a_failed_read_is_none():
    client = Recorder(error=QuestApiError("unreachable"))
    assert client.refresh_spec("quest", "q1")["refused"] is True
    assert client.get_spec("quest", "q1") is None and client.list_intents() is None
    assert refusal_of(QuestApiError("plain", status=500))["message"] == "plain"


def test_the_client_returns_a_202_body_unchanged():
    client = Recorder(reply={"pending": True, "decisionId": "dec_1"})
    assert client.place_quest("q1", "int_a") == {"pending": True, "decisionId": "dec_1"}


# --- registration --------------------------------------------------------------------------------

def test_writes_are_on_request_only_and_reads_are_not():
    specs = {n: b(CREDS) for n, b in intents.QUEST_INTENT_BUILDERS.items()}
    assert {n for n, s in specs.items() if s.on_request_only} == {"refresh_spec", "edit_spec_record", "place_quest"}
    assert not specs["list_intents"].mutates and not specs["get_spec"].mutates
    assert set(intents.SPEC_KEEPING_PINS) == set(specs)


def test_no_credentials_means_no_intent_tools():
    assert all(build({}) is None for build in intents.QUEST_INTENT_BUILDERS.values())
