"""Reference adapter implementations.

These satisfy the ports in ``quest_ai_runner.core.adapters_*`` (and ``hubofhubs.ports``); a consumer wires the ones it
needs into a ``RunnerConfig``. This package is the public import path for them
(``from quest_ai_runner.adapters import FilesAdapter, CompositeRetrievalAdapter``). Each class says what it is in its own
module docstring, and ``esoi/COMPONENTS.md`` lists them by role: retrieval (files, remote files, cached DB, Claude
sessions, Google Chat and Drive, Notion, Quest, MCP, web research), model providers (Anthropic, Gemini, OpenAI, DeepSeek,
Claude CLI), context assemblers and card stores (file, BM25, vector, hybrid, Qdrant, Quest API), deep runners
(fast edit, ACP, MCP operation) and the file writer. Optional extras (``gemini``, ``openai``, ``bm25``, ``qdrant``) are
imported behind a guard, so a name whose extra is missing is ``None``.

The DeepRunner reference (SubprocessGoalRunner) lives in core.goal_runner; the EscalationSink
reference (the Quest team decision-request) lives in runner.quest_client.
"""
from .acp_deep_runner import AcpConfig, AcpDeepRunner
from .mcp_client import MCPClient, MCPDiscovery, MCPResourceResult, MCPServerSpec, MCPToolResult
from .mcp_retrieval_adapter import MCPRetrievalAdapter
from .mcp_write_adapter import MCPWriteAdapter
from .mcp_write_runner import MCPOperationConfig, MCPOperationRunner
from .anthropic_provider import AnthropicProvider
from .quest_context_adapter import QuestContextAdapter, build_quest_resolvers
from .cached_db_adapter import CachedDbAdapter
from .provider_web_search_adapter import ProviderWebSearchAdapter
from hubofhubs.websearch.cache import WebCache, normalize_query
from hubofhubs.websearch.page_extract import check_url_is_safe, extract_main_text, select_passages, split_passages
from .web_search_backends import (
    BraveBackend,
    FallbackSearchBackend,
    GeminiGroundingBackend,
    ProviderNativeBackend,
    SearchBackend,
    SearchBackendError,
    SearchHit,
    SearchResponse,
    SearxngBackend,
    SerperBackend,
    TavilyBackend,
    select_search_backend,
)
from .web_research import WebResearchAdapter, build_web_research_from_env
from .card_metadata_generator import CardMetadataGenerator
from .claude_cli_provider import ClaudeCliProvider
from .claude_conversations_adapter import ClaudeConversationsAdapter
from .google_chat_adapter import (
    GoogleChatAdapter,
    ChatSpaces,
    space_label,
    static_token_provider,
    service_account_token_provider,
)
from .notion_adapter import NotionAdapter
from .google_drive_adapter import (
    GoogleDriveAdapter,
    parse_drive_url,
    DEFAULT_DRIVE_SCOPES,
)
from .drive_comments import (
    COMMENT_READ_SCOPES,
    COMMENT_WRITE_SCOPES,
    DriveComment,
    DriveComments,
    DriveFileChange,
    render_comments,
    unanswered,
)
from .inbound_mail import (
    INBOUND_MAIL_READ_SCOPES,
    InboundMail,
    MailMessage,
    is_bulk_mail,
    refuses_as_inbound_mailbox,
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
from .remote_files_adapter import RemoteFilesAdapter
from .private_link_web import PrivateLinkWebResearch
from .files_writer import FilesWriter
from .guidance_card_manager import GuidanceCard, GuidanceCardManager
from .hybrid_context_assembler import HybridContextAssembler
from .quest_guidance_loader import QuestGuidanceLoader
from .quest_retrieval_adapter import QuestRetrievalAdapter
from .quest_api_card_repository import (
    NullCardCache,
    QuestApiCardRepository,
    QuestApiCardVectorStore,
    QuestCardApiError,
)
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

# DeepSeekProvider (OpenAI-compatible API) shares the openai optional package.
try:
    from .deepseek_provider import DeepSeekProvider
except ImportError:
    DeepSeekProvider = None  # type: ignore[assignment,misc]

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
