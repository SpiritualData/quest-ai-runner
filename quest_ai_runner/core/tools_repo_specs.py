"""Tools that let a run find and walk the ESOI specifications of the repository it works in.

Five read-only tools over ``repo_specs`` (offered only when an ``esoi.toml`` is found above the working folder, or at
``QAR_SPEC_ROOT``): ``find_specs`` (which records does this request concern), ``related_specs`` (what is connected to a
record, ``depth`` steps out), ``intent_specs`` (every record of an intent), ``show_spec`` (one record, tier 0 to 2) and
``search_cards_and_specs`` (cards and records in one ranking, ``unified_search``; selected nowhere, offered beside
``find_specs``). They load
only what the run asks for, so a run starts from the few records that apply and reaches the rest on demand. No model is called:
the run itself is the judge of the short list.

A worker calls them from its own shell, a new process that may start in another folder, so the deep brief's command passes the
root it found (``python -m quest_ai_runner.tools --spec-root <root> call ...``, see ``ToolRegistry.render_deep_block``).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional

from . import repo_specs
from .tools import STANDARD_TOOL_BUILDERS, ToolContext, ToolResult, ToolSpec
from esoi import spec_search as search_lib

# Where search_cards_and_specs gets cards: a callable returning card views (``adapters.card_views.card_views`` over the run's
# card repository), registered by the consumer that owns the store. None: the tool searches the records alone and says so.
SOURCE_LABEL = {search_lib.RECORD_SOURCE: "esoi"}   # what the tool has always called the records' source
CardViewsSource = Callable[[], List[search_lib.View]]
card_views_source: Optional[CardViewsSource] = None


def set_card_views_source(source: Optional[CardViewsSource]) -> None:
    global card_views_source
    card_views_source = source

KEYWORDS = ("spec", "specification", "esoi", "problem", "ideal", "solution", "metric", "intent", "records", "related")


def with_repo(build):
    return lambda env: build(env) if repo_specs.find_root() is not None else None


def listing(rows) -> str:
    return "\n".join(f"- {r['kind']} {r['id']}: {r['title']}" + (f"  (step {r['depth']} via {r['via']})" if "depth" in r else "")
                     for r in rows) or "(none)"


def find_specs_spec(env: Mapping[str, str]) -> ToolSpec:
    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        request = str(args.get("request") or "").strip()
        if not request:
            return ToolResult(ok=False, text="Needs request: the words of the task or question.")
        got, index = repo_specs.find(request)
        if not got.ids:
            return ToolResult(ok=True, data={"ids": []}, text="No specification of this repository concerns that request.")
        return ToolResult(ok=True, data={"ids": got.ids, "linked": got.linked, "route": got.route}, text=(
            "Specifications this request concerns (linked ones were brought in by a selected record):\n"
            + repo_specs.render(index, got.ids, got.linked)
            + "\nRead one in full with show_spec; walk outward with related_specs."))

    return ToolSpec(
        name="find_specs", handler=handler,
        description=("Find the few ESOI specification records (problems, ideals, solutions, metrics) of this repository that a "
                     "request or task concerns. Fast and free: lexical and local vector search with a confidence gate."),
        when_to_use=("At the start of a task in a repository that has ESOI records, to learn which problem, ideal, solution "
                     "and metric the work belongs to before changing anything."),
        when_not_to_use="Questions that are not about this repository's work; reading every record (use intent_specs).",
        parameters={"type": "object", "properties": {"request": {"type": "string", "description": "The task or question, in its own words."}},
                    "required": ["request"]},
        mutates=False, origin="standard", workflow_step=False, needs_spec_root=True, keywords=KEYWORDS, timeout_seconds=60.0)


def related_specs_spec(env: Mapping[str, str]) -> ToolSpec:
    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        record = str(args.get("id") or "").strip()
        root = repo_specs.find_root()
        try:
            rows = repo_specs.related_records(repo_specs.repo_index(root), record, int(args.get("depth") or 1), int(args.get("limit") or 30))
        except KeyError:
            return ToolResult(ok=False, text=f"No record {record!r}. Take an id from find_specs or intent_specs.")
        return ToolResult(ok=True, data=rows, text=f"Connected to {record}, nearest first:\n{listing(rows)}")

    return ToolSpec(
        name="related_specs", handler=handler,
        description=("The records connected to one specification record: a problem's solutions and ideals, a solution's metrics and "
                     "implementations. depth 1 is direct links, 2 their links. Read from the index: free."),
        when_to_use="After find_specs, to see what else belongs with a record (its metric, the problems it addresses, its alternatives).",
        when_not_to_use="Searching by words (use find_specs).",
        parameters={"type": "object", "properties": {
            "id": {"type": "string", "description": "A record id."},
            "depth": {"type": "integer", "description": "Link steps to follow (default 1)."},
            "limit": {"type": "integer", "description": "Most records to return (default 30)."}}, "required": ["id"]},
        mutates=False, origin="standard", workflow_step=False, needs_spec_root=True, keywords=KEYWORDS, timeout_seconds=30.0)


def intent_specs_spec(env: Mapping[str, str]) -> ToolSpec:
    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        rows = repo_specs.intent_records(repo_specs.repo_index(repo_specs.find_root()), str(args.get("intent_id") or "").strip())
        return ToolResult(ok=True, data=rows, text=f"{len(rows)} records:\n{listing(rows)}")

    return ToolSpec(
        name="intent_specs", handler=handler,
        description="List every specification record of one intent (blank: the whole repository), problems first, one line each.",
        when_to_use="When the work belongs to an intent but no single record is clear yet, or to see the whole picture once.",
        when_not_to_use="Finding the records for a request (find_specs is shorter).",
        parameters={"type": "object", "properties": {"intent_id": {"type": "string", "description": "The intent's id; blank for all."}}},
        mutates=False, origin="standard", workflow_step=False, needs_spec_root=True, keywords=KEYWORDS, timeout_seconds=30.0)


def show_spec_spec(env: Mapping[str, str]) -> ToolSpec:
    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        record = str(args.get("id") or "").strip()
        try:
            return ToolResult(ok=True, text=repo_specs.show(repo_specs.find_root(), record, int(args.get("tier") if args.get("tier") is not None else 2)))
        except KeyError:
            return ToolResult(ok=False, text=f"No record {record!r}.")

    return ToolSpec(
        name="show_spec", handler=handler,
        description="Read one specification record: tier 0 its line, 1 plus summary and links, 2 the full record (default).",
        when_to_use="For the records find_specs or related_specs showed that you will act on; read only those in full.",
        when_not_to_use="Reading records in bulk.",
        parameters={"type": "object", "properties": {"id": {"type": "string"}, "tier": {"type": "integer", "enum": [0, 1, 2]}}, "required": ["id"]},
        mutates=False, origin="standard", workflow_step=False, needs_spec_root=True, keywords=KEYWORDS, timeout_seconds=30.0)


def search_cards_and_specs_spec(env: Mapping[str, str]) -> ToolSpec:
    def handler(args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        request = str(args.get("request") or "").strip()
        if not request:
            return ToolResult(ok=False, text="Needs request: the words of the task or question.")
        raw_tags = args.get("tags") or []
        tags = [t.strip() for t in (raw_tags.replace(";", ",").split(",") if isinstance(raw_tags, str) else raw_tags) if str(t).strip()]
        mode = str(args.get("tag_mode") or "boost")
        if mode not in ("boost", "filter"):
            return ToolResult(ok=False, text="tag_mode is boost or filter.")
        try:
            cards = card_views_source() if card_views_source else []
        except Exception:  # noqa: BLE001  a failing card store leaves the records
            cards = []
        got = repo_specs.search(request, cards=cards, tags=tags or None, tag_mode=mode)
        scope = "" if card_views_source else "\n(No card store is registered here: records only.)"
        data = {"items": [{"kind": i.kind, "key": i.key, "source": SOURCE_LABEL.get(i.source, i.source), "linked_from": i.linked_from,
                           "no_solution": i.no_solution} for i in got.items],
                "no_solution_yet": got.no_solution_yet, "route": got.route, "calls": got.calls}
        if not got.items:
            return ToolResult(ok=True, data=data, text="No card or specification concerns that request." + scope)
        return ToolResult(ok=True, data=data, text=(
            "Cards and specifications this request concerns, best first (linked ones were brought in by a ranked record):\n"
            + search_lib.render(got) + "\nRead a record in full with show_spec." + scope))

    return ToolSpec(
        name="search_cards_and_specs", handler=handler,
        description=("One ranked list of the context cards and ESOI records (cards, solutions, problems; with the ideals, metrics, "
                     "intents and implementations they link) a request concerns, each line kind first. Says NO SOLUTION YET "
                     "when the best record is a problem or intent nothing solves. Free without a model."),
        when_to_use=("At the start of work in a repository with ESOI records, to see the cards and the records together, and "
                     "to learn whether a solution exists yet; optional tags (esoi/tags.csv ids or problem ids) boost or filter."),
        when_not_to_use="Records alone (find_specs); walking one record's links (related_specs).",
        parameters={"type": "object", "properties": {
            "request": {"type": "string", "description": "The task or question, in its own words."},
            "tags": {"type": "string", "description": "Optional tag ids or problem ids, comma separated (off by default)."},
            "tag_mode": {"type": "string", "enum": ["boost", "filter"], "description": "boost (default) or filter."}},
            "required": ["request"]},
        mutates=False, origin="standard", workflow_step=False, needs_spec_root=True, keywords=KEYWORDS + ("card", "cards", "context"),
        timeout_seconds=60.0)


REPO_SPEC_BUILDERS = {"find_specs": with_repo(find_specs_spec), "related_specs": with_repo(related_specs_spec),
                      "intent_specs": with_repo(intent_specs_spec), "show_spec": with_repo(show_spec_spec),
                      "search_cards_and_specs": with_repo(search_cards_and_specs_spec)}
STANDARD_TOOL_BUILDERS.update(REPO_SPEC_BUILDERS)
