"""The standard tool ``search_scientific_literature``: published research papers, found through the hub's OpenAlex arm
(``hubofhubs.science.openalex``, an implementation of the ``know-the-world`` seat; extra ``science``).

Offered only when ``OPENALEX_API_KEY`` is set (OpenAlex gives a free key; the hub arm's docs say what it costs and how to get
one). Without it the tool is not in the catalog and the planner is told, on a request about papers, that it cannot search the
literature (``STANDARD_TOOL_UNAVAILABLE``), so the AI keeps saying plainly that it cannot instead of citing from memory.

The result lists each work with its title, authors, year, journal, DOI link and abstract and states that only these may be cited;
a work with no DOI says so. Nothing here is a model call. The hub reads no environment, so this module reads the names below and
hands them over: ``OPENALEX_API_KEY``, ``OPENALEX_MAILTO`` (a contact address for OpenAlex, optional) and
``QAR_SCIENCE_DAILY_LIMIT`` (real searches per UTC day for this process, default 300).
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .tools import STANDARD_TOOL_BUILDERS, STANDARD_TOOL_UNAVAILABLE, ToolContext, ToolResult, ToolSpec, UnavailableTool

TOOL_NAME = "search_scientific_literature"
DEFAULT_DAILY_LIMIT = 300
MAX_WORKS = 10
#: Words in a request that make the "not available here" note worth its tokens.
ASKING_ABOUT_PAPERS = ("paper", "papers", "literature", "scientific", "scholarly", "study", "studies", "journal", "citation",
                       "cite", "doi", "peer", "pubmed", "meta-analysis")


def daily_limit_from(env: Mapping[str, str]) -> int:
    try:
        return max(0, int(env.get("QAR_SCIENCE_DAILY_LIMIT") or DEFAULT_DAILY_LIMIT))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_LIMIT


def science_literature_spec(env: Mapping[str, str], transport=None) -> Optional[ToolSpec]:
    """The tool, or ``None`` while no OpenAlex key is configured. ``transport`` is for tests (a fake HTTP call)."""
    key = (env.get("OPENALEX_API_KEY") or "").strip()
    if not key:
        return None
    from hubofhubs.science.openalex import OpenAlexWorks, observation_of

    arm = OpenAlexWorks(api_key=key, mailto=(env.get("OPENALEX_MAILTO") or "").strip(), transport=transport,
                        daily_limit=daily_limit_from(env), abstract_chars=400)

    def as_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:  # noqa: ARG001
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult(ok=False, text="Needs query: the topic in a few plain keywords.")
        result = arm.find_works(query, year_from=as_int(args.get("year_from")), year_to=as_int(args.get("year_to")),
                                open_access_only=bool(args.get("open_access_only")),
                                sort_by_citations=bool(args.get("most_cited")),
                                limit=min(as_int(args.get("limit")) or 5, MAX_WORKS))
        observation = observation_of(result, abstract_chars=arm.abstract_chars)
        if observation.kind == "error":
            return ToolResult(ok=False, text=f"The literature search failed: {observation.error}. Tell the person it did not work; "
                                              "cite no paper, author or DOI from memory.")
        return ToolResult(ok=True, text=observation.text, data={"works": observation.hits, "total": result.total})

    return ToolSpec(
        name=TOOL_NAME,
        handler=handler,
        description=("Search published research papers (OpenAlex, a catalogue of scholarly works). Returns, for each work found, "
                     "its title, authors, year, journal, DOI link, citation count and abstract. Optional year range and "
                     "open-access filter."),
        when_to_use=("The person asks what the scientific literature, research or studies say about something, or asks for papers "
                     "to cite. Search with a few plain keywords, then cite only works the result lists."),
        when_not_to_use=("News, prices, how-tos or anything the web covers (use web search), or the person's own quests. Never use it to "
                         "back a claim you already made from memory: if nothing relevant comes back, say that."),
        parameters={
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "The topic in a few plain keywords (not a sentence)."},
                "year_from": {"type": "integer", "description": "Earliest publication year (optional)."},
                "year_to": {"type": "integer", "description": "Latest publication year (optional)."},
                "open_access_only": {"type": "boolean", "description": "Only works with a free full text (optional)."},
                "most_cited": {"type": "boolean", "description": "Order by citation count instead of relevance (optional)."},
                "limit": {"type": "integer", "description": f"How many works, 1 to {MAX_WORKS} (default 5)."},
            },
        },
        mutates=False,
        origin="standard",
        workflow_step=False,
        keywords=("science", "scientific", "research", "papers", "studies", "literature", "journal", "citation", "doi", "evidence",
                  "peer-reviewed", "scholarly", "openalex"),
        timeout_seconds=30.0,
    )


def science_literature_unavailable(env: Mapping[str, str]) -> Optional[UnavailableTool]:
    return None if (env.get("OPENALEX_API_KEY") or "").strip() else UnavailableTool(
        name=TOOL_NAME, keywords=ASKING_ABOUT_PAPERS,
        note=("search_scientific_literature is not set up in this deployment, so you cannot search research papers. If the person "
              "asks for studies, papers or citations, say plainly that you cannot, and name no paper, author or DOI from memory."))


STANDARD_TOOL_BUILDERS[TOOL_NAME] = science_literature_spec
STANDARD_TOOL_UNAVAILABLE[TOOL_NAME] = science_literature_unavailable
