"""Seeding autopilot settings from files: written when unset or still the runner's own, never over a person's change, conflicts reported."""
import logging

from quest_ai_runner.body.internal_context.quest.autopilot_seed import SeedLedger, seed_autopilot, seed_quest_autopilot

Q, A = "quest_x", "rep-rep-1"


class Client:
    def __init__(self, **live):
        self.ap = {"id": A, **live}
        self.quest = {"instructions": ""}
        self.patches = []

    def list_autopilots(self, quest_id):
        return {"autopilots": [dict(self.ap)]}

    def update_autopilot(self, quest_id, autopilot_id, body):
        self.patches.append(body)
        for k, v in body.items():
            self.ap[k] = {**self.ap.get(k, {}), **v} if isinstance(v, dict) else v

    def get_quest_autopilot(self, quest_id):
        return {"autopilot": dict(self.quest)}

    def update_quest_autopilot(self, quest_id, body):
        self.patches.append(body)
        self.quest.update(body)


def ledger(tmp_path):
    return SeedLedger(str(tmp_path / "seed.json"))


def test_seeds_fields_that_are_unset(tmp_path):
    c = Client(model=None, instructions="")
    r = seed_autopilot(c, Q, A, {"model": "opus", "instructions": "Do it."}, ledger(tmp_path))
    assert sorted(r.written) == ["instructions", "model"] and r.clean
    assert c.ap["model"] == "opus"


def test_a_persons_change_is_never_overwritten_and_is_reported(tmp_path, caplog):
    c = Client(model=None)
    led = ledger(tmp_path)
    seed_autopilot(c, Q, A, {"model": "opus"}, led)
    c.ap["model"] = "sonnet"                                   # a person edits it in Quest
    with caplog.at_level(logging.WARNING):
        r = seed_autopilot(c, Q, A, {"model": "opus"}, SeedLedger(str(tmp_path / "seed.json")))
    assert c.ap["model"] == "sonnet" and c.patches == [{"model": "opus"}]
    assert r.conflicts == [{"field": "model", "kept": "sonnet", "file": "opus"}] and not r.clean
    assert sum("kept Quest's value" in m for m in caplog.messages) == 1


def test_a_person_value_present_before_the_first_seed_is_a_conflict(tmp_path):
    c = Client(model="sonnet")
    r = seed_autopilot(c, Q, A, {"model": "opus"}, ledger(tmp_path))
    assert c.patches == [] and r.conflicts[0]["field"] == "model"


def test_reapplies_its_own_earlier_value_when_the_file_changes(tmp_path):
    c = Client(model=None)
    seed_autopilot(c, Q, A, {"model": "opus"}, ledger(tmp_path))
    r = seed_autopilot(c, Q, A, {"model": "fable"}, SeedLedger(str(tmp_path / "seed.json")))
    assert c.ap["model"] == "fable" and r.written == ["model"] and r.clean


def test_equal_values_write_nothing_and_are_learned_as_ours(tmp_path):
    c = Client(model="opus")
    r = seed_autopilot(c, Q, A, {"model": "opus"}, ledger(tmp_path))
    assert c.patches == [] and r.unchanged == ["model"]
    r = seed_autopilot(c, Q, A, {"model": "fable"}, SeedLedger(str(tmp_path / "seed.json")))
    assert c.ap["model"] == "fable"


def test_force_overrides_a_persons_value(tmp_path):
    c = Client(model="sonnet")
    r = seed_autopilot(c, Q, A, {"model": "opus"}, ledger(tmp_path), force=True)
    assert c.ap["model"] == "opus" and r.clean


def test_only_fields_in_the_file_are_touched_and_email_recipients_nest(tmp_path):
    c = Client(model="sonnet", name="Mary", email={"recipients": None})
    r = seed_autopilot(c, Q, A, {"email": {"recipients": ["a@example.org"]}}, ledger(tmp_path))
    assert c.patches == [{"email": {"recipients": ["a@example.org"]}}] and c.ap["model"] == "sonnet" and r.written == ["email.recipients"]


def test_quest_level_instructions_follow_the_same_rule(tmp_path):
    c = Client()
    led = ledger(tmp_path)
    seed_quest_autopilot(c, Q, {"instructions": "v1"}, led)
    c.quest["instructions"] = "edited by a person"
    r = seed_quest_autopilot(c, Q, {"instructions": "v2"}, SeedLedger(str(tmp_path / "seed.json")))
    assert c.quest["instructions"] == "edited by a person" and r.conflicts[0]["field"] == "instructions"


def test_a_missing_autopilot_raises(tmp_path):
    import pytest
    with pytest.raises(KeyError):
        seed_autopilot(Client(), Q, "nope", {"model": "opus"}, ledger(tmp_path))
