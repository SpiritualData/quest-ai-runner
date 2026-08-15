"""Reference adapter implementations.

These satisfy the core interfaces; a consumer wires the ones it needs into a RunnerConfig:
  * FilesAdapter              — RetrievalAdapter over a configured file root (quest-docs + corpus).
  * FilesWriter               — FileWriter: the OPT-IN write side of that same root. Confines every
                                path with the SAME resolver FilesAdapter reads through, refuses
                                credential-ish files, and backs up what it replaces. Constructing
                                one is how a consumer grants this library write access; nothing
                                builds it implicitly.
  * FastEditRunner            — DeepRunner that lands a bounded file edit in ONE model call
                                (whole-file rewrite for short files, SEARCH/REPLACE above a line
                                threshold) instead of spawning a full agent. Needs a FileWriter.
                                Wired as the first rung of the deep-runner ladder when
                                RunnerConfig.file_writer is set; see docs/fast-edit-runner.md.
  * CachedDbAdapter           — RetrievalAdapter: live DB reads via a short-TTL cache (no file sync).
  * ClaudeConversationsAdapter — RetrievalAdapter over Claude Code session transcripts (local directory).
  * GoogleChatAdapter         — RetrievalAdapter over Google Chat spaces/threads/messages (read +
                                discovery + pre-flight assemble). Auth is injected via a token
                                provider (``static_token_provider`` or, for a Google Workspace,
                                ``service_account_token_provider`` with domain-wide delegation —
                                the latter needs the optional [google] extra). HTTP is stdlib-only.
  * GoogleDriveAdapter        — RetrievalAdapter over Google Drive files/folders (list a folder,
                                read a file's text content: Google Docs, Google Sheets first sheet,
                                PDF via the optional [drive] extra, or plain text). Auth is injected
                                the SAME way as GoogleChatAdapter (reuses its token-provider
                                helpers); HTTP is stdlib-only except the lazy ``pypdf`` import used
                                only when a PDF is actually read. ``parse_drive_url`` parses a Drive/
                                Docs/Sheets URL into a file/folder id.
  * QuestRetrievalAdapter     — RetrievalAdapter: query Quest API for goal/quest context, notes, related goals.
  * WebSearchAdapter          — RetrievalAdapter that searches the live web via the Tavily API (stdlib
                                urllib only; no extra deps). Enable with WEB_SEARCH_ENABLED=true +
                                WEB_SEARCH_API_KEY env vars, or pass api_key= at construction.
  * ProviderWebSearchAdapter  — RetrievalAdapter that searches the live web via the model provider's
                                NATIVE tool (Anthropic web_search / Gemini Google Search grounding),
                                reusing the LLM key (no separate web-search key). The key-free default;
                                wired automatically by build_orchestrator unless WEB_SEARCH_ENABLED=false.
  * CompositeRetrievalAdapter — RetrievalAdapter that runs multiple adapters IN PARALLEL, merging
                                results. Query files, databases, conversations, Quest, task memory together.
  * AnthropicProvider         — ModelProvider (plan / answer / live models.list bucketing). Needs an
                                ANTHROPIC_API_KEY (per-token billing).
  * ClaudeCliProvider         — ModelProvider that drives the local ``claude`` CLI headless, KEYLESS:
                                plan/answer run on the box's Claude Code subscription login (no API key).
  * GeminiProvider            — ModelProvider backed by Google Gemini API. Needs a GOOGLE_API_KEY.
  * FileContextStore          — ContextAssembler backed by per-card JSON files (stdlib-only). Selects
                                relevant cards by keyword overlap, checks file freshness, and renders
                                a context_view string for the orchestrator's pre-flight injection.
  * ReferenceResolver         — resolves ONE typed context-card content item (a reference resolved
                                fresh to current content, or an LLM note) into rendered text. Ships
                                built-in ``note``/``file`` resolvers; ``build_resolver_registry``
                                merges them with consumer-injected collection/conversation/query
                                resolvers. NEVER raises. See reference_resolver.py.
  * VectorContextAssembler    — ContextAssembler backed by a VectorStore (semantic search with
                                optional agentic LLM query-gen + review). Stdlib + the VectorStore
                                Protocol only; heavy deps behind an optional extra.
  * HybridContextAssembler    — Fuses a keyword/IDF assembler and a vector assembler in parallel
                                (RRF-style complementary fusion). Stdlib only.
  * BM25ContentStore          — ContextAssembler using BM25 over ACTUAL FILE CONTENT (not summaries).
                                Finds exact identifiers, rare tokens, and specific phrases that the
                                dense vector arm (which only embeds summaries) cannot. Agentic
                                parallel multi-query: generates diverse queries via an optional
                                ModelProvider and searches IN PARALLEL. Requires the [bm25] extra.
  * QdrantVectorStore         — VectorStoreBase backed by a local-filesystem or remote Qdrant
                                instance. Requires the [qdrant] optional extra.
  * QuestContextAdapter       — HTTP client for ``POST /api/cards/assemble`` (the Quest
                                context hub). Returns merged context string via ``resolve()``
                                or both context and cards via ``fetch()``; degrades gracefully
                                on failures. ``build_quest_resolvers(adapter)`` builds a
                                ``{collection, query}`` ReferenceResolver dict ready to inject
                                into ``RunnerConfig.reference_resolvers`` so a QAR resolves
                                Quest refs through the hub instead of leaving them unresolved.

  * AcpDeepRunner            — OPT-IN alternative DeepRunner: drives Claude over the Agent Client
                                Protocol (a live JSON-RPC session with the Node
                                ``claude-agent-acp`` agent) instead of a one-shot ``claude -p``
                                subprocess, which is what makes MID-TURN STEERING possible — a
                                message queued while the deep turn is running is injected into that
                                turn, not held until the next attempt. Purely additive:
                                ``SubprocessGoalRunner`` remains the default. Needs the optional
                                [acp] extra plus Node >= 22. See docs/acp-deep-runner.md.
  * MCPClient / MCPServerSpec — raw MCP (Model Context Protocol) protocol client (stdio subprocess
                                or streamable HTTP transport), zero QAR semantics: connect/discover/
                                list tools+resources/call a tool/read a resource. Needs the optional
                                [mcp] extra; imported lazily inside its own connection seam, exactly
                                like AcpDeepRunner, so it never requires the package to be installed.
  * MCPRetrievalAdapter      — RetrievalAdapter over ONE MCP server, mapping the discovery quartet
                                onto MCP's own resources/list + tools/list, gating tools/call behind
                                an explicit allowlist, and namespacing every surfaced name with a
                                configured alias so multiple MCP servers never collide inside a
                                CompositeRetrievalAdapter. Read-only foundation phase.

The DeepRunner reference (SubprocessGoalRunner) lives in core.goal_runner; the EscalationSink
reference (the Quest team decision-request) lives in runner.quest_client.
"""
from .acp_deep_runner import AcpConfig, AcpDeepRunner
from .mcp_client import MCPClient, MCPDiscovery, MCPResourceResult, MCPServerSpec, MCPToolResult
from .mcp_retrieval_adapter import MCPRetrievalAdapter
from .anthropic_provider import AnthropicProvider
from .quest_context_adapter import QuestContextAdapter, build_quest_resolvers
from .cached_db_adapter import CachedDbAdapter
from .web_search_adapter import WebSearchAdapter
from .provider_web_search_adapter import ProviderWebSearchAdapter
from .card_metadata_generator import CardMetadataGenerator
from .claude_cli_provider import ClaudeCliProvider
from .claude_conversations_adapter import ClaudeConversationsAdapter
from .google_chat_adapter import (
    GoogleChatAdapter,
    static_token_provider,
    service_account_token_provider,
)
from .google_drive_adapter import (
    GoogleDriveAdapter,
    parse_drive_url,
    DEFAULT_DRIVE_SCOPES,
)
from .composite_retrieval_adapter import CompositeRetrievalAdapter
from .session_file_conversation_store import SessionFileConversationStore
from .conversation_card_builder import ConversationCardBuilder
from .feedback_processor import FeedbackProcessor
from .card_repository import (
    CardRepository,
    FilesystemCardRepository,
    card_embed_text,
)
from .file_context_store import FileContextStore
from .fast_edit_runner import FastEditConfig, FastEditRunner
from .files_adapter import FilesAdapter
from .files_writer import FilesWriter
from .guidance_card_manager import GuidanceCard, GuidanceCardManager
from .hybrid_context_assembler import HybridContextAssembler
from .quest_guidance_loader import QuestGuidanceLoader
from .quest_retrieval_adapter import QuestRetrievalAdapter
from .quest_api_card_repository import QuestApiCardRepository
from .reference_resolver import (
    ReferenceResolver,
    NoteResolver,
    build_resolver_registry,
    collect_reference_resolvers,
    coerce_resolver,
    make_file_resolver,
)
from .vector_context_assembler import VectorContextAssembler

