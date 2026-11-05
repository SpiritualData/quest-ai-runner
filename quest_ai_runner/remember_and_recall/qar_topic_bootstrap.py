"""QAR's arm of the hub's ``cards-from-files`` seat: the model-written topic cards the bootstrap has always proposed.

``FileContextStore.bootstrap`` asks a model to name the areas of the files it has not carded and to write one topic card for each
area, then folds the result into the cards the store already holds. That pass (``adapters/context_topic_cards``) is what this arm
returns, unchanged: it is QAR's own selection for the seat, so a card is a topic that groups information from several files, never a
file index. The model is a constructor need (the ``call-a-model`` seat), as in the hub's ``llm_topic_cards``; every build spends calls.

What stays in the bootstrap and is not this arm's business: where the cards are written, who they belong to (origin stamps and scope
tags), the card limit, the per-folder ceilings, the folder gate and the upload to the Quest backend.
"""
from __future__ import annotations

import typing
from typing import Any, List, Mapping, Optional, Sequence

from esoi import implementation, solution
from hubofhubs.the_intelligence.subconscious.cards_from_files.cards_from_files import changed_cards
from hubofhubs.the_intelligence.subconscious.cards_from_files.interface import CardsFromFiles
from hubofhubs.the_world.model_plumbing.vendor_model_caller.interface import ModelCalling

from ..adapters.context_topic_cards import _llm_topic_cards


@solution("cards-from-files", kind="event", entry="build", example={"root": ".", "files": [], "existing": {}})
@typing.runtime_checkable
class CardBuilding(CardsFromFiles, typing.Protocol):
    """The hub's ``cards-from-files`` interface (``build`` and ``changed``) in QAR's scope, where QAR's records select the arm."""


@implementation("cards-from-files", "qar_topic_bootstrap", scope="quest_ai_runner")
class QarTopicBootstrap:
    """The topic cards QAR's bootstrap proposes for files no card covers: areas discovered, topics written per area, duplicates
    folded into the cards ``existing`` holds. Returns ``{id, name, keywords, summary, files}`` (files as paths); the bootstrap stamps,
    limits and writes them. With no model nothing can be identified and the answer is an empty list."""
    REQUIRES = ()

    def __init__(self, provider: ModelCalling, model: Optional[str] = None):
        self.provider = provider
        self.model = model

    def build(self, root: str, files: Sequence[str], existing: Mapping[str, Mapping[str, Any]]) -> List[dict]:
        return _llm_topic_cards(list(files), self.provider, model=self.model, existing_cards=list(existing.values()), walk_root=root)

    def changed(self, root: str, cards: Mapping[str, Mapping[str, Any]]) -> List[str]:
        return changed_cards(root, cards)
