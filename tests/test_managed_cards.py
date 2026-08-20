"""Offline tests for CONSUMER-MANAGED context cards.

A consumer may derive a card from its OWN source of truth (one card per record in its database) and
rewrite that card whenever the record changes. Such a card declares the parts only its writer owns:

  * ``managed_fields`` -- embedded fields (``name``/``description``/``summary``) the card-update API
    must not edit.
  * ``managed_items``  -- content-item ids the card-update API must not remove or replace.

Additions are deliberately NOT blocked: a learning updater keeps accruing notes onto the same card,
which is the point of putting them there, while the consumer-owned digest and its live reference stay
exactly as the consumer wrote them. A card declaring neither key must behave exactly as before.

Offline: filesystem card repo only, no provider, no network.
"""
from __future__ import annotations

import json
from pathlib import Path

from quest_ai_runner.adapters.file_context_store import FileContextStore, _managed_names


def _store(tmp_path: Path) -> FileContextStore:
    return FileContextStore(str(tmp_path / "cards"), repo_root=None, auto_bootstrap=False)


def _write_card(store: FileContextStore, card: dict) -> None:
    store._write_card_atomic(card["id"], card)


def _managed_card() -> dict:
    return {
        "id": "quest-abc",
        "name": "Run a marathon",
        "summary": "Run a marathon by June",
        "description": "Outcome: run a marathon by June. Current state: runs 5k.",
        "keywords": ["marathon", "running"],
        "managed_fields": ["name", "description", "summary"],
        "managed_items": ["quest-state"],
        "content": [
            {"id": "quest-state", "type": "quest", "locator": {"quest_id": "abc"},
             "why": "live quest state", "ts": 100.0},
        ],
    }


def test_managed_fields_are_not_overwritten(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_card(store, _managed_card())

    assert store.update_card("quest-abc", fields={"name": "something else",
                                                  "description": "a stale rewrite"}) is True

    card = store.get_card("quest-abc")
    assert card["name"] == "Run a marathon"
    assert card["description"].startswith("Outcome: run a marathon")


def test_managed_item_survives_remove_and_replace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_card(store, _managed_card())

    store.update_card("quest-abc", remove=["quest-state"])
    store.update_card("quest-abc", replace=[("quest-state", {"type": "note",
                                                             "locator": {"text": "hijacked"}})])

    items = {it["id"]: it for it in store.get_card("quest-abc")["content"]}
    assert "quest-state" in items
    assert items["quest-state"]["type"] == "quest"
    assert items["quest-state"]["locator"] == {"quest_id": "abc"}


def test_learned_content_still_accrues_on_a_managed_card(tmp_path: Path) -> None:
    """The whole point: a managed card is still where this record's learned notes land."""
    store = _store(tmp_path)
    _write_card(store, _managed_card())

    assert store.add_content("quest-abc", {"id": "note-1", "type": "note",
                                           "locator": {"text": "user prefers morning runs"},
                                           "ts": 200.0}) is True

    ids = {it["id"] for it in store.get_card("quest-abc")["content"]}
    assert ids == {"quest-state", "note-1"}


def test_unmanaged_card_is_unchanged(tmp_path: Path) -> None:
    """No declaration -> the previous behaviour, byte for byte."""
    store = _store(tmp_path)
    _write_card(store, {
        "id": "learned", "name": "old name", "description": "old description",
        "content": [{"id": "item-1", "type": "note", "locator": {"text": "old"}, "ts": 1.0}],
    })

    store.update_card("learned", fields={"name": "new name", "description": "new description"},
                      remove=["item-1"])

    card = store.get_card("learned")
    assert card["name"] == "new name"
    assert card["description"] == "new description"
    assert card["content"] == []


def test_partially_managed_card_still_accepts_the_unmanaged_field(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_card(store, {"id": "half", "name": "kept", "description": "editable",
                        "managed_fields": ["name"]})

    store.update_card("half", fields={"name": "ignored", "description": "written"})

    card = store.get_card("half")
    assert card["name"] == "kept"
    assert card["description"] == "written"


def test_malformed_declaration_degrades_to_unmanaged(tmp_path: Path) -> None:
    assert _managed_names(None) == set()
    assert _managed_names("name") == set()
    assert _managed_names(["name", "", None, 3]) == {"name", "3"}

    store = _store(tmp_path)
    _write_card(store, {"id": "bad", "name": "old", "managed_fields": "name"})
    store.update_card("bad", fields={"name": "new"})
    assert store.get_card("bad")["name"] == "new"


def test_managed_declaration_persists_across_updates(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_card(store, _managed_card())
    store.add_content("quest-abc", {"id": "note-1", "type": "note", "locator": {"text": "x"}})

    raw = json.loads((tmp_path / "cards" / "quest-abc.json").read_text())
    assert raw["managed_fields"] == ["name", "description", "summary"]
    assert raw["managed_items"] == ["quest-state"]
