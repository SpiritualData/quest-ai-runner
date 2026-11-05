"""The interfaces of Context Retriever's Quest arms: ``select-context`` (which stored cards a request reads) and ``choose-the-context`` (the context a request needs, from the person's own cards, files and conversations). Importing this module runs nothing."""
from __future__ import annotations

import typing
from typing import Any, Dict, Optional
from esoi import solution
from hubofhubs.api import AssembledContext, ContextAssembler


@solution("choose-the-context", kind="event", entry="choose", example="What should I work on first this week?")
@typing.runtime_checkable
class ContextChoosing(typing.Protocol):
    """The context a request needs, chosen from the person's own cards, files and conversations."""

    def choose(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, assembler: Optional[ContextAssembler] = None) -> AssembledContext: ...


@solution("select-context", kind="event", entry="assemble", example="What should I work on first this week?")
@typing.runtime_checkable
class ContextSelecting(ContextAssembler, typing.Protocol):
    """Which stored cards, files and conversations a request reads before it is planned: ``assemble`` returns the rendered context and the card ids
    in relevance order (empty when nothing is confident enough) and ``record`` writes the outcome back. The hub's Context Retriever arms are
    candidates for the same seat and compete on its metric."""
