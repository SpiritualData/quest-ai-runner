"""``QuestClient.list_quest_notes`` pages the notes endpoint and never asks for an unbounded list."""
from quest_ai_runner.runner.quest_client import QuestClient


class _Recorder(QuestClient):
    def __init__(self, pages):
        super().__init__(base_url="http://x", api_key="k")
        self.pages = list(pages)
        self.calls = []

    def _request(self, method, path, *, params=None, body=None, timeout_override=None):
        self.calls.append(dict(params or {}))
        return self.pages.pop(0)


def _note(i):
    return {"note_id": f"n{i}", "text": str(i)}


def test_pages_until_limit_and_returns_oldest_to_newest_with_the_total():
    c = _Recorder([
        {"notes": [_note(i) for i in (9, 8, 7)], "total": 10, "unfiltered_total": 10, "next_cursor": "c1"},
        {"notes": [_note(i) for i in (6, 5)], "total": 10, "unfiltered_total": 10, "next_cursor": "c2"},
    ])
    notes = c.list_quest_notes("quest_1", limit=5)
    assert [n["note_id"] for n in notes] == ["n5", "n6", "n7", "n8", "n9"]
    assert notes.total == 10
    assert c.calls == [{"limit": 5, "before": None}, {"limit": 2, "before": "c1"}]


def test_stops_when_the_server_has_no_more():
    c = _Recorder([{"notes": [_note(1), _note(0)], "total": 2, "unfiltered_total": 2, "next_cursor": None}])
    notes = c.list_quest_notes("quest_1")
    assert [n["note_id"] for n in notes] == ["n0", "n1"] and notes.total == 2 and len(c.calls) == 1


def test_a_backend_without_paging_answers_a_bare_list_and_only_the_newest_are_kept():
    c = _Recorder([[_note(i) for i in range(10)]])
    notes = c.list_quest_notes("quest_1", limit=4)
    assert [n["note_id"] for n in notes] == ["n6", "n7", "n8", "n9"] and notes.total == 10
