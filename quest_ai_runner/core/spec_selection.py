"""Find the intent specifications a new use case or input concerns, from an ESOI specification index.

The index (``esoi spec index``, plain JSON) is the data model: one entry per record with a stable id, a one-line title,
a short summary, links, weighted terms and document frequencies. This module is the AI system on top of it. The pipeline:

  1. LEXICAL narrowing: field-weighted TF-IDF over the index's own terms and document frequencies (no corpus pass).
  2. SEMANTIC widening (optional): ``vector_search(text, k) -> [entry id]`` is injected by the consumer (a vector store
     over the same entries), because words in a request rarely match a record's words. Lists are fused by reciprocal rank.
  3. FINAL selection: the candidates' tier-1 lines are cut into batches and a cheap model judges every batch in parallel
     (one call each), so latency is one call, not one per candidate. An id the model returns that was not offered is
     dropped; a batch whose call or parse fails keeps its candidates in fused order rather than losing them.
  4. LINKS: each selected record brings up to ``expand`` linked records, marked as such.
  5. Reading in full (tier 2) is the caller's, and only of what this returns.

The gated implementation (the selected one) spends the model only where the cheap stages cannot decide: when lexical and
vector search agree on the top record with strong evidence it answers without a call, when neither finds anything credible it
returns nothing without a call, and otherwise one call judges the best candidates together. ``related_specs`` and
``intent_specs`` let a reader walk from a chosen record to its connected records or list a whole intent, on demand.

With no provider the fused candidates are the answer (graceful: never worse than lexical). The index is read as data,
so this module needs no import of the library that wrote it. Standard library only apart from the model call.
"""
from __future__ import annotations

import json
import logging
import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from esoi import implementation, solution

from . import prompt_budget
from .prompt_specs import declare, render_prompt
from .adapters import STEP_SELECT, answer_with_reasoning

log = logging.getLogger("quest-ai-runner.spec-selection")

SUPPORTED_INDEX_VERSION = 1
RRF_OFFSET = 60            # reciprocal rank fusion: score = sum 1 / (RRF_OFFSET + rank)
TOKEN = re.compile(r"[a-z0-9]+")
STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the their this to was were which "
                 "with without not no but can may use used uses when what who why how into than then them they there so "
                 "such each every any all one more most only also other another own same too very if over under up out "
                 "about after before between both do does done get gets via per".split())

VectorSearch = Callable[[str, int], List[Any]]   # (request text, k) -> entry ids best first, or (id, cosine) pairs
# Records that state intent are candidates; implementations are reached through their solution's links (there are several per
# solution and they would crowd the candidates).
CANDIDATE_KINDS = ("intent", "problem", "ideal", "solution", "metric")
# The gate (documented in docs/spec-selection.md): tuned on the dev split of the evaluation cases only.
GATE_SURE_VECTOR = 0.72      # top cosine at or above this, or ...
GATE_SURE_LEXICAL = 60.0     # ... top lexical score at or above this, with both searches agreeing on the first record
GATE_NOTHING_VECTOR = 0.52   # no credible match: top cosine below this ...
GATE_NOTHING_LEXICAL = 12.0  # ... and top lexical score below this
GATE_AGREE_RANK = 3          # a record also in the top few of both searches joins the sure answer
GATE_POOL = 20               # candidates the single judging call sees
UNJUDGED_TOP = 3             # what an ambiguous request returns when there is no model to judge it

SELECT_PROMPT = """You choose which project specifications a new request concerns.

REQUEST: {task}

CANDIDATES (kind id: title. summary):
{block}

Return the candidates that the request concerns: a problem it exhibits, an ideal that decides what "better" means for it, \
a solution or metric it should use. Prefer the most specific records and at most four. Return an empty list only when the \
request is about something none of the candidates describe (a similar word is not enough). Use only the ids shown. Respond \
with ONLY valid JSON (no markdown): {{"ids": ["<id>", ...]}}"""


@dataclass
class SpecSelection:
    """What was chosen and how: ``ids`` best first, then linked ids; ``candidates`` is everything the model was shown."""
    ids: List[str] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)
    linked: Dict[str, str] = field(default_factory=dict)     # linked id -> the selected id it came in through
    calls: int = 0
    failed_batches: int = 0
    sources: Dict[str, List[str]] = field(default_factory=dict)   # id -> ["lexical", "vector"] that proposed it
    route: str = ""   # how it was decided: sure / nothing (no model), judged (model), unjudged (no provider), lexical (baseline)


