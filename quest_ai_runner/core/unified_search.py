"""One ranking over context cards and ESOI records (``search-cards-and-specs``), beside today's two pipelines as its rival.

Today a run's cards come from the context assembler and its records from ``spec_selection``, two pipelines with their own
stages, budgets and output shapes. This module ranks both in one list. It is a composition, not a third pipeline: every stage
is the one ``spec_selection`` already has, applied to a list of views instead of an index's entries.

  * VIEW. ``RecordView`` is a read-only view of one item: a card (``views_from_cards``) or one row of ``esoi/*.csv``
    (``views_from_spec_index``, over the index ``esoi spec index`` builds). A view holds a reference to the item's own fields
    and weighted terms; nothing is copied into a card store or written anywhere (the mount principle: a copy is what
    inflated the stores).
  * RANK. Cards, solutions and problems (``RANKED_KINDS``) compete in one field-weighted TF-IDF (``narrow_lexical_scored``),
    an optional injected vector search over the same view keys (``semantic_scored``), one reciprocal-rank fusion (``fuse``),
    the same confidence gate (``gate_decision``) and at most one cheap model call over the pool (``parse_ids``).
  * LINKS. Ideals, metrics, intents and implementations are not ranked: each ranked record brings up to ``expand`` of them,
    marked ``linked`` (``attach_links``), one of each kind before a second of any.
  * NO SOLUTION YET. When the top record is a problem or an intent that no solution serves, the result says so, which is the
    run's cue to define one.
  * TAGS (optional, off by default). ``esoi/tags.csv`` plus each solution's connected problems, as a filter or a boost.

No stage here is tuned for the mixed list: the gate thresholds are ``spec_selection``'s, tuned on records alone, and
the selection is ``metric-search-cards-and-specs``'s: ``two_pipelines`` (the two pipelines side by side, unchanged) stays
selected because the merged ranking leads the mixed cases but does not hold the spec_selection and context_selection halves.
Standard library only apart from the model call.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from esoi import implementation, solution

from . import prompt_budget
from .prompt_specs import declare, render_prompt
from .adapters import STEP_SELECT, answer_with_reasoning
from .spec_selection import (GATE_POOL, SpecSelection, VectorSearch, attach_links, fuse, gate_decision, lexical_vector_gated,
                             narrow_lexical_scored, parse_ids, semantic_scored, tokens)

log = logging.getLogger("quest-ai-runner.unified-search")

CARD_SOURCE = "cards"
RECORD_SOURCE = "esoi"
CARD_KEY_PREFIX = "card:"   # a card view's key (``RecordView.key``)
RANKED_KINDS = ("card", "solution", "problem")
LINKED_KINDS = ("ideal", "metric", "intent", "implementation")   # also the order a ranked record brings them in
NO_SOLUTION_KINDS = ("problem", "intent")
TAG_MODES = ("boost", "filter")
TITLE_WEIGHT, KEYWORD_WEIGHT, TEXT_WEIGHT = 3.0, 2.0, 1.0   # the weights the spec index gives a title, a short field, a body
LINE_TEXT = 160   # characters of a view's text shown to the model beside its title

SELECT_PROMPT = """You choose what a run needs in view for a request: context cards (notes, files, conversations a project \
keeps) and the project's specification records (a problem it exhibits, a solution it should use).

REQUEST: {task}

CANDIDATES (kind key: title. text):
{block}

