"""Reference adapter implementations.

These satisfy the ports in ``quest_ai_runner.core.adapters_*`` (and ``hubofhubs.ports``); a consumer wires the ones it
needs into a ``RunnerConfig``. This package is the public import path for them
(``from quest_ai_runner.adapters import FilesAdapter, CompositeRetrievalAdapter``). Each class says what it is in its own
module docstring, and ``esoi/COMPONENTS.md`` lists them by role: retrieval (files, remote files, cached DB, Claude
sessions, Google Chat and Drive, Notion, Quest, MCP, web research), model providers (Anthropic, Gemini, OpenAI, DeepSeek,
Claude CLI), context assemblers and card stores (file, BM25, vector, hybrid, Qdrant, Quest API), deep runners
(fast edit, ACP, MCP operation) and the file writer. Optional extras (``bm25``, ``qdrant``) are
imported behind a guard, so a name whose extra is missing is ``None``.

The DeepRunner reference (SubprocessGoalRunner) lives in core.goal_runner; the EscalationSink
reference (the Quest team decision-request) lives in runner.quest_client.
"""
from .acp_deep_runner import AcpConfig, AcpDeepRunner
from .mcp_client import MCPClient, MCPServerSpec
from .mcp_retrieval_adapter import MCPRetrievalAdapter
from .mcp_write_runner import MCPOperationRunner
from .cached_db_adapter import CachedDbAdapter
from .provider_web_search_adapter import ProviderWebSearchAdapter
from hubofhubs.websearch.cache import WebCache, normalize_query
from hubofhubs.websearch.page_extract import check_url_is_safe, extract_main_text
from .web_search_backends import FallbackSearchBackend, SearchBackend, SearchBackendError, SearchHit, SearchResponse, select_search_backend
from .web_research import WebResearchAdapter, build_web_research_from_env
from .claude_cli_provider import ClaudeCliProvider
from .claude_conversations_adapter import ClaudeConversationsAdapter
from .google_chat_adapter import GoogleChatAdapter, ChatSpaces, static_token_provider, service_account_token_provider
from .notion_adapter import NotionAdapter
from .google_drive_adapter import GoogleDriveAdapter
from .drive_comments import COMMENT_WRITE_SCOPES, DriveComments, unanswered
from .inbound_mail import InboundMail, is_bulk_mail, refuses_as_inbound_mailbox
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
from .quest_api_card_repository import QuestApiCardRepository, QuestApiCardVectorStore
from .reference_resolver import ReferenceResolver, NoteResolver, build_resolver_registry, collect_reference_resolvers
from .vector_context_assembler import VectorContextAssembler

# Each of these needs an optional extra (bm25s, qdrant-client + fastembed): a missing extra leaves the
# name ``None`` instead of breaking ``import quest_ai_runner.adapters``.
try:
    from .bm25_content_store import BM25ContentStore
except ImportError:
    BM25ContentStore = None  # type: ignore[assignment,misc]
try:
    from .qdrant_vector_store import QdrantVectorStore, make_voyage_embedder
except ImportError:
    QdrantVectorStore = None  # type: ignore[assignment,misc]
    make_voyage_embedder = None  # type: ignore[assignment]
try:
    from .qdrant_card_repository import QdrantCardRepository, QdrantCardVectorStore
except ImportError:
    QdrantCardRepository = None  # type: ignore[assignment,misc]
    QdrantCardVectorStore = None  # type: ignore[assignment,misc]
