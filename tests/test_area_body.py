"""The the-body area: its members answer offline by the strict definition, and the arm reads, writes and hands over what it holds."""
import pytest
from esoi import Pending, targets

import quest_ai_runner
from quest_ai_runner.the_body.area_body import BodyActing
from quest_ai_runner.the_world.tools.file_reading.files_adapter import FilesAdapter
from quest_ai_runner.the_world.tools.file_editing.files_writer import FilesWriter
from tests.conftest import StubProvider

AREA_IDS = ("the-body", "persist-cards", "write-files", "operate-a-quest", "claim-reach", "run-queued-tasks",
            "hand-off-a-goal", "access-quest-operations")


def strict_outcome(solution_id):
    example = {sid: example for sid, level, kind, example in targets(quest_ai_runner.assembler())}[solution_id]
    try:
        quest_ai_runner.run(solution_id, example, environ={})
    except Pending as pending:
        assert {reason.kind for reason in pending.reasons} <= {"key", "install", "needs"}, pending.lines
        return "pending"
    return "ok"


@pytest.mark.parametrize("solution_id", AREA_IDS)
def test_each_id_of_the_area_answers_offline(solution_id):
    assert strict_outcome(solution_id) == "pending"


def test_the_stores_pend_on_the_account_key_and_the_folder():
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("persist-cards", "card-1", environ={})
    assert {reason.kind for reason in caught.value.reasons} == {"key"}
    with pytest.raises(Pending) as caught:
        quest_ai_runner.run("write-files", "NOTES.md", environ={})
    assert [reason.kind for reason in caught.value.reasons] == ["key"]


class Account:
    """The slice of the Quest account the area reads."""
    base_url, api_key, team_id = "http://quest.invalid", "key", ""

    def list_quest_goals(self, quest_id, **options):
        return [{"quest_id": quest_id, "title": "Ship the launch note"}]


class Reader(FilesAdapter):
    """The files adapter with the one optional method of the interface it does not carry."""

    def own_updates(self, *args, **options):
        return None


class Cards:
    """A card repository held in a dict."""

    def __init__(self):
        self.cards = {"card-1": {"id": "card-1", "summary": "launch"}}

    def load_all(self):
        return dict(self.cards)

    def read(self, card_id):
        return self.cards.get(card_id)

    def write(self, card_id, card):
        self.cards[card_id] = card
        return True

    def delete(self, card_id):
        return self.cards.pop(card_id, None) is not None

    def exists(self, card_id):
        return card_id in self.cards

    def revision(self):
        return len(self.cards)


class ScriptedModel(StubProvider):
    """A scripted model that cannot search the web."""

    supports_web_search = False

    def web_search(self, *args, **kwargs):
        raise NotImplementedError


def area_over(tmp_path, provider):
    provided = {"access-quest-operations": Account(), "gather-evidence": Reader(str(tmp_path)), "persist-cards": Cards(),
                "write-files": FilesWriter(str(tmp_path), backups_enabled=False), "vendor-model-caller": provider}
    return quest_ai_runner.run(BodyActing, environ={}, provided=provided)


def test_the_area_reads_and_writes_what_the_host_holds(tmp_path):
    (tmp_path / "NOTES.md").write_text("Launch moves to Friday")
    area = area_over(tmp_path, ScriptedModel([]))
    assert "Launch moves to Friday" in area.read("NOTES.md").text
    assert area.card("card-1")["summary"] == "launch"
    assert area.goals("quest-1")[0]["title"] == "Ship the launch note"
    assert area.write("NOTES.md", "Launch moved to Friday").ok
    assert (tmp_path / "NOTES.md").read_text() == "Launch moved to Friday"


def test_the_area_claims_a_reach_and_assigns_a_goal(tmp_path):
    provider = ScriptedModel([{"action": "deep", "rationale": "outside the sources", "model_tier": "sonnet"}],
                             answer_text='{"assignee": "u1", "ai_can_do": true, "reason": "research"}')
    area = area_over(tmp_path, provider)
    assert area.claim("Send the launch note to the whole team")["route"] == "deep"
    members = [{"user_id": "u1", "name": "Ana", "role": "owner"}]
    assigned = area.assign({"outcome": "Ship", "title": "Draft the note", "members": members})
    assert set(assigned) == {"assignee", "ai_can_do"}
