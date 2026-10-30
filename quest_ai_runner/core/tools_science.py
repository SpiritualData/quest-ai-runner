"""The standard tool ``search_scientific_literature``: published research papers, found through the hub's OpenAlex arm
(``hubofhubs.science.openalex``, an implementation of the ``know-the-world`` seat; extra ``science``).

Offered only when ``OPENALEX_API_KEY`` is set (OpenAlex gives a free key; the hub arm's docs say what it costs and how to get
one). Without it the tool is not in the catalog and the planner is told, on a request about papers, that it cannot search the
literature (``STANDARD_TOOL_UNAVAILABLE``), so the AI keeps saying plainly that it cannot instead of citing from memory.

The result lists each work with its title, authors, year, journal, DOI link and abstract and states that only these may be cited;
a work with no DOI says so. Nothing here is a model call. The hub reads no environment, so this module reads the names below and
hands them over: ``OPENALEX_API_KEY``, ``OPENALEX_MAILTO`` (a contact address for OpenAlex, optional) and
``QAR_SCIENCE_DAILY_LIMIT`` (real searches per UTC day for this process, default 300).

The registry is rebuilt more than once in a process (the poller resolves it for each job), so the day's count and the answer
cache live in module state (``shared_limiter``, ``shared_arm``) and survive the rebuild; a restart of the process starts the
count again, which is why the cap is documented per process.

Every work's title and abstract is third-party text; the hub's result says so (``UNTRUSTED_RULE``) and that only abstracts were read.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, Mapping, Optional, Tuple

from .tools import STANDARD_TOOL_BUILDERS, STANDARD_TOOL_UNAVAILABLE, ToolContext, ToolResult, ToolSpec, UnavailableTool

TOOL_NAME = "search_scientific_literature"
DEFAULT_DAILY_LIMIT = 300
MAX_WORKS = 10
#: What makes a request one about the research literature, matched on the PERSON's message (never on model output, hard rule
#: #3). Specific phrases only: a single common word ("paper", "study", "journal", "peer", "cite") fired on "study for my exam",
#: "journal entry", "paper towels" and "peer review my essay", and the note costs tokens on every request it fires on.
ASKING_ABOUT_PAPERS = (
    r"\b(scientific|scholarly|academic|research|journal|clinical|published|peer[- ]reviewed)\s+(literature|papers?|articles?|studies|evidence|research|trials?|sources?)\b",
    r"\bpeer[- ]reviewed\b",
    r"\b(literature|systematic)\s+(review|search)\b",
    r"\b(meta[- ]analys[ie]s|pubmed|arxiv|google scholar|semantic scholar|openalex|crossref)\b",
    r"\bdoi\b|\b10\.\d{4,9}/",
    r"\b(studies|research|science)\s+(say|says|show|shows|suggest|suggests|find|finds|found|indicate|indicates)\b",
    r"\bcite\b.{0,30}\b(papers?|studies|research|articles?|literature)\b",
)

SHARED_LOCK = threading.Lock()
SHARED_LIMITERS: Dict[Tuple[str, int], Any] = {}
SHARED_ARMS: Dict[Tuple[str, str, int], Any] = {}


def shared_limiter(key: str, limit: int):
    """The one daily counter for this key and limit in this process, kept across registry rebuilds."""
    from hubofhubs.websearch.limiter import DailyLimiter

    with SHARED_LOCK:
        if (key, limit) not in SHARED_LIMITERS:
            SHARED_LIMITERS[(key, limit)] = DailyLimiter(limit)
        return SHARED_LIMITERS[(key, limit)]


def shared_arm(key: str, mailto: str, limit: int):
    """The one OpenAlex arm (so one cache and one counter) per key, contact and limit in this process."""
    from hubofhubs.science.openalex import OpenAlexWorks

    limiter = shared_limiter(key, limit)
    with SHARED_LOCK:
        if (key, mailto, limit) not in SHARED_ARMS:
            SHARED_ARMS[(key, mailto, limit)] = OpenAlexWorks(api_key=key, mailto=mailto, abstract_chars=400, limiter=limiter)
        return SHARED_ARMS[(key, mailto, limit)]


def reset_shared_state() -> None:
    """Forget the shared counters and arms (tests)."""
    with SHARED_LOCK:
        SHARED_LIMITERS.clear()
        SHARED_ARMS.clear()


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

    mailto, limit = (env.get("OPENALEX_MAILTO") or "").strip(), daily_limit_from(env)
    if transport is None:
        arm = shared_arm(key, mailto, limit)
    else:   # a test's fake transport: its own arm, but the same day's counter as any rebuilt registry
        arm = OpenAlexWorks(api_key=key, mailto=mailto, transport=transport, abstract_chars=400, limiter=shared_limiter(key, limit))

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
        name=TOOL_NAME, patterns=ASKING_ABOUT_PAPERS,
        note=("search_scientific_literature is not set up here, so you cannot search research papers. If asked for studies or "
              "citations, say so and name no paper, author or DOI from memory."))


STANDARD_TOOL_BUILDERS[TOOL_NAME] = science_literature_spec
STANDARD_TOOL_UNAVAILABLE[TOOL_NAME] = science_literature_unavailable