# GeminiProvider requires the google-generativeai optional package.
# Guard the import so that ``import quest_ai_runner.adapters`` works even without it installed.
try:
    from .gemini_provider import GeminiProvider
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False
    GeminiProvider = None  # type: ignore[assignment,misc]

# OpenAIProvider requires the openai optional package.
# Guard the import so that ``import quest_ai_runner.adapters`` works even without it installed.
try:
    from .openai_provider import OpenAIProvider
    _OPENAI_AVAILABLE = True
except ImportError:
    _OPENAI_AVAILABLE = False
    OpenAIProvider = None  # type: ignore[assignment,misc]

# BM25ContentStore requires the [bm25] optional extra (bm25s).
# Guard the import so that ``import quest_ai_runner.adapters`` works even without
# bm25s installed.  Consumers that want BM25ContentStore must install the extra.
try:
    from .bm25_content_store import BM25ContentStore
    _BM25_AVAILABLE = True
except ImportError:
    _BM25_AVAILABLE = False
    BM25ContentStore = None  # type: ignore[assignment,misc]

# QdrantVectorStore requires the [qdrant] optional extra (qdrant-client + fastembed).
# Guard the import so that ``import quest_ai_runner.adapters`` works even without
# those packages installed.  Consumers that want QdrantVectorStore must install the
# extra and then either import it directly from this module or from its own module.
try:
    from .qdrant_vector_store import QdrantVectorStore, make_voyage_embedder, make_openai_embedder
    _QDRANT_AVAILABLE = True
