"""Reference adapter implementations.

These satisfy the ports in ``quest_ai_runner.core.adapters_*`` (and ``hubofhubs.ports``); a consumer wires the ones it
needs into a ``RunnerConfig``. This package is the import path for the adapters that more than one area, or the generic
core, uses (``from quest_ai_runner.adapters import FilesAdapter``). An adapter that only one area uses lives in that area's
folder (``quest_ai_runner/<intent id>/``: ``know_the_world`` for web research, ``select_context`` for the context assemblers,
``act_as_the_body`` for the retrieval adapters, the writer, the ACP runner and the Quest, Notion and Drive sources,
``remember_and_recall`` for the Qdrant stores, the file card cache, the hub card store and the label generator) and is imported
from there. Each class says what it is in its own module docstring, and ``esoi/COMPONENTS.md`` lists them by role. The fast-edit
and MCP-operation deep runners and the MCP protocol client are the hub's (``hubofhubs.act``). The optional extra ``qdrant`` is needed
only by the Qdrant stores in ``remember_and_recall``.

The DeepRunner reference (SubprocessGoalRunner) lives in core.goal_runner; the EscalationSink
reference (the Quest team decision-request) lives in runner.quest_client.
"""
from hubofhubs.websearch.cache import WebCache, normalize_query
from hubofhubs.websearch.page_extract import check_url_is_safe, extract_main_text
from .claude_cli_provider import ClaudeCliProvider
from .claude_conversations_adapter import ClaudeConversationsAdapter
from .google_chat_adapter import GoogleChatAdapter, ChatSpaces, static_token_provider, service_account_token_provider
from .drive_comments import COMMENT_WRITE_SCOPES, DriveComments, unanswered
from .inbound_mail import InboundMail, is_bulk_mail, refuses_as_inbound_mailbox
from .session_file_conversation_store import SessionFileConversationStore
from hubofhubs.recall.conversation_card_builder import ConversationCardBuilder
from .feedback_processor import FeedbackProcessor
from hubofhubs.recall.card_repository import (
    CardRepository,
    FilesystemCardRepository,
    card_embed_text,
)
from .file_context_store import FileContextStore
from .files_adapter import FilesAdapter
from .private_link_web import PrivateLinkWebResearch
from .guidance_card_manager import GuidanceCard, GuidanceCardManager
from .quest_guidance_loader import QuestGuidanceLoader
from .quest_api_card_repository import QuestApiCardRepository, QuestApiCardVectorStore
from hubofhubs.recall.reference_resolver import ReferenceResolver, NoteResolver, build_resolver_registry, collect_reference_resolvers
