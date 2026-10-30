"""BM25ContentStore: QAR's binding of the hub's BM25 content store (``hubofhubs.recall.bm25_store``).

The store is the hub's: a ContextAssembler that indexes the actual text of the files under a root with BM25 (exact
identifiers, rare tokens, specific phrases that the dense vector layer never sees), refreshes only the files whose hash
changed, and optionally has a model write extra queries that run in parallel. It reads no environment and holds no prompt of
its own. What stays with QAR:

* the directories a walk never enters: QAR's skip set (``QAR_SKIP_DIRS`` and its card store folder, ``walk_settings``);
* the query-generation prompt as a registered prompt spec (the standard text is the hub's);
* the declaration that this class is the ``bm25`` implementation of ``select-context``, and the install hint for the extra.

Install the optional extra to use it: ``pip install 'quest-ai-runner[bm25]'`` (``bm25s``).
"""
from __future__ import annotations

from typing import Any

from esoi import implementation
from hubofhubs.recall import bm25_store as hub
from hubofhubs.recall.bm25_store import BOOTSTRAP_MAX_BYTES, SOURCE_EXTS  # noqa: F401  (same objects, old import path)

from ..core.prompt_specs import declare, render_prompt
from .walk_settings import effective_skip_dirs

QUERY_SPEC = declare(
    "select-context.bm25-queries", solution="select-context", metric="metric-select-context",
    tier="caller", cache_layer="none", template=hub.QUERY_TEMPLATE, writes_text=False)


def query_prompt(num_queries: int, task_text: str) -> str:
    return render_prompt("select-context.bm25-queries", num_queries=num_queries, task_text=task_text)


@implementation("select-context", "bm25")
class BM25ContentStore(hub.BM25ContentStore):
    """The hub's BM25 content store under QAR's skip set and registered query prompt.

    Takes the hub's constructor arguments (``root``, ``index_dir``, ``provider``, ``query_model``, ``num_queries``,
    ``top_k``, ``max_in_view``, ``confidence_threshold``).
    """

    def __init__(self, **settings: Any) -> None:
        try:
            super().__init__(skip_dirs=effective_skip_dirs, query_prompt=query_prompt, **settings)
        except ImportError as exc:
            raise ImportError(str(exc).replace("hubofhubs[bm25]", "quest-ai-runner[bm25]")) from exc
