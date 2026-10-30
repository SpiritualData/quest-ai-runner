"""The LLM topic-card pass.

Split out of ``file_context_store.py`` by definition; that module re-exports every name here.
"""
from __future__ import annotations

import logging

from typing import Any, Dict, List
from pathlib import Path

from hubofhubs.recall.context_card_extraction import discover_areas, extract_topic_cards
from hubofhubs.recall.context_bootstrap_support import (
    CHUNK_SIZE,
    run_parallel,
    select_representative_files,
)
from .card_store_settings import bootstrap_workers
from .context_card_dedup import _dedup_topic_cards

_log = logging.getLogger("quest-ai-runner.context")


def _llm_topic_cards(file_paths, provider, model=None, existing_cards=None, walk_root=None):
    """3-stage parallel LLM fan-out to identify semantic topic cards.

    Stage 1 -- chunk the path list and run area-discovery calls in parallel (each chunk
    is small so each call is fast). Stage 2 -- for each discovered area, sample representative
    files, extract snippets, summarize by length, then run a topic-extraction call in parallel.
    Stage 3 -- dedup via keyword-clustering + an LLM merge decision (``_dedup_topic_cards``),
    also folding new cards into duplicate ``existing_cards``.

    ``walk_root`` (optional) is used in Stage 2 to read file snippets for context. When absent,
    Stage 2 falls back to file paths only.

    Never raises. Returns [] on any unrecoverable error.
    """
    if provider is None or not file_paths:
        return []
    try:
        allowed = set(file_paths)
        paths = list(file_paths)

        # Stage 1: select representative files per folder using TF-DF-IDF to reduce token spend,
        # then chunk and discover areas in parallel. This heuristic selects files distinctive
        # within each folder (high within-folder term frequency, penalizing corpus-generic terms),
        # so the LLM sees diverse structure without processing all paths.
        representative_paths = select_representative_files(paths, samples_per_folder=3)
        chunks = [representative_paths[i:i + CHUNK_SIZE] for i in range(0, len(representative_paths), CHUNK_SIZE)]
        _log.info(
            "context index: bootstrap stage 1 — %d representative file(s) from %d total file(s) "
            "across %d chunk(s)",
            len(representative_paths), len(paths), len(chunks),
        )
        areas: List[Dict[str, Any]] = []
        for result in run_parallel(
            [lambda c=chunk: discover_areas(c, provider, model) for chunk in chunks],
            max_workers=bootstrap_workers(),
        ):
            if result:
                areas.extend(result)

        if not areas:
            _log.warning("context index: bootstrap stage 1 returned no areas")
            return []
        _log.info("context index: bootstrap stage 2 — extracting topics from %d area(s)", len(areas))

        # Stage 2: extract topic cards per area in parallel (with sampled snippets).
        raw_cards: List[Dict[str, Any]] = []
        walk_root_path = Path(walk_root).resolve() if walk_root else None
        for result in run_parallel(
            [lambda a=area: extract_topic_cards(a, allowed, provider, model, walk_root=walk_root_path) for area in areas],
            max_workers=bootstrap_workers(),
        ):
            if result:
                raw_cards.extend(result)

        if not raw_cards:
            _log.warning("context index: bootstrap stage 2 returned no topic cards")
            return []

        # Stage 3: dedup via keyword-clustering + LLM merge decision (folding into existing cards).
        merged = _dedup_topic_cards(raw_cards, provider, model, existing_cards=existing_cards)
        _log.info(
            "context index: bootstrap stage 3 — deduped %d raw cards into %d unique cards",
            len(raw_cards), len(merged),
        )
        return merged

    except Exception as exc:  # noqa: BLE001
        _log.warning("context index: LLM topic identification failed: %s", exc, exc_info=True)
        return []
