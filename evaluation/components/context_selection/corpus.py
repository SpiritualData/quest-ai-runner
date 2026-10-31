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
    SOURCE_FILE_WEIGHT, TEST_FILE_WEIGHT, build_rich_summary, extract_symbols, is_test_path, path_slug,
)

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".quest-context", ".eggs", ".mypy_cache",
             ".pytest_cache", "dist", "build", "quest_ai_runner.egg-info"}


# The evaluation harness is not part of the corpus: its own task and label strings would sit in the haystack
# (the old script found itself), and every new eval file would move the numbers.
SKIP_TOP_LEVEL = {"evaluation"}


@dataclass
class Corpus:
    root: Path
    files: list = field(default_factory=list)   # root-relative .py paths (extra roots: prefixed by their dir name)
    cards: dict = field(default_factory=dict)   # card id -> card (one file each)
    symbols: int = 0
    extra_roots: dict = field(default_factory=dict)   # path prefix (the root's dir name) -> directory

    @property
    def key(self):
        """Identity of the indexed roots, for the per-process caches the arms keep."""
        return "|".join([str(self.root)] + [str(p) for p in self.extra_roots.values()])

    def path_of(self, rel):
        """The file on disk for a path this corpus lists."""
        head, _, rest = rel.partition("/")
        if head in self.extra_roots:
            return self.extra_roots[head] / rest
        return self.root / rel


def walk_py(base, prefix=""):
    """Sorted ``.py`` paths under ``base``, relative to it (and to ``prefix`` when given)."""
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        skip = SKIP_DIRS | (SKIP_TOP_LEVEL if Path(dirpath) == base and not prefix else set())
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(str(Path(prefix) / (Path(dirpath) / name).relative_to(base)))
    return found


def build_corpus(root, extra_roots=()):
    """Walk ``root`` (and each of ``extra_roots``) for ``.py`` files and build one card per file. Read-only,
    deterministic, no model. An extra root's files are listed under its directory name (``hubofhubs/x.py``), so
    the primary root's paths, and every case that names them, are unchanged."""
    root = Path(root).resolve()
    corpus = Corpus(root=root)
    corpus.files.extend(walk_py(root))
    for extra in extra_roots:
        base = Path(extra).resolve()
        if base == root or base.name in corpus.extra_roots or (root / base.name).exists():
            raise ValueError(f"extra root {base} would collide with an existing path prefix")
        corpus.extra_roots[base.name] = base
        corpus.files.extend(walk_py(base, base.name))
    for rel in corpus.files:
        path = corpus.path_of(rel)
        syms = extract_symbols(path)
        summary, description = build_rich_summary(rel, path, syms)
        cid = path_slug(rel)
        corpus.cards[cid] = dict(
            id=cid, keywords=[], summary=summary, description=description,
            files=[dict(path=rel, symbols=syms)],
            weight=TEST_FILE_WEIGHT if is_test_path(rel) else SOURCE_FILE_WEIGHT)
        corpus.symbols += len(syms)
    return corpus


_CACHE = {}


def corpus_for(root, extra_roots=()):
    """One build per set of roots per process: the cases share it."""
    key = "|".join([str(Path(root).resolve())] + [str(Path(e).resolve()) for e in extra_roots])
    if key not in _CACHE:
        _CACHE[key] = build_corpus(root, extra_roots)
    return _CACHE[key]


def configured_corpus(default_root):
    """The corpus the scorer indexes. ``QAR_EVAL_CORPUS_ROOT`` replaces the primary root; ``QAR_EVAL_CORPUS_EXTRA_ROOTS``
    (``os.pathsep``-separated directories) adds roots, none by default, so recorded rows stay comparable."""
    extra = [p for p in (os.environ.get("QAR_EVAL_CORPUS_EXTRA_ROOTS") or "").split(os.pathsep) if p]
    return corpus_for(os.environ.get("QAR_EVAL_CORPUS_ROOT") or default_root, extra)