def load_spec_index(source: Union[str, Path, Dict[str, Any]]) -> Dict[str, Any]:
    """The index from a file path or an already parsed dict; refuses a version this module does not understand."""
    index = source if isinstance(source, dict) else json.loads(Path(source).read_text(encoding="utf-8"))
    if index.get("version") != SUPPORTED_INDEX_VERSION or "entries" not in index:
        raise ValueError(f"not a version {SUPPORTED_INDEX_VERSION} specification index (esoi spec index writes one)")
    return index


def tokens(text: str) -> List[str]:
    """The same folding the index used when it was built (lowercase, no stop words, light plural fold)."""
    out = []
    for word in TOKEN.findall((text or "").lower()):
        if word in STOP or (len(word) < 3 and not word.isdigit()):
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return out


def narrow_lexical_scored(index: Dict[str, Any], text: str, k: int, kinds: Optional[Sequence[str]] = None) -> List[Tuple[str, float]]:
    """(id, score) of the ``k`` entries of the wanted kinds with the best field-weighted TF-IDF against the request."""
    query = set(tokens(text))
    n, df = index["n"], index["df"]
    scored = []
    for entry in index["entries"]:
        if kinds is not None and entry["kind"] not in kinds:
            continue
        matched = [t for t in query if t in entry["terms"]]
        if matched:
            score = sum(entry["terms"][t] * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0) for t in matched)
            scored.append((-score, entry["id"]))
    return [(i, -neg) for neg, i in sorted(scored)[:k]]


def narrow_lexical(index: Dict[str, Any], text: str, k: int, kinds: Optional[Sequence[str]] = None) -> List[str]:
    """Ids of the ``k`` entries with the best field-weighted TF-IDF against the request, best first; none for no match."""
    return [i for i, _ in narrow_lexical_scored(index, text, k, kinds)]


def semantic_scored(vector_search: Optional[VectorSearch], text: str, k: int, by_id: Dict[str, Any],
                    kinds: Optional[Sequence[str]] = None) -> List[Tuple[str, float]]:
    """(id, cosine) from the injected vector search, restricted to ids in the index and the wanted kinds; a search that
    returns bare ids gets the score 0.0 (so the gate cannot treat it as strong evidence). A failing search is empty."""
    if vector_search is None:
        return []
    try:
        raw = vector_search(text, k * 3 if kinds is not None else k)
    except Exception:  # noqa: BLE001  semantic widening is optional; lexical still answers
        log.debug("spec vector search failed", exc_info=True)
        return []
    out = []
    for item in raw:
        entry_id, score = (item if isinstance(item, (tuple, list)) else (item, 0.0))
        if entry_id in by_id and (kinds is None or by_id[entry_id]["kind"] in kinds):
            out.append((entry_id, float(score)))
    return out[:k]


def fuse(rankings: List[List[str]], k: int) -> List[str]:
    """Reciprocal rank fusion: an id high in several lists beats one high in a single list."""
    score: Counter = Counter()
    for ranking in rankings:
        for rank, entry_id in enumerate(ranking):
            score[entry_id] += 1.0 / (RRF_OFFSET + rank + 1)
    return [i for i, _ in sorted(score.items(), key=lambda kv: (-kv[1], kv[0]))[:k]]


def parse_ids(text: str, offered: set) -> Optional[List[str]]:
    """The offered ids a model answered with, in its order, or None when the answer is not the requested JSON."""
    match = re.search(r"\{.*\}", text or "", re.S)
    try:
        ids = json.loads(match.group(0))["ids"] if match else None
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(ids, list):
        return None
    seen: List[str] = []
    for i in ids:
        if isinstance(i, str) and i in offered and i not in seen:
            seen.append(i)
    return seen


def judge_batch(task: str, lines: List[str], offered: set, provider: Any, model: Optional[str]) -> Optional[List[str]]:
    """One model call over one batch; None when it fails, so the caller keeps the batch's candidates."""
    try:
        reply = answer_with_reasoning(provider, [{"role": "user", "content": render_prompt("spec-selection.batch-judge", task=task, block="\n".join(lines))}],
                                      model=model, reasoning="minimal", step=STEP_SELECT)
    except Exception:  # noqa: BLE001  a failed call must never lose the candidates
        log.debug("spec batch judge failed", exc_info=True)
        return None
    return parse_ids(reply, offered)


def card_line(entry: Dict[str, Any]) -> str:
    return f"{entry['kind']} {entry['id']}: {entry['title']}" + (f". {entry['summary']}" if entry.get("summary") and entry["summary"] != entry["title"] else "")


