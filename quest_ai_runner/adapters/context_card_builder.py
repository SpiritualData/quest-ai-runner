"""Where the bootstrap walk asks the hub's ``cards-from-files`` seat for the topic cards of the files no card covers.

The walk (``context_store_bootstrap_walk``) keeps everything Quest-specific: the folder gate, the card limit and ceilings, origin
stamps, scope tags and where the cards are written. What it asks of the seat is only the proposal. Adapters never import the hub
connector, so ``quest_ai_runner.hub`` registers the factory that resolves the seat through QAR's Assembler (the same pattern as
``core.tools.set_quest_client_factory``).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

CardsFromFilesFactory = Callable[[Any, Optional[str]], Any]
factory: Optional[CardsFromFilesFactory] = None


def set_cards_from_files_factory(make: Optional[CardsFromFilesFactory]) -> None:
    """Register ``make(provider, model)``, which returns the selected ``cards-from-files`` arm built for that provider."""
    global factory
    factory = make


def by_id(cards: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    """The cards the store holds, keyed by id for the seat; a repeated id is kept under its position so none is lost."""
    keyed: Dict[str, Mapping[str, Any]] = {}
    for position, card in enumerate(cards):
        key = str(card.get("id") or "")
        keyed[key if key and key not in keyed else f"{key}#{position}"] = card
    return keyed


def propose_topic_cards(files: Sequence[str], provider: Any, model: Optional[str], existing_cards: Sequence[Mapping[str, Any]], walk_root: Any) -> List[dict]:
    """The cards the selected arm proposes for ``files``. Never raises for a model failure (the arm answers an empty list)."""
    if factory is None:
        raise RuntimeError("no cards-from-files factory is registered: import quest_ai_runner (its hub connector registers it)")
    return factory(provider, model).build(str(walk_root), files, by_id(existing_cards))
