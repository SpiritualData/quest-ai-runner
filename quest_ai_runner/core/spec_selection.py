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
from typing import Any, Callable, Dict, List, Optional, Union

from esoi import implementation, solution

from . import prompt_budget
from .adapters import STEP_SELECT, answer_with_reasoning

log = logging.getLogger("quest-ai-runner.spec-selection")

SUPPORTED_INDEX_VERSION = 1
RRF_OFFSET = 60            # reciprocal rank fusion: score = sum 1 / (RRF_OFFSET + rank)
TOKEN = re.compile(r"[a-z0-9]+")
STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the their this to was were which "
                 "with without not no but can may use used uses when what who why how into than then them they there so "
                 "such each every any all one more most only also other another own same too very if over under up out "
                 "about after before between both do does done get gets via per".split())

VectorSearch = Callable[[str, int], List[str]]   # (request text, k) -> entry ids, best first

SELECT_PROMPT = """You choose which project specifications a new request concerns.

REQUEST: {task}

CANDIDATES (kind id: title. summary):
{block}

Return ONLY the candidates that the request concerns: a problem it exhibits, an ideal that decides what "better" means for \
it, a solution or metric it should use. Prefer few and exact over many. Use only the ids shown. Respond with ONLY valid JSON \
(no markdown): {{"ids": ["<id>", ...]}}"""


@dataclass
class SpecSelection:
    """What was chosen and how: ``ids`` best first, then linked ids; ``candidates`` is everything the model was shown."""
    ids: List[str] = field(default_factory=list)
    candidates: List[str] = field(default_factory=list)
    linked: Dict[str, str] = field(default_factory=dict)     # linked id -> the selected id it came in through
    calls: int = 0
    failed_batches: int = 0
    sources: Dict[str, List[str]] = field(default_factory=dict)   # id -> ["lexical", "vector"] that proposed it


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


def narrow_lexical(index: Dict[str, Any], text: str, k: int) -> List[str]:
    """Ids of the ``k`` entries with the best field-weighted TF-IDF against the request, best first; none for no match."""
    query = set(tokens(text))
    n, df = index["n"], index["df"]
    scored = []
    for entry in index["entries"]:
        matched = [t for t in query if t in entry["terms"]]
        if matched:
            score = sum(entry["terms"][t] * (math.log((n + 1) / (df.get(t, 0) + 1)) + 1.0) for t in matched)
            scored.append((-score, entry["id"]))
    return [i for _, i in sorted(scored)[:k]]


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
        reply = answer_with_reasoning(provider, [{"role": "user", "content": SELECT_PROMPT.format(task=task, block="\n".join(lines))}],
                                      model=model, reasoning="minimal", step=STEP_SELECT)
    except Exception:  # noqa: BLE001  a failed call must never lose the candidates
        log.debug("spec batch judge failed", exc_info=True)
        return None
    return parse_ids(reply, offered)


def card_line(entry: Dict[str, Any]) -> str:
    return f"{entry['kind']} {entry['id']}: {entry['title']}" + (f". {entry['summary']}" if entry.get("summary") and entry["summary"] != entry["title"] else "")


@solution("spec-selection")
def select_specs(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                 vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                 batch_size: int = 10, max_workers: int = 4, expand: int = 1) -> SpecSelection:
    """The records of ``index`` that the request ``task`` concerns, as a ``SpecSelection`` (ids best first, linked ids
    after). Lexical narrowing and an optional vector search propose candidates, a cheap model judges them in parallel
    batches; with no provider the fused candidates are returned."""


@implementation("spec-selection", "lexical-vector-llm")
def lexical_vector_llm(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                       vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                       batch_size: int = 10, max_workers: int = 4, expand: int = 1) -> SpecSelection:
    """Lexical TF-IDF over the index, plus a vector search when one is injected, fused, then parallel batched model judging."""
    task = prompt_budget.decision_excerpt(task)
    by_id = {e["id"]: e for e in index["entries"]}
    lexical = narrow_lexical(index, task, k_lexical)
    semantic: List[str] = []
    if vector_search is not None:
        try:
            semantic = [i for i in vector_search(task, k_vector) if i in by_id]
        except Exception:  # noqa: BLE001  semantic widening is optional; lexical still answers
            log.debug("spec vector search failed", exc_info=True)
    candidates = fuse([r for r in (lexical, semantic) if r], k_lexical + k_vector)
    result = SpecSelection(candidates=candidates, sources={i: [n for n, r in (("lexical", lexical), ("vector", semantic)) if i in r]
                                                              for i in candidates})
    if not candidates:
        return result
    if provider is None:
        chosen = list(candidates)
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
    result.ids = list(chosen)
    for entry_id in chosen:
        added = 0
        for ref in by_id[entry_id]["refs"]:
            if added < expand and ref not in result.ids and ref in by_id:
                result.ids.append(ref)
                result.linked[ref] = entry_id
                added += 1
    return result


@implementation("spec-selection", "lexical-only")
def lexical_only(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                 vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                 batch_size: int = 10, max_workers: int = 4, expand: int = 1) -> SpecSelection:
    """The baseline: the lexical top few, no vector search and no model. Free and instant; shows what the rest adds."""
    by_id = {e["id"]: e for e in index["entries"]}
    top = narrow_lexical(index, prompt_budget.decision_excerpt(task), min(k_lexical, 5))
    result = SpecSelection(candidates=list(top), ids=list(top), sources={i: ["lexical"] for i in top})
    for entry_id in top:
        for ref in by_id[entry_id]["refs"][:expand]:
            if ref not in result.ids:
                result.ids.append(ref)
                result.linked[ref] = entry_id
    return result