def attach_links(result: SpecSelection, by_id: Dict[str, Any], chosen: List[str], expand: int) -> SpecSelection:
    """Set ``result.ids`` to ``chosen`` followed by up to ``expand`` linked records of each chosen one, marked in ``linked``."""
    result.ids = list(chosen)
    for entry_id in chosen:
        added = 0
        for ref in by_id[entry_id]["refs"]:
            if added < expand and ref not in result.ids and ref in by_id:
                result.ids.append(ref)
                result.linked[ref] = entry_id
                added += 1
    return result


def propose(task: str, index: Dict[str, Any], vector_search: Optional[VectorSearch], k_lexical: int, k_vector: int,
            kinds: Optional[Sequence[str]]):
    """Stages 1 and 2: the scored lexical list, the scored vector list and their fusion."""
    by_id = {e["id"]: e for e in index["entries"]}
    lexical = narrow_lexical_scored(index, task, k_lexical, kinds)
    semantic = semantic_scored(vector_search, task, k_vector, by_id, kinds)
    candidates = fuse([r for r in ([i for i, _ in lexical], [i for i, _ in semantic]) if r], k_lexical + k_vector)
    return by_id, lexical, semantic, candidates


@solution("spec-selection")
def select_specs(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                 vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                 batch_size: int = 10, max_workers: int = 4, expand: int = 1,
                 kinds: Optional[Sequence[str]] = CANDIDATE_KINDS) -> SpecSelection:
    """The records of ``index`` that the request ``task`` concerns, as a ``SpecSelection`` (ids best first, linked ids
    after). Lexical narrowing and an optional vector search propose candidates; a confidence gate answers the clear cases
    and the empty ones itself, and a cheap model judges the rest in one call; with no provider the fused candidates are
    returned."""


@implementation("spec-selection", "lexical-vector-llm")
def lexical_vector_llm(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                       vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                       batch_size: int = 10, max_workers: int = 4, expand: int = 1,
                       kinds: Optional[Sequence[str]] = CANDIDATE_KINDS) -> SpecSelection:
    """Lexical TF-IDF over the index, plus a vector search when one is injected, fused, then parallel batched model judging
    of every candidate (no gate: the model is called for every request)."""
    task = prompt_budget.decision_excerpt(task)
    by_id, lexical, semantic, candidates = propose(task, index, vector_search, k_lexical, k_vector, kinds)
    result = SpecSelection(candidates=candidates, route="judged",
                           sources={i: [n for n, r in (("lexical", lexical), ("vector", semantic)) if any(i == x for x, _ in r)]
                                    for i in candidates})
    if not candidates:
        result.route = "nothing"
        return result
    if provider is None:
        chosen = list(candidates)
        result.route = "unjudged"
    else:
        batches = [candidates[i:i + batch_size] for i in range(0, len(candidates), batch_size)]
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(batches)))) as pool:
            verdicts = list(pool.map(lambda b: judge_batch(task, [card_line(by_id[i]) for i in b], set(b), provider, model), batches))
        result.calls = len(batches)
        picked = set()
        for batch, verdict in zip(batches, verdicts):
            if verdict is None:
                result.failed_batches += 1
                picked.update(batch)           # keep the batch rather than lose it
            else:
                picked.update(verdict)
        chosen = [i for i in candidates if i in picked]
    return attach_links(result, by_id, chosen, expand)


def gate_decision(lexical: List[Tuple[str, float]], semantic: List[Tuple[str, float]]) -> str:
    """``sure`` (answer without a model), ``nothing`` (return nothing without a model) or ``ask`` (one model call). A sure
    answer needs the two searches to agree on the first record and strong evidence from at least one of them (a high cosine
    or a high lexical score); nothing needs both to be weak. A vector search that gives no scores can never make a sure
    answer, and without any vector search the lexical score alone can still make an empty one."""
    top_lex = lexical[0][1] if lexical else 0.0
    top_vec = semantic[0][1] if semantic else 0.0
    if lexical and semantic and lexical[0][0] == semantic[0][0] and (top_vec >= GATE_SURE_VECTOR or top_lex >= GATE_SURE_LEXICAL):
        return "sure"
    if top_vec < GATE_NOTHING_VECTOR and top_lex < GATE_NOTHING_LEXICAL:
        return "nothing"
    return "ask"


