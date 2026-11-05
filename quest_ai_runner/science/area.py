"""``the-science``: what a request needs to know beyond the person's own material comes from the world, with its source.

The area arm ``web_with_support_check`` takes the web member (``search-the-web``) as a typed seat and offers the hub's lines for the
area, ``describe``, ``search(queries)`` and ``fetch(url)``: one compact observation of title, link and snippet per hit, and the passages
of one page that bear on a focus. Neither raises; a failure comes back as an observation of kind ``error``. The research is the hub's
snippet-first adapter under QAR's tuning (``science/web_research.py``), unchanged.
"""
from __future__ import annotations

import typing
from typing import Any, List, Optional, Union

from esoi import implementation, solution
from hubofhubs.the_world.science.interface import WorldKnowing

from .web_research import WebSearching


@solution("the-science", kind="event", entry="search", example="meditation and attention")
@typing.runtime_checkable
class KnowingTheWorld(WorldKnowing, typing.Protocol):
    """The hub's ``the-science`` interface: what the world says now, as observations a reader can cite."""


@implementation("the-science", "web_with_support_check")
class WebWithSupportCheck:
    """World questions answered from snippet-first web research; each line is handed to the web member that runs it."""
    REQUIRES = ()     # the seat it is handed

    def __init__(self, web: WebSearching) -> None:
        self.web = web

    def describe(self) -> str:
        return self.web.describe()

    def search(self, queries: Union[str, List[str]], *, max_results: Optional[int] = None, fresh: bool = False) -> Any:
        return self.web.search(queries, max_results=max_results, fresh=fresh)

    def fetch(self, url: str, *, focus: Optional[str] = None, fresh: bool = False, full: bool = False, part: Optional[int] = None) -> Any:
        return self.web.fetch(url, focus=focus, fresh=fresh, full=full, part=part)
