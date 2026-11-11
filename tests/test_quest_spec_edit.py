"""ESOI Application with Quest as the records location: read, edit, ensure-one addition, an edit held for approval and a refusal, against a fake Quest API
(no network, no model)."""
from typing import Any, Dict, List

from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.quest_spec_edit import QuestSpecEdit

QUEST = "quest-1"


class FakeQuest:
    """The three spec calls of the Quest client, answering the shapes the server does: a record dict on success, ``{"pending": True, "decisionId"}`` for a
    held change, ``{"refused": True, ...}`` for a refusal."""
    last_error = ""

    def __init__(self, autopilot: bool = True):
        self.autopilot = autopilot
        self.calls: List[str] = []
        self.records: Dict[str, Dict[str, Any]] = {
            "esr_s1": {"recordId": "esr_s1", "type": "solution", "revision": 1, "fields": {"name": "Reach", "purpose": "old"}, "fieldMeta": {},
                       "milestoneEntries": [{"id": "m1", "sentence": "First reached.", "strategy": "", "metricId": None, "direction": "up", "final": False,
                                             "optional": False, "metAt": "2026-01-01T00:00:00Z"}]},
            "esr_m1": {"recordId": "esr_m1", "type": "metric", "revision": 1, "fields": {"name": "Signups", "solution_ids": ["esr_s1"]}, "fieldMeta": {}},
            "esr_problem_anchor": {"recordId": "esr_problem_anchor", "type": "problem", "revision": 1, "derived": True, "fields": {"problem": "Few know us"}, "fieldMeta": {}}}
        self.locked = {("esr_s1", "description")}

    def get_spec(self, subject_type, subject_id):
        self.calls.append("get")
        return {"subjectType": subject_type, "subjectId": subject_id, "records": [dict(r) for r in self.records.values()]}

    def create_spec_record(self, subject_type, subject_id, record_type, fields, *, before=None):
        self.calls.append("create")
        self.before = before
        if not self.autopilot:
            return {"refused": True, "status": 403, "code": "FORBIDDEN", "message": "Autopilot is off for this quest", "lockedFields": []}
        record_id = "esr_new%d" % len(self.records)
        self.records[record_id] = {"recordId": record_id, "type": record_type, "revision": 1, "fields": dict(fields), "fieldMeta": {}}
        return dict(self.records[record_id])

    def edit_spec_record(self, record_id, fields, expected_revision, *, status=None, milestone_entries=None):
        self.calls.append("edit")
        record = self.records[record_id]
        if record.get("derived"):
            return {"pending": True, "decisionId": "decision-9"}
        if any((record_id, name) in self.locked for name in fields):
            return {"refused": True, "status": 409, "code": "FIELD_LOCKED", "message": "x", "lockedFields": sorted(fields)}
        record["fields"].update(fields)
        if milestone_entries is not None:
            record["milestoneEntries"] = milestone_entries
        record["revision"] += 1
        return dict(record)


def arm(quest=None):
    return QuestSpecEdit(client=quest or FakeQuest())


def change(**edit):
    return {"subject": QUEST, "edit": edit}


def test_read_gives_the_quests_records_by_table():
    tables = arm().read(QUEST)
    assert tables["solutions"]["esr_s1"]["purpose"] == "old" and tables["metrics"]["esr_m1"]["name"] == "Signups"


def test_an_edit_is_made_through_quest_and_says_before_and_after():
    quest = FakeQuest()
    out = arm(quest).apply(change(kind="spec_edit", target="esr_s1", field="purpose", value="new"))
    assert out == {"applied": True, "table": "solutions", "target": "esr_s1", "field": "purpose", "before": "old", "after": "new"}
    assert quest.records["esr_s1"]["fields"]["purpose"] == "new"


def test_an_addition_creates_once_and_a_repeat_creates_nothing():
    quest = FakeQuest()
    add = change(kind="record_add", table="implementations", key="Post A", fields={"solution_id": "esr_s1", "description": "Hypothesis: short posts get more clicks."})
    first, second = arm(quest).apply(add), arm(quest).apply(dict(add, edit=dict(add["edit"], key="  post a ")))
    assert first["created"] is True and second["created"] is False and second["note"] == "already there"
    assert [r for r in quest.records.values() if r["type"] == "implementation"].__len__() == 1 and quest.calls.count("create") == 1


def test_a_new_solution_names_the_solution_it_comes_before_and_is_last_otherwise():
    quest = FakeQuest()
    arm(quest).apply(change(kind="record_add", table="solutions", key="Launch page", fields={"purpose": "Show it.", "before": "esr_s1"}))
    assert quest.before == "esr_s1" and "before" not in quest.records["esr_new3"]["fields"]
    arm(quest).apply(change(kind="record_add", table="solutions", key="Last one", fields={"purpose": "Close."}))
    assert quest.before is None


def test_a_milestone_is_added_once_and_keeps_the_ones_already_met():
    quest = FakeQuest()
    add = change(kind="milestone_add", target="esr_s1", key="Ten signups.", fields={"metric": "esr_m1"})
    assert arm(quest).apply(add)["created"] is True
    again = arm(quest).apply(add)
    entries = quest.records["esr_s1"]["milestoneEntries"]
    assert again["note"] == "already there" and [e["sentence"] for e in entries] == ["First reached.", "Ten signups."]
    assert entries[0]["metAt"] == "2026-01-01T00:00:00Z" and entries[1]["metricId"] == "esr_m1"


def test_an_edit_quest_holds_for_the_owner_is_reported_as_proposed_not_done():
    out = arm().apply(change(kind="problem_edit", target="esr_problem_anchor", field="problem", value="Few people know us"))
    assert out["applied"] is False and out["proposed"] is True and out["decision"] == "decision-9" and "waiting for approval" in out["reason"]


def test_a_field_a_person_edited_is_refused_plainly():
    out = arm().apply(change(kind="spec_edit", target="esr_s1", field="description", value="mine"))
    assert out["applied"] is False and "a person edited description" in out["reason"]


def test_permission_denied_is_reported_plainly_and_nothing_is_created():
    quest = FakeQuest(autopilot=False)
    out = arm(quest).apply(change(kind="record_add", table="metrics", key="Clicks", fields={"solution_ids": "esr_s1"}))
    assert out["applied"] is False and out["reason"].startswith("not permitted: Autopilot is off") and not any(k.startswith("esr_new") for k in quest.records)


def test_a_missing_record_and_an_empty_key_are_refused_by_the_hub_edit():
    assert arm().apply(change(kind="spec_edit", target="esr_none", field="purpose", value="x"))["applied"] is False
    assert arm().apply(change(kind="record_add", table="metrics", key="", fields={"name": "x"}))["applied"] is False


def test_the_client_sends_before_in_the_body_only_when_given():
    from quest_ai_runner.the_body.internal_context.quest.client.quest_client_intents import IntentsMixin
    sent = []

    class Client(IntentsMixin):
        def __init__(self):
            pass

        def intent_write(self, method, path, body, **kwargs):
            sent.append((method, path, body))
            return {}
    Client().create_spec_record("quest", "q1", "solution", {"name": "A"}, before="esr_s1")
    Client().create_spec_record("quest", "q1", "solution", {"name": "B"})
    assert sent[0][2] == {"type": "solution", "fields": {"name": "A"}, "before": "esr_s1"} and sent[0][1].endswith("/specs/quest/q1/records")
    assert "before" not in sent[1][2]
