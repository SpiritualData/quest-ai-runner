"""``the-science``: what a request needs to know beyond the person's own material comes from the world, with its source.

The area arm ``web_with_support_check`` takes the web member (``search-the-web``) as a typed seat and offers the hub's lines for the
area, ``describe``, ``search(queries)`` and ``fetch(url)``: one compact observation of title, link and snippet per hit, and the passages
of one page that bear on a focus. Neither raises; a failure comes back as an observation of kind ``error``. The research is the hub's
snippet-first adapter under QAR's tuning (``science/web_research.py``), unchanged.
"""
from __future__ import annotations

from typing import Any, List, Optional, Union

from esoi import implementation

from quest_ai_runner.the_world.tools.web_search.web_research import WebSearching



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
        return self.page_reader().fetch(url, focus=focus, fresh=fresh, full=full, part=part)

    def page_reader(self) -> Any:
        """The member that reads one page: the web member itself when it can (the snippet-first research adapter), else the one-call research adapter built from the
        environment. A search-only backend (provider grounding) has no page read, which left every page a review tried to open as "could not be opened"."""
        if hasattr(self.web, "fetch"):
            return self.web
        if getattr(self, "reader", None) is None:
            import os
            from quest_ai_runner.the_world.tools.web_search.web_research import build_web_research_from_env
            self.reader = build_web_research_from_env(os.environ)
        if self.reader is None:
            raise RuntimeError("no web research is configured to read a page")
        return self.reader