except ImportError:
    _QDRANT_AVAILABLE = False
    QdrantVectorStore = None  # type: ignore[assignment,misc]
    make_voyage_embedder = None  # type: ignore[assignment]
    make_openai_embedder = None  # type: ignore[assignment]

# QdrantCardRepository + QdrantCardVectorStore: a generic Qdrant-backed CardRepository (cards persist
# as points in one collection, optionally tenant-scoped) and a query-only vector arm over the SAME
# collection. Behind the [qdrant] optional extra, same guarded pattern as QdrantVectorStore.
try:
    from .qdrant_card_repository import QdrantCardRepository, QdrantCardVectorStore
    _QDRANT_CARDS_AVAILABLE = True
except ImportError:
    _QDRANT_CARDS_AVAILABLE = False
    QdrantCardRepository = None  # type: ignore[assignment,misc]
    QdrantCardVectorStore = None  # type: ignore[assignment,misc]

__all__ = [
    "AcpConfig",
    "AcpDeepRunner",
    "MCPClient",
    "MCPServerSpec",
    "MCPDiscovery",
    "MCPToolResult",
    "MCPResourceResult",
    "MCPRetrievalAdapter",
    "FilesAdapter",
    "FilesWriter",
    "FastEditRunner",
    "FastEditConfig",
    "CachedDbAdapter",
    "ClaudeConversationsAdapter",
    "GoogleChatAdapter",
    "static_token_provider",
    "service_account_token_provider",
    "GoogleDriveAdapter",
    "parse_drive_url",
    "DEFAULT_DRIVE_SCOPES",
    "SessionFileConversationStore",
    "QuestRetrievalAdapter",
    "WebSearchAdapter",
    "ProviderWebSearchAdapter",
    "CompositeRetrievalAdapter",
    "ConversationCardBuilder",
    "CardMetadataGenerator",
    "GuidanceCardManager",
    "GuidanceCard",
    "FeedbackProcessor",
    "QuestGuidanceLoader",
    "AnthropicProvider",
    "ClaudeCliProvider",
    "GeminiProvider",
    "OpenAIProvider",
    "FileContextStore",
    "ReferenceResolver",
    "NoteResolver",
    "build_resolver_registry",
    "collect_reference_resolvers",
    "coerce_resolver",
    "make_file_resolver",
    "VectorContextAssembler",
    "HybridContextAssembler",
    "BM25ContentStore",
    "QdrantVectorStore",
    "QdrantCardRepository",
    "QdrantCardVectorStore",
    "make_voyage_embedder",
    "make_openai_embedder",
    "CardRepository",
    "FilesystemCardRepository",
    "card_embed_text",
    "QuestContextAdapter",
    "build_quest_resolvers",
    "QuestApiCardRepository",
]
