"""The async updater can rewrite a card's embedded name/description; doing so must change the
card's embedding fingerprint so VectorStore.sync() re-embeds it (and the name is part of the
embedded text). Covers the requirement that updating embedded fields triggers re-embedding."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from quest_ai_runner.adapters.file_context_store import FileContextStore


def _write(cards_dir: Path, card: Dict[str, Any]) -> None:
    (cards_dir / f"{card['id']}.json").write_text(json.dumps(card, indent=2), encoding="utf-8")


def _card(card_id: str, **kw: Any) -> Dict[str, Any]:
    base = {
        "id": card_id, "keywords": kw.get("keywords", []), "summary": kw.get("summary", ""),
        "name": kw.get("name", ""), "description": kw.get("description", ""),
        "files": kw.get("files", []), "content": kw.get("content", []),
        "conventions": [], "provenance": {}, "usage_count": 0, "last_outcome": "unknown",
    }
    return base


def _fps(store: FileContextStore) -> Dict[str, str]:
    return {it["id"]: it["fingerprint"] for it in store.export_for_embedding()}


def _text(store: FileContextStore, item_id: str) -> str:
    return next(it["text"] for it in store.export_for_embedding() if it["id"] == item_id)


def test_updating_description_changes_embedding_fingerprint(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _write(cards_dir, _card("dreams", keywords=["dream"], summary="dreams", description="old desc"))
    store = FileContextStore(str(cards_dir), confidence_threshold=0.0)

    fp_before = _fps(store)["card:dreams"]
    assert store.update_card("dreams", fields={"description": "all about the user's dream journal"})
    fp_after = _fps(store)["card:dreams"]

    assert fp_before != fp_after, "changing the embedded description must re-fingerprint the card"


def test_updating_name_is_embedded_and_re_fingerprints(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _write(cards_dir, _card("dreams", keywords=["dream"], summary="dreams", name="Dreams"))
    store = FileContextStore(str(cards_dir), confidence_threshold=0.0)

    fp_before = _fps(store)["card:dreams"]
    assert store.update_card("dreams", fields={"name": "Dream Journal Insights"})

    assert "Dream Journal Insights" in _text(store, "card:dreams"), "the name must be embedded"
    assert fp_before != _fps(store)["card:dreams"], "changing the name must re-fingerprint"


def test_unchanged_card_keeps_stable_fingerprint(tmp_path):
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    _write(cards_dir, _card("d", keywords=["x"], summary="s", description="d"))
    store = FileContextStore(str(cards_dir), confidence_threshold=0.0)
    assert _fps(store)["card:d"] == _fps(store)["card:d"], "fingerprint must be stable when unchanged"
