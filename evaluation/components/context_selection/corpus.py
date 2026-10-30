"""Builds the corpus every context-selection solution is handed, with no model call.

The production store builds its topic cards with a model (``FileContextStore.bootstrap`` is a no-op
without a provider), so a zero-call evaluation cannot use that path. This module reproduces the
deterministic part it always had, using the store's own extractors: one card per ``.py`` file with its
symbols, the module and class/function docstring first lines (``summary`` / ``description``), the path,
and the test-file weight. The ``evaluation/`` folder is left out. Scoring those cards with ``FileContextStore.assemble`` is the "IDF with
docstrings" arm; ``card_embed_text`` over the same cards is what the vector arm embeds.
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

import common  # noqa: F401  (puts the repo root on sys.path)
from hubofhubs.recall.context_card_extraction import (
    _SOURCE_FILE_WEIGHT, _TEST_FILE_WEIGHT, _build_rich_summary, _extract_symbols, _is_test_path, _path_slug,
)

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".quest-context", ".eggs", ".mypy_cache",
             ".pytest_cache", "dist", "build", "quest_ai_runner.egg-info"}


# The evaluation harness is not part of the corpus: its own task and label strings would sit in the haystack
# (the old script found itself), and every new eval file would move the numbers.
SKIP_TOP_LEVEL = {"evaluation"}


@dataclass
class Corpus:
    root: Path
    files: list = field(default_factory=list)   # repo-relative .py paths, sorted
    cards: dict = field(default_factory=dict)   # card id -> card (one file each)
    symbols: int = 0


def build_corpus(root):
    """Walk ``root`` for ``.py`` files and build one card per file. Read-only, deterministic, no model."""
    root = Path(root).resolve()
    corpus = Corpus(root=root)
    for dirpath, dirnames, filenames in os.walk(root):
        skip = SKIP_DIRS | (SKIP_TOP_LEVEL if Path(dirpath) == root else set())
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            if name.endswith(".py"):
                corpus.files.append(str((Path(dirpath) / name).relative_to(root)))
    for rel in corpus.files:
        path = root / rel
        syms = _extract_symbols(path)
        summary, description = _build_rich_summary(rel, path, syms)
        cid = _path_slug(rel)
        corpus.cards[cid] = dict(
            id=cid, keywords=[], summary=summary, description=description,
            files=[dict(path=rel, symbols=syms)],
            weight=_TEST_FILE_WEIGHT if _is_test_path(rel) else _SOURCE_FILE_WEIGHT)
        corpus.symbols += len(syms)
    return corpus


_CACHE = {}


def corpus_for(root):
    """One build per root per process: the 15 cases share it."""
    key = str(Path(root).resolve())
    if key not in _CACHE:
        _CACHE[key] = build_corpus(key)
    return _CACHE[key]
