"""Offline tests for ``QuestApiCardRepository`` speaking the CardRepository protocol.

Fully OFFLINE: the HTTP layer (``_request``) is stubbed, so these prove the CONTRACT the
context store depends on rather than any network behaviour.

Why this file exists. The Quest-API card backend is what makes quest-backend the source of truth
for context cards instead of each runner keeping its own ``.quest-context`` directory, and it was
broken end to end in a way nothing reported:

  * ``load_all`` returned a LIST, while the protocol (and ``FileContextStore._load_all``, which
    calls ``.values()`` on the result) requires ``{card_id: card_dict}``. Every read either raised
    into a swallowing except-block or produced garbage.
  * the API's reply carried the cards under ``cards`` as a MAPPING at the time, so the old
    ``list(result["cards"])`` produced a list of card IDS, i.e. strings where cards were expected.
  * ``search_cards`` took ``max_results`` positionally and returned a list, but the store calls it
    as ``search(text, limit=...)`` and then requires a dict, so the native search arm could never
    run at all.

So: mapping in, mapping out, both reply shapes accepted, and the store's own call signature.
"""
from typing import Any, Dict, Optional

from quest_ai_runner.adapters.file_context_store import FileContextStore
from quest_ai_runner.adapters.quest_api_card_repository import QuestApiCardRepository

CARD_A = {"id": "morning-routine", "name": "Morning Routine", "summary": "Before 9am."}
CARD_B = {"id": "production-bugs", "name": "Production Bugs", "summary": "Server crashes."}


def repo_with(reply: Optional[Dict[str, Any]], record=None) -> QuestApiCardRepository:
    """A repository whose HTTP layer returns ``reply`` for every request."""
    repo = QuestApiCardRepository(base_url="https://api.example", api_key="k", user_id="u")

    def fake_request(method, path, body=None, params=None):
        if record is not None:
            record.append({"method": method, "path": path, "params": params})
        return reply

    repo._request = fake_request  # type: ignore[assignment]
    return repo


def test_load_all_returns_the_mapping_the_protocol_requires():
    repo = repo_with({"cards": [CARD_A, CARD_B]})

    cards = repo.load_all()

    assert isinstance(cards, dict)
    assert cards == {"morning-routine": CARD_A, "production-bugs": CARD_B}
    # The store calls .values() on this; a list would raise there.
    assert [c["name"] for c in cards.values()] == ["Morning Routine", "Production Bugs"]


def test_load_all_accepts_the_older_mapping_reply_shape():
    """A deployed backend of the older vintage answers with a mapping, not a list."""
    repo = repo_with({"cards": {"morning-routine": CARD_A, "production-bugs": CARD_B}})

    assert repo.load_all() == {"morning-routine": CARD_A, "production-bugs": CARD_B}


def test_load_all_stamps_an_id_that_lived_only_in_the_key():
    repo = repo_with({"cards": {"keyless": {"name": "No id field"}}})

    assert repo.load_all() == {"keyless": {"name": "No id field", "id": "keyless"}}


def test_load_all_degrades_to_empty_rather_than_raising():
    for reply in (None, {}, {"cards": None}, {"cards": "junk"}, {"cards": ["not a card"]}):
        assert repo_with(reply).load_all() == {}


def test_search_cards_is_callable_the_way_the_context_store_calls_it():
    """FileContextStore calls ``search(text, limit=N)`` and requires a dict back."""
    calls: list = []
    repo = repo_with({"cards": [CARD_A]}, record=calls)

    result = repo.search_cards("morning", limit=32)

    assert result == {"morning-routine": CARD_A}
    # The endpoint reads ``max_results``; the old ``max`` was silently ignored.
    assert calls[-1]["params"] == {"q": "morning", "max_results": 32}


def test_search_cards_returns_none_on_a_bad_reply_so_the_store_falls_back():
    assert repo_with(None).search_cards("anything", limit=5) is None


def test_the_context_store_can_actually_read_through_this_repository(tmp_path):
    """The integration that was broken: the store reading cards via the API repo."""
    repo = repo_with({"cards": [CARD_A, CARD_B]})
    store = FileContextStore(str(tmp_path / "cards"), auto_bootstrap=False, card_repository=repo)

    loaded = store._load_all()

    assert set(loaded) == {"morning-routine", "production-bugs"}
    assert loaded["production-bugs"]["name"] == "Production Bugs"
