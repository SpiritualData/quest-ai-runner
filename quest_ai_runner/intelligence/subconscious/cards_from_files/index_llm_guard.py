"""The volume guard of a context index pass: no model stage runs on thousands of files or cards.

``llm_allowed`` is asked once per pass, before stage 2 (topic discovery) and stage 3 (stale card regeneration), with the counts the pass already holds:
over ``QAR_INDEX_LLM_MAX_FILES`` files to analyse, or over ``QAR_INDEX_LLM_MAX_CARDS`` cards already stored, every model stage is skipped, one warning
names the counts, and a marker file (``llm_stages_skipped.json`` in the cards folder) records it so the next pass stays quiet and does not retry until
the counts drop (then the marker is removed). ``cap_work`` bounds what a pass that may use the model does: ``QAR_INDEX_MAX_NEW_FILES_PER_RUN`` files and
``QAR_INDEX_MAX_STALE_CARDS_PER_RUN`` cards; the rest carries over to the next pass because it stays uncovered or stale.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import List, Sequence, Tuple

from quest_ai_runner.body.configuration.card_store_settings import (
    index_llm_max_cards, index_llm_max_files, index_max_new_files_per_run, index_max_stale_cards_per_run)

_log = logging.getLogger("quest-ai-runner.context")

MARKER_NAME = "llm_stages_skipped.json"


def llm_allowed(cards_dir, new_files: int, card_count: int, *, write_marker: bool = True) -> bool:
    """False when the pass must skip every model stage (see the module docstring). Never raises."""
    max_files, max_cards = index_llm_max_files(), index_llm_max_cards()
    too_many_files = bool(max_files) and new_files > max_files
    too_many_cards = bool(max_cards) and card_count > max_cards
    marker = Path(str(cards_dir)) / MARKER_NAME
    if not (too_many_files or too_many_cards):
        try:
            if marker.exists() and write_marker:
                marker.unlink()
                _log.info("context index: counts are back under the limits (%d new files, %d cards); model stages resume", new_files, card_count)
        except OSError:
            pass
        return True
    already = marker.exists()
    if not already:
        _log.warning("context index: skipping every model stage: %d file(s) to analyse (limit %s) and %d card(s) in the store (limit %s). No model call is made "
                     "until the counts drop under QAR_INDEX_LLM_MAX_FILES / QAR_INDEX_LLM_MAX_CARDS; prune the store or narrow the corpus.",
                     new_files, max_files or "none", card_count, max_cards or "none")
        if write_marker:
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(json.dumps({"files_to_analyse": new_files, "cards": card_count, "max_files": max_files, "max_cards": max_cards,
                                              "ts": round(time.time())}), encoding="utf-8")
            except OSError as error:
                _log.warning("context index: could not write %s: %s", marker, error)
    return False


def cap_work(new_files: Sequence[str], stale_card_ids: Sequence[str]) -> Tuple[List[str], List[str]]:
    """The files and stale cards this pass may spend model calls on, in the order given; the rest waits for the next pass."""
    max_files, max_stale = index_max_new_files_per_run(), index_max_stale_cards_per_run()
    files = list(new_files)[:max_files] if max_files else list(new_files)
    stale = list(stale_card_ids)[:max_stale] if max_stale else list(stale_card_ids)
    if len(files) < len(new_files):
        _log.info("context index: analysing %d of %d new file(s) this run (QAR_INDEX_MAX_NEW_FILES_PER_RUN); the rest carry over", len(files), len(new_files))
    if len(stale) < len(stale_card_ids):
        _log.info("context index: regenerating %d of %d stale card(s) this run (QAR_INDEX_MAX_STALE_CARDS_PER_RUN); the rest carry over", len(stale), len(stale_card_ids))
    return files, stale
