"""Production context assemblers over a folder of files: a plain text-search ranker and the hub's feature-reranked retriever.

Each class is a ``ContextAssembler`` (``assemble`` and ``record``), so the ``select-context`` seat can build it. The ranking is one method,
``ranked_files``, which the evaluation arms of the context-selection metric call too: what is measured is the code that would run, not a copy.
``FeatureRerankAssembler`` owns no retrieval code; it builds the hub's ``retrieve-context`` arm (``hubofhubs.retrieval.fusion.FeatureRerank``)
and renders its hits. The corpus is a mapping of path to text handed in, else the readable text files under ``root`` (default: the working
folder), read on the first request.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Union

from esoi import implementation
from hubofhubs.api import AssembledContext, ContextAssembler
from hubofhubs.api import FeatureRerank

from quest_ai_runner.the_body.internal_context.card_store.file_context_store import tokenize

TEXT_SUFFIXES = (".py", ".md", ".txt", ".rst", ".toml", ".yaml", ".yml", ".json", ".csv")
SKIPPED_FOLDERS = {"node_modules", "__pycache__", "venv", ".venv", "site-packages", "dist", "build"}
MAX_FILE_BYTES = 200_000
MAX_FILES = 5000
BLIND_GREP_LIMIT = 10
Corpus = Union[Mapping[str, str], Callable[[], Mapping[str, str]], None]


def read_folder(root: Path) -> Dict[str, str]:
    """The readable text files under ``root`` as ``{path relative to root: text}``, skipping hidden and vendored folders and large files."""
    out: Dict[str, str] = {}
    for folder, names, files in os.walk(root):
        names[:] = sorted(n for n in names if not n.startswith(".") and n not in SKIPPED_FOLDERS)
        for name in sorted(files):
            path = Path(folder) / name
            if path.suffix in TEXT_SUFFIXES and not name.startswith(".") and path.stat().st_size <= MAX_FILE_BYTES:
                out[str(path.relative_to(root))] = path.read_text(encoding="utf-8", errors="replace")
                if len(out) >= MAX_FILES:
                    return out
    return out


class FolderAssembler(ContextAssembler):
    """What the two assemblers share: where the corpus comes from, and rendering ranked files as one context view."""

    label = "files"

    def __init__(self, files: Corpus = None, root: Optional[str] = None) -> None:
        self.source, self.root, self.held = files, Path(root) if root else None, None

    def corpus(self) -> Mapping[str, str]:
        if self.held is None:
            source = self.source
            self.held = dict(source() if callable(source) else source) if source is not None else read_folder(self.root or Path.cwd())
        return self.held

    def ranked_files(self, task_text: str) -> List[str]:
        raise NotImplementedError

    def assemble(self, task_text: str, *, meta: Optional[Dict[str, Any]] = None, on_event: Optional[Any] = None) -> AssembledContext:
        ranked = self.ranked_files(task_text)
        view = "\n".join(f"- {path}: {self.corpus().get(path, '').strip().splitlines()[0] if self.corpus().get(path, '').strip() else ''}"
                         for path in ranked)
        return AssembledContext(context_view=("Files that bear on the request:\n" + view) if ranked else "", card_ids=list(ranked),
                                sources=[{"adapter": self.label, "label": "files", "items": list(ranked)}] if ranked else [])

    def record(self, task_text: str, outcome: Dict[str, Any]) -> None:
        return None


@implementation("select-context", "blind_grep")
class BlindGrepAssembler(FolderAssembler):
    """Ranks every file by how many distinct task tokens (tokenized the way the card store does) appear in its text: no cards, no index, no
    model. What any context layer must beat to justify its existence. Ties break by path, so the ranking is deterministic."""

    label = "keyword"

    def __init__(self, files: Corpus = None, root: Optional[str] = None, limit: int = BLIND_GREP_LIMIT) -> None:
        super().__init__(files, root)
        self.limit, self.lowered = limit, None

    def ranked_files(self, task_text: str, files: Optional[Mapping[str, str]] = None) -> List[str]:
        if files is not None:
            self.held, self.lowered = dict(files), None
        if self.lowered is None:
            self.lowered = {path: text.lower() for path, text in self.corpus().items()}
        tokens = tokenize(task_text)
        scores = {path: sum(1 for t in tokens if t in text) for path, text in self.lowered.items()}
        return [path for path, score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0])) if score > 0][:self.limit]


@implementation("select-context", "hub_feature_rerank")
class FeatureRerankAssembler(FolderAssembler):
    """The hub's ``feature_rerank`` retrieval arm: BM25 and dense retrieval fused, then reranked by weighted features and priors. The hub owns
    the ranking; this renders its hits as the context view. The first request indexes the corpus, later ones reuse the index."""

    label = "hybrid"

    def __init__(self, files: Corpus = None, root: Optional[str] = None, budget_tokens: int = 4000,
                 history: Sequence[Mapping[str, Any]] = ()) -> None:
        super().__init__(files, root)
        self.retriever, self.budget_tokens, self.history, self.prepared = FeatureRerank(), budget_tokens, tuple(history), False

    def ranked_files(self, task_text: str, files: Optional[Mapping[str, str]] = None) -> List[str]:
        if files is not None:
            self.held, self.prepared = dict(files), False
        hits = self.retriever.retrieve(task_text, None if self.prepared else self.corpus(), budget_tokens=self.budget_tokens,
                                       history=self.history)
        self.prepared = True
        return list(dict.fromkeys(hit.file for hit in hits))
