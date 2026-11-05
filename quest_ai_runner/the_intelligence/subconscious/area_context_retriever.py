"""``choose-the-context``: the material a planner reads is the material the request concerns, however it is worded.

The area arm ``per_lane_assembly`` takes the area's members as typed seats and offers the area's one line, ``choose(task_text, assembler=...)``:
the context the lane's assembler (``select-context``, which the lane's configuration builds, so it is passed in) selects for a request. The other lines hand a question to the member that owns it:
``resolve`` follows a card's pointer to the person's current text (``resolve-references``), ``search`` ranks the cards of a run with the
specification records of a repository in one list (``search-cards-and-specs``) and ``index`` finds a card by what it means
(``index-by-meaning``). Each member is built on first use, so a line that does not read one never asks for it. Which assembler the lane
runs, and how it is built from the environment, stay the lane's configuration (``config_assembler.py``), unchanged.
"""
from __future__ import annotations

import typing
from typing import Any, Dict, List, Optional, Sequence

from esoi import implementation, solution
from esoi.assemble import Lazy
from hubofhubs.api import AssembledContext, ContextAssembler, VectorStore
from hubofhubs.api import ReferenceResolver

from quest_ai_runner.the_intelligence.subconscious.context_retriever.unified_search import search_cards_and_specs


@solution("choose-the-context", kind="event", entry="choose", example="What should I work on first this week?")
@typing.runtime_checkable
class ContextChoosing(typing.Protocol):
    """The context a request needs, chosen from the person's own cards, files and conversations."""

    def choose(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, assembler: Optional[ContextAssembler] = None) -> AssembledContext: ...


@implementation("choose-the-context", "per_lane_assembly")
class PerLaneAssembly:
    """The reference resolver and the vector index, each handed in as a seat and built on first use; ``choose`` reads the lane's assembler passed
    in, ``search`` ranks cards and specification records, and the other lines delegate to the member that owns them."""
    REQUIRES = ()     # the seats it is handed

    def __init__(self, references: Lazy[ReferenceResolver], index: Lazy[VectorStore]) -> None:
        self.references, self.index = references, index

    def choose(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, assembler: Optional[ContextAssembler] = None) -> AssembledContext:
        if assembler is None:
            raise ValueError("choose reads the lane's context assembler: pass it as assembler=")
        return assembler.assemble(task_text, meta=meta)

    def resolve(self, locator: Dict[str, Any], *, max_chars: int = 2000) -> str:
        return self.references.resolve(locator, max_chars=max_chars)

    def search(self, task: str, views: Sequence[Any], **settings: Any) -> Any:
        return search_cards_and_specs(task, views, **settings)

    def find(self, query: str, *, scope: Optional[Dict[str, Any]] = None, top_k: int = 8) -> List[Any]:
        return self.index.search(query, scope=scope, top_k=top_k)