Return the candidates the request concerns, most useful first, at most {limit}. Return an empty list only when the request is \
about something none of the candidates describe (a similar word is not enough). Use only the keys shown. Respond with ONLY \
valid JSON (no markdown): {{"ids": ["<key>", ...]}}"""


@dataclass
class RecordView:
    """One searchable item, read in place. ``links`` are ids within the same source; ``tags`` are tag ids and problem ids."""
    kind: str
    id: str
    title: str
    text: str = ""
    source: str = RECORD_SOURCE
    links: Tuple[str, ...] = ()
    tags: Tuple[str, ...] = ()
    terms: Mapping[str, float] = field(default_factory=dict)
    intent: str = ""

    @property
    def key(self) -> str:
        """Unique across sources: a record's own id (so a vector search over the spec index needs no mapping), and
        ``card:<id>`` for a card, so a card can never shadow a record."""
        return self.id if self.source == RECORD_SOURCE else f"{self.kind}:{self.id}"


@dataclass
class TagTable:
    """``esoi/tags.csv`` (tag -> the problems it mitigates) and each solution's ``tags`` column, read as data."""
    tag_problems: Dict[str, Tuple[str, ...]] = field(default_factory=dict)
    solution_tags: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def expand(self, wanted: Iterable[str]) -> set:
        """A requested tag stands for itself and for the problems it mitigates; a problem id stands for itself."""
        out = set()
        for tag in wanted:
            out.add(tag)
            out.update(self.tag_problems.get(tag, ()))
        return out


@dataclass
class SearchItem:
    kind: str
    id: str
    source: str
    title: str
    key: str = ""
    linked_from: str = ""      # the key of the ranked item that brought this one; empty for a ranked item
    no_solution: bool = False  # a problem or intent that no solution serves


@dataclass
class UnifiedSelection:
    """Ranked items best first, each followed by the records it brought; ``no_solution_yet`` is the key of the top record when
    it is a problem or intent that no solution serves (empty otherwise)."""
    items: List[SearchItem] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)
    sources: Dict[str, List[str]] = field(default_factory=dict)
    calls: int = 0
    failed_batches: int = 0
    route: str = ""
    no_solution_yet: str = ""


def weighted_terms(title: str, text: str = "", keywords: Sequence[str] = ()) -> Dict[str, float]:
    """Field-weighted terms for an item the index did not build (a card), folded exactly as the index folds a record."""
    out: Dict[str, float] = {}
    for weight, words in ((TITLE_WEIGHT, title), (KEYWORD_WEIGHT, " ".join(map(str, keywords))), (TEXT_WEIGHT, text)):
        for term in tokens(words):
            out[term] = max(out.get(term, 0.0), weight)
    return out


def views_from_cards(cards: Mapping[str, Mapping[str, Any]], *, text_of=None, source: str = CARD_SOURCE) -> List[RecordView]:
    """A view per card dict (``{id: card}``, as ``CardRepository.load_all`` returns). ``text_of(card)`` is the card's search
    text (the adapters pass ``card_embed_text``, the one definition); without it, name and summary are used."""
    views = []
    for card_id, card in cards.items():
        if not isinstance(card, Mapping):
            continue
        title = str(card.get("name") or card_id)
        text = text_of(card) if text_of else str(card.get("description") or card.get("summary") or "")
        keywords = [k for k in (card.get("keywords") or []) if isinstance(k, str)]
        tags = tuple(t for t in (card.get("tags") or []) if isinstance(t, str))
        views.append(RecordView(kind="card", id=str(card_id), title=title, text=text, source=source, tags=tags,
                                terms=weighted_terms(title, text, keywords)))
    return views


def views_from_spec_index(index: Mapping[str, Any], tag_table: Optional[TagTable] = None) -> List[RecordView]:
    """A view per record of a loaded spec index, every kind (ranked and linked-only). The view points at the entry's own
    title, summary, refs and terms. Tags: a solution carries its ``tags`` column plus its linked problems; a problem carries
    its own id plus every listed tag that mitigates it."""
    entries = index["entries"]
    kind_of = {e["id"]: e["kind"] for e in entries}
    mitigated_by: Dict[str, List[str]] = {}
    for tag, problems in (tag_table.tag_problems.items() if tag_table else ()):
        for pid in problems:
            mitigated_by.setdefault(pid, []).append(tag)
    views = []
    for e in entries:
        links = tuple(e.get("refs") or ())
        if e["kind"] == "solution":
            tags = (tag_table.solution_tags.get(e["id"], ()) if tag_table else ()) + tuple(r for r in links if kind_of.get(r) == "problem")
        elif e["kind"] == "problem":
            tags = (e["id"], *mitigated_by.get(e["id"], ()))
        else:
            tags = ()
        views.append(RecordView(kind=e["kind"], id=e["id"], title=e.get("title", ""), text=e.get("summary", ""),
                                source=RECORD_SOURCE, links=links, tags=tuple(dict.fromkeys(tags)), terms=e.get("terms") or {},
                                intent=e.get("intent", "")))
    return views


