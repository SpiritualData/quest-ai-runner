"""Find the intent specifications a new use case or input concerns, from an ESOI specification index.

The generic stages (field-weighted lexical narrowing, optional vector widening, reciprocal rank fusion, the confidence gate,
batched judging, links) are the ESOI library's ``esoi.spec_search``; the index is its ``esoi.spec_index``. This module is what
is QAR's own: the index loader, the judge prompt and the model call, the gate thresholds tuned for QAR's records, and the
three declared implementations of the solution ``spec-selection``, each a thin call of the library with those pieces.

With no provider the fused candidates are the answer (graceful: never worse than lexical).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from esoi import implementation, solution, spec_search as search_lib

from . import prompt_budget
from .prompt_specs import declare, render_prompt
from hubofhubs.ports.model import STEP_SELECT, answer_with_reasoning

log = logging.getLogger("quest-ai-runner.spec-selection")

SUPPORTED_INDEX_VERSION = 1
VectorSearch = search_lib.VectorSearch
SpecSelection = search_lib.Selection   # ids, candidates, linked, calls, failed_batches, sources, route
# Records that state intent are candidates; implementations are reached through their solution's links (there are several per
# solution and they would crowd the candidates).
CANDIDATE_KINDS = search_lib.RECORD_CANDIDATE_KINDS
# The gate (documented in docs/spec-selection.md): tuned on the dev split of the evaluation cases only.
GATE = search_lib.Gate(sure_vector=0.72, sure_lexical=60.0, nothing_vector=0.52, nothing_lexical=12.0, agree_rank=3, pool=20)

SELECT_PROMPT = """You choose which project specifications a new request concerns.

REQUEST: {task}

CANDIDATES (kind id: title. summary):
{block}

Return the candidates that the request concerns: a problem it exhibits, an ideal that decides what "better" means for it, \
a solution or metric it should use. Prefer the most specific records and at most four. Return an empty list only when the \
request is about something none of the candidates describe (a similar word is not enough). Use only the ids shown. Respond \
with ONLY valid JSON (no markdown): {{"ids": ["<id>", ...]}}"""


def load_spec_index(source: Union[str, Path, Dict[str, Any]]) -> Dict[str, Any]:
    """The index from a file path or an already parsed dict; refuses a version this module does not understand."""
    index = source if isinstance(source, dict) else json.loads(Path(source).read_text(encoding="utf-8"))
    if index.get("version") != SUPPORTED_INDEX_VERSION or "entries" not in index:
        raise ValueError(f"not a version {SUPPORTED_INDEX_VERSION} specification index (esoi spec index writes one)")
    return index


def model_judge(provider: Any, model: Optional[str]) -> Callable[[str, List[str], int], str]:
    """The library's ``judge``: renders the QAR prompt over the candidate lines, calls the model, returns its reply text. A
    failing call raises and the library keeps the candidates."""
    def judge(task: str, lines: List[str], limit: int) -> str:  # noqa: ARG001  the prompt does not name a limit
        return answer_with_reasoning(provider, [{"role": "user", "content": render_prompt("spec-selection.batch-judge", task=task, block="\n".join(lines))}],
                                     model=model, reasoning="minimal", step=STEP_SELECT)
    return judge


def card_line(entry: Dict[str, Any]) -> str:
    return f"{entry['kind']} {entry['id']}: {entry['title']}" + (f". {entry['summary']}" if entry.get("summary") and entry["summary"] != entry["title"] else "")


def run(strategy: str, task: str, index: Dict[str, Any], provider: Any, model: Optional[str], vector_search: Optional[VectorSearch],
        k_lexical: int, k_vector: int, batch_size: int, max_workers: int, expand: int, kinds: Optional[Sequence[str]]) -> SpecSelection:
    return search_lib.search(prompt_budget.decision_excerpt(task), search_lib.views_from_index(index),
                            judge=model_judge(provider, model) if provider is not None else None, vector_search=vector_search,
                            strategy=strategy, k_lexical=k_lexical, k_vector=k_vector, batch_size=batch_size, max_workers=max_workers,
                            expand=expand, ranked_kinds=kinds, gate=GATE)


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
    return run("judge-all", task, index, provider, model, vector_search, k_lexical, k_vector, batch_size, max_workers, expand, kinds)


@implementation("spec-selection", "lexical-vector-gated")
def lexical_vector_gated(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                         vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                         batch_size: int = 10, max_workers: int = 4, expand: int = 1,
                         kinds: Optional[Sequence[str]] = CANDIDATE_KINDS) -> SpecSelection:
    """Lexical plus vector candidates, then the confidence gate (``GATE``): a clear case and an empty case are answered with no
    model call, every other request costs one call that judges the best candidates together. With no provider the ambiguous
    requests return the best fused candidates."""
    return run("gated", task, index, provider, model, vector_search, k_lexical, k_vector, batch_size, max_workers, expand, kinds)


@implementation("spec-selection", "lexical-only")
def lexical_only(task: str, index: Dict[str, Any], *, provider: Any = None, model: Optional[str] = None,
                 vector_search: Optional[VectorSearch] = None, k_lexical: int = 15, k_vector: int = 15,
                 batch_size: int = 10, max_workers: int = 4, expand: int = 1,
                 kinds: Optional[Sequence[str]] = CANDIDATE_KINDS) -> SpecSelection:
    """The baseline: the lexical top three, no vector search and no model. Free and instant; shows what the rest adds."""
    return run("lexical", task, index, None, None, None, k_lexical, k_vector, batch_size, max_workers, expand, kinds)


SELECT_SPEC = declare(
    "spec-selection.batch-judge", solution="spec-selection", metric="metric-spec-selection",
    tier="caller", cache_layer="none", template=SELECT_PROMPT)
