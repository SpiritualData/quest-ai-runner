"""A hub ``CardStore`` over QAR's own card persistence boundary.

``hubofhubs.seats.CardStore`` is the hub's simple seat: add, get, search, delete, count over ``hubofhubs.card.Card``. QAR keeps
its cards as dicts behind ``CardRepository`` (``card_repository`` for files, ``quest_api_card_repository`` for the Quest API).
This adapter lets any ``CardRepository`` stand in that hub seat, so a consumer of the hub (or QAR itself) can ask for a
``card-store`` and get QAR's persistence. It is registered under the name ``qar-card-repository`` by ``quest_ai_runner.hub``.

A hub ``Card`` maps onto the QAR card schema as: ``id`` to ``id``, ``text`` to ``summary``, ``tags`` to ``keywords`` and
``source`` to ``provenance.source``. Search matches every word of the query as a case-insensitive substring of the summary
and keywords, like the hub's own default store; QAR's ranked selection stays in ``FileContextStore``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from hubofhubs.card import Card


class CardRepositoryStore:
    """Keeps hub ``Card`` objects in a QAR ``CardRepository`` (the ``repository`` option of the ``card-store`` seat)."""

    def __init__(self, repository: Any) -> None:
        self.repository = repository

    @staticmethod
    def card_dict(card: Card) -> Dict[str, Any]:
        return {"id": card.id, "keywords": list(card.tags), "summary": card.text, "provenance": {"source": card.source}}

    @staticmethod
    def hub_card(raw: Dict[str, Any]) -> Card:
        provenance = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
        return Card(id=str(raw.get("id", "")), text=str(raw.get("summary", "")), source=str(provenance.get("source", "")),
                    tags=tuple(str(k) for k in raw.get("keywords") or ()))

    def add(self, card: Card) -> str:
        if not self.repository.write(card.id, self.card_dict(card)):
            raise ValueError(f"the card repository refused to store card {card.id!r}")
        return card.id

    def get(self, card_id: str) -> Optional[Card]:
        raw = self.repository.read(card_id)
        return self.hub_card(raw) if raw else None

    def search(self, query: str = "", limit: int = 10, tag: str = "") -> List[Card]:
        words = [w.lower() for w in query.split() if w]
        found = []
        for card_id, raw in sorted(self.repository.load_all().items()):
            card = self.hub_card({**raw, "id": raw.get("id", card_id)})
            haystack = (card.text + " " + " ".join(card.tags)).lower()
            if all(w in haystack for w in words) and (not tag or tag in card.tags):
                found.append(card)
        return found[:max(0, limit)]

    def delete(self, card_id: str) -> bool:
        existed = self.repository.exists(card_id)
        return bool(self.repository.delete(card_id)) and existed

    def count(self) -> int:
        return len(self.repository.load_all())