def served_by_solution(views: Sequence[RecordView]) -> set:
    """Keys of the problems and intents some solution serves. A problem is served when it links a solution or a solution
    links it (the index stores links both ways; either direction counts). An intent is served when a solution belongs to it
    (its ``intent`` field) or it links a solution. Everything not in this set is "no solution yet"."""
    by_key = {v.key: v for v in views}
    served = set()
    for v in views:
        if v.kind == "solution":
            served.update(r for r in v.links if r in by_key and by_key[r].kind in NO_SOLUTION_KINDS)
            if v.intent:
                served.add(v.intent)
        elif v.kind in NO_SOLUTION_KINDS and any(r in by_key and by_key[r].kind == "solution" for r in v.links):
            served.add(v.key)
    return served


def link_map(views: Sequence[RecordView]) -> Dict[str, Dict[str, List[str]]]:
    """For ``attach_links``: each view's links restricted to the linked-only kinds, ordered one of each kind (ideal, metric,
    intent, implementation) before the second of any, the record's own intent counted as a link."""
    by_key = {v.key: v for v in views}
    out = {}
    for v in views:
        refs = list(v.links) + ([v.intent] if v.intent and v.intent != v.key else [])
        per_kind = {k: [r for r in dict.fromkeys(refs) if r in by_key and by_key[r].kind == k] for k in LINKED_KINDS}
        ordered = []
        for step in range(max((len(x) for x in per_kind.values()), default=0)):
            ordered += [per_kind[k][step] for k in LINKED_KINDS if step < len(per_kind[k])]
        out[v.key] = {"refs": ordered}
    return out


def view_line(view: RecordView) -> str:
    text = " ".join((view.text or "").split())
    return f"{view.kind} {view.key}: {view.title}" + (f". {text[:LINE_TEXT]}" if text and text != view.title else "")


def judge(task: str, pool: List[str], by_key: Mapping[str, RecordView], limit: int, provider: Any,
          model: Optional[str]) -> Optional[List[str]]:
    """One model call over the pool; None when it fails, so the caller keeps the best fused candidates."""
    prompt = render_prompt("search-cards-and-specs.pool-judge", task=task, limit=limit, block="\n".join(view_line(by_key[k]) for k in pool))
    try:
        reply = answer_with_reasoning(provider, [{"role": "user", "content": prompt}], model=model, reasoning="minimal",
                                      step=STEP_SELECT)
    except Exception:  # noqa: BLE001  a failed call must never lose the candidates
        log.debug("unified judge failed", exc_info=True)
        return None
    return parse_ids(reply, set(pool))


@solution("search-cards-and-specs")
def search_cards_and_specs(task: str, views: Sequence[RecordView], *, provider: Any = None, model: Optional[str] = None,
                           vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                           max_ranked: int = 5, expand: int = 3, ranked_kinds: Sequence[str] = RANKED_KINDS,
                           tags: Optional[Sequence[str]] = None, tag_table: Optional[TagTable] = None,
                           tag_mode: str = "boost") -> UnifiedSelection:
    """The cards and records ``task`` concerns, in one ranked list labelled by kind, each ranked record followed by the
    records it links; ``no_solution_yet`` set when the top record is a problem or intent no solution serves."""