@implementation("spec-selection", "lexical-vector-gated")
def lexical_vector_gated(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                         vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                         batch_size: int = 10, max_workers: int = 4, expand: int = 1,
                         kinds: Optional[Sequence[str]] = CANDIDATE_KINDS) -> SpecSelection:
    """Lexical plus vector candidates, then a confidence gate: a clear case is answered from the two searches' agreement and an
    empty case returns nothing, both with no model call; every other request costs one call that judges the best
    ``GATE_POOL`` candidates together. Cheaper and faster than judging every request in batches, with the model kept for the
    requests that need it. With no provider the ambiguous requests return the best fused candidates."""
    task = prompt_budget.decision_excerpt(task)
    by_id, lexical, semantic, candidates = propose(task, index, vector_search, k_lexical, k_vector, kinds)
    result = SpecSelection(candidates=candidates,
                           sources={i: [n for n, r in (("lexical", lexical), ("vector", semantic)) if any(i == x for x, _ in r)]
                                    for i in candidates})
    route = gate_decision(lexical, semantic) if candidates else "nothing"
    if route == "nothing":
        result.route = "nothing"
        return result
    if route == "sure":
        rank = lambda ranked: [i for i, _ in ranked[:GATE_AGREE_RANK]]   # noqa: E731
        agreed = set(rank(lexical)) & set(rank(semantic))
        result.route = "sure"
        return attach_links(result, by_id, [i for i in candidates if i in agreed], expand)
    pool = candidates[:GATE_POOL]
    if provider is None:
        result.route = "unjudged"
        return attach_links(result, by_id, pool[:UNJUDGED_TOP], expand)
    result.route, result.calls = "judged", 1
    verdict = judge_batch(task, [card_line(by_id[i]) for i in pool], set(pool), provider, model)
    if verdict is None:
        result.failed_batches = 1
        verdict = pool[:UNJUDGED_TOP]                  # a failed call keeps the best few rather than nothing
    return attach_links(result, by_id, [i for i in pool if i in set(verdict)], expand)


@implementation("spec-selection", "lexical-only")
def lexical_only(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                 vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                 batch_size: int = 10, max_workers: int = 4, expand: int = 1,
                 kinds: Optional[Sequence[str]] = CANDIDATE_KINDS) -> SpecSelection:
    """The baseline: the lexical top three, no vector search and no model. Free and instant; shows what the rest adds."""
    by_id = {e["id"]: e for e in index["entries"]}
    top = narrow_lexical(index, prompt_budget.decision_excerpt(task), min(k_lexical, 3), kinds)
    result = SpecSelection(candidates=list(top), route="lexical", sources={i: ["lexical"] for i in top})
    return attach_links(result, by_id, top, expand)


def related_specs(index: Dict[str, Any], entry_id: str, depth: int = 1, limit: int = 30) -> List[Dict[str, Any]]:
    """Records connected to ``entry_id`` (a chosen record's problems, ideals, solutions, metrics, implementations), nearest
    first, each ``{id, kind, title, depth, via}``. ``depth`` 1 is the direct links, 2 theirs. Read from the index alone: free.
    Raises KeyError for an unknown id. Order within a step: problems, solutions, ideals, metrics, implementations."""
    by_id = {e["id"]: e for e in index["entries"]}
    if entry_id not in by_id:
        raise KeyError(entry_id)
    rank = {"problem": 0, "solution": 1, "ideal": 2, "metric": 3, "implementation": 4, "intent": 5}
    seen, out, frontier = {entry_id}, [], [entry_id]
    for step in range(1, max(depth, 0) + 1):
        found = [{"id": ref, "kind": by_id[ref]["kind"], "title": by_id[ref]["title"], "depth": step, "via": src}
                 for src in frontier for ref in by_id[src]["refs"] if ref in by_id and not (ref in seen or seen.add(ref))]
        found.sort(key=lambda h: (rank.get(h["kind"], 9), h["id"]))
        out += found
        frontier = [h["id"] for h in found]
    return out[:limit]


def intent_specs(index: Dict[str, Any], intent_id: str = "") -> List[Dict[str, Any]]:
    """Every record of one intent (empty id: all), problems first, as ``{id, kind, title}``: the list to scan when a request is
    known to belong to an intent but not yet to a record."""
    rank = {"intent": 0, "problem": 1, "ideal": 2, "solution": 3, "metric": 4, "implementation": 5}
    mine = [e for e in index["entries"] if not intent_id or e["intent"] == intent_id or e["id"] == intent_id]
    return [{"id": e["id"], "kind": e["kind"], "title": e["title"]} for e in sorted(mine, key=lambda e: (rank.get(e["kind"], 9), e["id"]))]


SELECT_SPEC = declare(
    "spec-selection.batch-judge", solution="spec-selection", metric="metric-spec-selection",
    tier="caller", cache_layer="none", template=SELECT_PROMPT)
