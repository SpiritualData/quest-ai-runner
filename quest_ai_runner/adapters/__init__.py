"""Reference adapter implementations.

These satisfy the core interfaces; a consumer wires the ones it needs into a RunnerConfig:
  * FilesAdapter              — RetrievalAdapter over a configured file root (quest-docs + corpus).
  * CachedDbAdapter           — RetrievalAdapter: live DB reads via a short-TTL cache (no file sync).
  * AnthropicProvider         — ModelProvider (plan / answer / live models.list bucketing). Needs an
                                ANTHROPIC_API_KEY (per-token billing).
  * ClaudeCliProvider         — ModelProvider that drives the local ``claude`` CLI headless, KEYLESS:
                                plan/answer run on the box's Claude Code subscription login (no API key).
  * FileContextStore          — ContextAssembler backed by per-card JSON files (stdlib-only). Selects
                                relevant cards by keyword overlap, checks file freshness, and renders
                                a context_view string for the orchestrator's pre-flight injection.
  * VectorContextAssembler    — ContextAssembler backed by a VectorStore (semantic search with
                                optional agentic LLM query-gen + review). Stdlib + the VectorStore
                                Protocol only; heavy deps behind an optional extra.
  * HybridContextAssembler    — Fuses a keyword/IDF assembler and a vector assembler in parallel
                                (RRF-style complementary fusion). Stdlib only.
  * QdrantVectorStore         — VectorStoreBase backed by a local-filesystem or remote Qdrant
                                instance. Requires the [qdrant] optional extra.

The DeepRunner reference (SubprocessGoalRunner) lives in core.goal_runner; the EscalationSink
reference (the Quest team decision-request) lives in runner.quest_client.
"""
from .anthropic_provider import AnthropicProvider
from .cached_db_adapter import CachedDbAdapter
from .claude_cli_provider import ClaudeCliProvider
from .file_context_store import FileContextStore
from .files_adapter import FilesAdapter
from .hybrid_context_assembler import HybridContextAssembler
from .vector_context_assembler import VectorContextAssembler

# QdrantVectorStore requires the [qdrant] optional extra (qdrant-client + fastembed).
# Guard the import so that ``import quest_ai_runner.adapters`` works even without
# those packages installed.  Consumers that want QdrantVectorStore must install the
# extra and then either import it directly from this module or from its own module.
try:
    from .qdrant_vector_store import QdrantVectorStore
    _QDRANT_AVAILABLE = True
except ImportError:
    _QDRANT_AVAILABLE = False
    QdrantVectorStore = None  # type: ignore[assignment,misc]

__all__ = [
    "FilesAdapter",
    "CachedDbAdapter",
    "AnthropicProvider",
    "ClaudeCliProvider",
    "FileContextStore",
    "VectorContextAssembler",
    "HybridContextAssembler",
    "QdrantVectorStore",
]