@implementation("search-cards-and-specs", "solutions_and_problems_with_links")
def solutions_and_problems_with_links(task: str, views: Sequence[RecordView], *, provider: Any = None,
                                      model: Optional[str] = None, vector_search: Optional[VectorSearch] = None,
                                      k_lexical: int = 15, k_vector: int = 15, max_ranked: int = 5, expand: int = 3,
                                      ranked_kinds: Sequence[str] = RANKED_KINDS, tags: Optional[Sequence[str]] = None,
                                      tag_table: Optional[TagTable] = None, tag_mode: str = "boost") -> UnifiedSelection:
    """Cards, solutions and problems ranked together by the spec-selection stages (lexical, optional vector, fusion, gate, at
    most one model call); ideals, metrics, intents and implementations reached through links. ``max_ranked`` is the one item
    budget across sources. ``tags`` (off by default) boosts the views that carry any of them, or with ``tag_mode="filter"``
    keeps only those."""
    if tag_mode not in TAG_MODES:
        raise ValueError(f"tag_mode is one of {TAG_MODES}")
    task = prompt_budget.decision_excerpt(task)
    by_key = {v.key: v for v in views}
    df: Counter = Counter()
    for v in views:
        df.update(v.terms.keys())
    index = {"n": len(views), "df": df,
             "entries": [{"id": v.key, "kind": v.kind, "terms": v.terms} for v in views]}   # references, not copies
    kinds_by_key = {v.key: {"kind": v.kind} for v in views}
    lexical = narrow_lexical_scored(index, task, k_lexical, ranked_kinds)
    semantic = semantic_scored(vector_search, task, k_vector, kinds_by_key, ranked_kinds)
    rankings = [r for r in ([i for i, _ in lexical], [i for i, _ in semantic]) if r]
    candidates = fuse(rankings, k_lexical + k_vector)
    if tags:
        wanted = (tag_table or TagTable()).expand(tags)
        tagged = [k for k in candidates if wanted & set(by_key[k].tags)]
        candidates = tagged if tag_mode == "filter" else fuse(rankings + ([tagged] if tagged else []), k_lexical + k_vector)
    result = UnifiedSelection(candidates=candidates,
                              sources={k: [n for n, r in (("lexical", lexical), ("vector", semantic)) if any(k == x for x, _ in r)]
                                       for k in candidates})
    route = gate_decision(lexical, semantic) if candidates else "nothing"
    if route == "nothing":
        result.route = "nothing"
        return result
    if route == "sure":
        agreed = {i for i, _ in lexical[:3]} & {i for i, _ in semantic[:3]}
        chosen = [k for k in candidates if k in agreed]
    elif provider is None:
        route, chosen = "unjudged", candidates[:max_ranked]
    else:
        route, result.calls = "judged", 1
        pool = candidates[:GATE_POOL]
        verdict = judge(task, pool, by_key, max_ranked, provider, model)
        if verdict is None:
            result.failed_batches = 1
            verdict = pool[:max_ranked]
        chosen = [k for k in pool if k in set(verdict)]
    result.route = route
    return finish(result, views, by_key, chosen[:max_ranked], expand)


FIND_SPECS_EXPAND = 1   # the links each record brings in the find_specs tool (``repo_specs.find``)


