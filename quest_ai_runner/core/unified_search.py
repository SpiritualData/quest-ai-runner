"""One ranking over context cards and ESOI records (``search-cards-and-specs``), beside today's two pipelines as its rival.

Today a run's cards come from the context assembler and its records from ``spec_selection``, two pipelines with their own
stages, budgets and output shapes. The ESOI library's ``esoi.spec_search`` ranks both in one list (views over cards and over
the specification index, one lexical search, an optional vector search, one fusion, the confidence gate, at most one model
call, links, "no solution yet"). This module is QAR's part only: the judge prompt and model call, the gate thresholds, the
ranked and linked kinds, and the rival arm.

No stage here is tuned for the mixed list: the gate thresholds are ``spec_selection``'s, tuned on records alone, and
the selection is ``metric-search-cards-and-specs``'s: ``two_pipelines`` (the two pipelines side by side, unchanged) stays
selected because the merged ranking leads the mixed cases but does not hold the spec_selection and context_selection halves.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence

from esoi import implementation, solution, spec_search as search_lib

from . import prompt_budget
from .prompt_specs import declare, render_prompt
from .spec_selection import GATE, VectorSearch
from hubofhubs.ports.model import STEP_SELECT, answer_with_reasoning

CARD_SOURCE = "cards"
CARD_KEY_PREFIX = "card:"   # a card view's key
RANKED_KINDS = ("card", "solution", "problem")
LINKED_KINDS = ("ideal", "metric", "intent", "implementation")   # also the order a ranked record brings them in
FIND_SPECS_EXPAND = 1   # the links each record brings in the find_specs tool (``repo_specs.find``)

SELECT_PROMPT = """You choose what a run needs in view for a request: context cards (notes, files, conversations a project \
keeps) and the project's specification records (a problem it exhibits, a solution it should use).

REQUEST: {task}

CANDIDATES (kind key: title. text):
{block}

Return the candidates the request concerns, most useful first, at most {limit}. Return an empty list only when the request is \
about something none of the candidates describe (a similar word is not enough). Use only the keys shown. Respond with ONLY \
valid JSON (no markdown): {{"ids": ["<key>", ...]}}"""


def model_judge(provider: Any, model: Optional[str]) -> Callable[[str, List[str], int], str]:
    """The library's ``judge`` for the mixed list: the QAR prompt over the candidate lines, one model call, the reply text."""
    def judge(task: str, lines: List[str], limit: int) -> str:
        prompt = render_prompt("search-cards-and-specs.pool-judge", task=task, limit=limit, block="\n".join(lines))
        return answer_with_reasoning(provider, [{"role": "user", "content": prompt}], model=model, reasoning="minimal", step=STEP_SELECT)
    return judge


@solution("search-cards-and-specs")
def search_cards_and_specs(task: str, views: Sequence[search_lib.View], *, provider: Any = None, model: Optional[str] = None,
                           vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                           max_ranked: int = 5, expand: int = 3, ranked_kinds: Sequence[str] = RANKED_KINDS,
                           tags: Optional[Sequence[str]] = None, tag_table: Optional[search_lib.TagTable] = None,
                           tag_mode: str = "boost") -> search_lib.Selection:
    """The cards and records ``task`` concerns, in one ranked list labelled by kind, each ranked record followed by the
    records it links; ``no_solution_yet`` set when the top record is a problem or intent no solution serves."""


@implementation("search-cards-and-specs", "solutions_and_problems_with_links")
def solutions_and_problems_with_links(task: str, views: Sequence[search_lib.View], *, provider: Any = None,
                                      model: Optional[str] = None, vector_search: Optional[VectorSearch] = None,
                                      k_lexical: int = 15, k_vector: int = 15, max_ranked: int = 5, expand: int = 3,
                                      ranked_kinds: Sequence[str] = RANKED_KINDS, tags: Optional[Sequence[str]] = None,
                                      tag_table: Optional[search_lib.TagTable] = None, tag_mode: str = "boost") -> search_lib.Selection:
    """Cards, solutions and problems ranked together by the library's stages (``esoi.spec_search.search``, gated); ideals,
    metrics, intents and implementations reached through links. ``max_ranked`` is the one item budget across sources. ``tags``
    (off by default) boosts the views that carry any of them, or with ``tag_mode="filter"`` keeps only those."""
    return search_lib.search(prompt_budget.decision_excerpt(task), views,
                            judge=model_judge(provider, model) if provider is not None else None, vector_search=vector_search,
                            strategy="gated", k_lexical=k_lexical, k_vector=k_vector, max_ranked=max_ranked, expand=expand,
                            ranked_kinds=ranked_kinds, link_kinds=LINKED_KINDS, tags=tags, tag_table=tag_table,
                            tag_mode=tag_mode, gate=GATE, line_text=None)


@implementation("search-cards-and-specs", "two_pipelines")
def two_pipelines(task: str, views: Sequence[search_lib.View], *, provider: Any = None, model: Optional[str] = None,
                  vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15, max_ranked: int = 5,
                  expand: int = 3, ranked_kinds: Sequence[str] = RANKED_KINDS, tags: Optional[Sequence[str]] = None,
                  tag_table: Optional[search_lib.TagTable] = None, tag_mode: str = "boost",
                  select_cards: Optional[Callable[[str], Sequence[str]]] = None) -> search_lib.Selection:
    """Today's two pipelines side by side, neither changed. Cards: the ids ``select_cards(task)`` returns, in its order (the
    run's own select-context assembler, injected because core imports no adapter, for example
    ``lambda t: assembler.assemble(t).card_ids``; none when it is not given). Records: what the find_specs tool returns, the
    library's ``lexical-vector-gated`` over the record views with one link each; ``vector_search`` is over the record
    keys. Cards first, then records. Neither pipeline knows "no solution yet", reads tags or shares a budget, so
    ``max_ranked``, ``expand``, ``ranked_kinds`` and the tag arguments are not used."""
    card_ids: List[str] = []
    for raw in (select_cards(task) if select_cards else ()):
        cid = raw[len(CARD_KEY_PREFIX):] if raw.startswith(CARD_KEY_PREFIX) else raw
        if cid not in card_ids:
            card_ids.append(cid)
    by_key = {v.key: v for v in views}
    records = [v for v in views if v.source == search_lib.RECORD_SOURCE]
    specs = search_lib.gated(prompt_budget.decision_excerpt(task), records,
                             judge=model_judge(provider, model) if provider is not None else None, vector_search=vector_search,
                             k_lexical=k_lexical, k_vector=k_vector, expand=FIND_SPECS_EXPAND,
                             ranked_kinds=search_lib.RECORD_CANDIDATE_KINDS, gate=GATE, line_text=None)
    result = search_lib.Selection(candidates=list(specs.candidates), sources=dict(specs.sources), calls=specs.calls,
                                  failed_batches=specs.failed_batches, route=f"cards {len(card_ids)}, records {specs.route}")
    for cid in card_ids:
        view = by_key.get(CARD_KEY_PREFIX + cid)
        result.items.append(search_lib.Found(kind="card", id=cid, source=view.source if view else CARD_SOURCE,
                                             title=view.title if view else cid, key=CARD_KEY_PREFIX + cid))
    for found in specs.items:
        result.items.append(search_lib.Found(kind=found.kind, id=found.id, source=found.source, title=found.title, key=found.key,
                                             linked_from=found.linked_from))
    return result


SELECT_SPEC = declare(
    "search-cards-and-specs.pool-judge", solution="search-cards-and-specs",
    metric="metric-search-cards-and-specs", tier="caller", cache_layer="none",
    template=SELECT_PROMPT)