@implementation("search-cards-and-specs", "two_pipelines")
def two_pipelines(task: str, views: Sequence[RecordView], *, provider: Any = None, model: Optional[str] = None,
                  vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15, max_ranked: int = 5,
                  expand: int = 3, ranked_kinds: Sequence[str] = RANKED_KINDS, tags: Optional[Sequence[str]] = None,
                  tag_table: Optional[TagTable] = None, tag_mode: str = "boost",
                  select_cards: Optional[Callable[[str], Sequence[str]]] = None) -> UnifiedSelection:
    """Today's two pipelines side by side, neither changed. Cards: the ids ``select_cards(task)`` returns, in its order (the
    run's own select-context assembler, injected because core imports no adapter, for example
    ``lambda t: assembler.assemble(t).card_ids``; none when it is not given). Records: what the find_specs tool returns,
    spec-selection's ``lexical_vector_gated`` over the record views with one link each; ``vector_search`` is over the record
    keys. Cards first, then records. Neither pipeline knows "no solution yet", reads tags or shares a budget, so
    ``max_ranked``, ``expand``, ``ranked_kinds`` and the tag arguments are not used."""
    card_ids = []
    for raw in (select_cards(task) if select_cards else ()):
        cid = raw[len(CARD_KEY_PREFIX):] if raw.startswith(CARD_KEY_PREFIX) else raw
        if cid not in card_ids:
            card_ids.append(cid)
    records = [v for v in views if v.source == RECORD_SOURCE]
    by_key = {v.key: v for v in views}
    df: Counter = Counter()
    for v in records:
        df.update(v.terms.keys())
    index = {"n": len(records), "df": df,
             "entries": [{"id": v.key, "kind": v.kind, "title": v.title, "summary": v.text, "terms": v.terms,
                          "refs": list(v.links)} for v in records]}
    specs = lexical_vector_gated(task, index, provider=provider, model=model, vector_search=vector_search,
                                 k_lexical=k_lexical, k_vector=k_vector, expand=FIND_SPECS_EXPAND)
    result = UnifiedSelection(candidates=list(specs.candidates), sources=dict(specs.sources), calls=specs.calls,
                              failed_batches=specs.failed_batches, route=f"cards {len(card_ids)}, records {specs.route}")
    for cid in card_ids:
        view = by_key.get(CARD_KEY_PREFIX + cid)
        result.items.append(SearchItem(kind="card", id=cid, source=view.source if view else CARD_SOURCE,
                                       title=view.title if view else cid, key=CARD_KEY_PREFIX + cid))
    for key in specs.ids:
        v = by_key[key]
        result.items.append(SearchItem(kind=v.kind, id=v.id, source=v.source, title=v.title, key=key,
                                       linked_from=specs.linked.get(key, "")))
    return result


def finish(result: UnifiedSelection, views: Sequence[RecordView], by_key: Mapping[str, RecordView], chosen: List[str],
           expand: int) -> UnifiedSelection:
    """Links (``attach_links``), kinds and the "no solution yet" mark."""
    linked = attach_links(SpecSelection(), link_map(views), chosen, expand)
    served = served_by_solution(views)
    for key in linked.ids:
        v = by_key[key]
        result.items.append(SearchItem(kind=v.kind, id=v.id, source=v.source, title=v.title, key=key,
                                       linked_from=linked.linked.get(key, ""),
                                       no_solution=v.kind in NO_SOLUTION_KINDS and key not in served))
    top_record = next((i for i in result.items if not i.linked_from and i.source == RECORD_SOURCE), None)
    if top_record is not None and top_record.no_solution:
        result.no_solution_yet = top_record.key
    return result


def render(result: UnifiedSelection, budget: int = 4000) -> str:
    """One line per item, kind first; linked items say what brought them. The "no solution yet" line leads when set. Lines
    past ``budget`` characters are counted, not shown."""
    lines = []
    if result.no_solution_yet:
        lines.append(f"NO SOLUTION YET for {result.no_solution_yet}: define one (a solution record that addresses it), "
                     "then record what you did on the record.")
    for item in result.items:
        line = f"{item.kind} {item.key}: {item.title}"
        if item.linked_from:
            line += f"  (linked from {item.linked_from})"
        if item.no_solution:
            line += "  [no solution yet]"
        lines.append(line)
    out, used = [], 0
    for n, line in enumerate(lines):
        if used + len(line) + 1 > budget and out:
            out.append(f"({len(lines) - n} more not shown)")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


SELECT_SPEC = declare(
    "search-cards-and-specs.pool-judge", solution="search-cards-and-specs",
    metric="metric-search-cards-and-specs", tier="caller", cache_layer="none",
    template=SELECT_PROMPT)
