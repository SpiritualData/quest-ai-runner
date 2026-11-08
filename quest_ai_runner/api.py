"""The one public surface of Quest AI Runner for a host application (the Quest backend): import from here, never from a module path.

``run`` is the front door: ``quest_ai_runner.run("<id>", input)`` runs any solution by its id and ``quest_ai_runner.run(Interface)`` returns its typed
handle, for QAR's own solutions and for every hub solution beneath them (the entry of each solution is the one its interface declares with
``@solution``). The names below are what the host builds against where it has to implement a port, read a constant or hold a value object, grouped
by the solution whose interface they belong to. A name is listed only while a consumer outside this repository imports it; it is never underscore-
prefixed, and nothing here probes a runner or a provider for what it can do: the interface is the contract. Hub names (``DeepResult``,
``AssembledContext`` and the rest) come from ``hubofhubs.api``, not from here.
"""
from quest_ai_runner import run

# Front door: one line for any solution (``run("<id>", input)`` or ``run(Interface)``), the hub's and QAR's own
from quest_ai_runner.the_self.assembler.resolve import names, register, resolve

# Request intake (the_consciousness/request_intake): queued tasks, the lane's poller and the bridge to async code
from quest_ai_runner.the_consciousness.request_intake.executor import TaskExecutor
from quest_ai_runner.the_consciousness.request_intake.poller import Poller
from quest_ai_runner.the_consciousness.request_intake.async_bridge import run_coro_sync, run_detached
from quest_ai_runner.the_consciousness.terminal.adapters_sinks import StreamSink

# Judgment calls: the human escalation channel and the staged-plan hand-off a host implements
from quest_ai_runner.the_self.interface import EscalationSinkBase
from quest_ai_runner.the_consciousness.judgment_calls.interface import StagedPlanRunner

# Representatives and Feedback learning: the verbatim feedback ledger and its states
from quest_ai_runner.the_consciousness.feedback_learning.feedback_ledger import (
    ACCEPTED, AWAITING_ACCEPTANCE, AWAITING_ANSWER, BLOCKED, DECLINED, DONE, FeedbackItem, FeedbackLedger, IN_FORCE, IN_PROGRESS, KIND_CONTEXT,
    KIND_QUESTION, KIND_REQUEST, KIND_STANDING, KIND_UNKNOWN, NEEDS_REAPPLYING, NOTED, OPEN, OPEN_STATES, PARTIALLY_APPLIED, SUPERSEDED,
    guidance_writer_for, open_items_block, record_run_account)

# ESOI Application (esoi-application): the name of the arm that edits a quest's own specification through Quest (a host selects it with ``arms`` and passes its Quest client in ``options``)
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.quest_spec_edit import QUEST_SPEC_RECORDS

# Quest account access (the_body/internal_context/quest): the Quest API as the system's own data
from quest_ai_runner.the_body.internal_context.quest.client.quest_client import QuestClient, QuestApiError
from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import DECISION_KIND_RE
from quest_ai_runner.the_body.intake_sources.context_sources_quest import MAX_AWAITING_ACTION
from quest_ai_runner.the_body.internal_context.adapters_values import Escalation
from quest_ai_runner.the_body.internal_context.interface import OwnUpdates

# Internal Context: the retrieval sources a host composes
from quest_ai_runner.the_body.internal_context.quest.cached_db_adapter import CachedDbAdapter
from quest_ai_runner.the_world.tools.connected_system_operations.composite_retrieval_adapter import CompositeRetrievalAdapter
from quest_ai_runner.the_world.tools.connected_system_operations.google_chat_adapter import service_account_token_provider
from quest_ai_runner.the_world.tools.connected_system_operations.google_drive_adapter import DEFAULT_DRIVE_SCOPES, GoogleDriveAdapter, parse_drive_url
from quest_ai_runner.the_world.tools.file_reading.files_adapter import FilesAdapter
from quest_ai_runner.the_world.tools.file_reading.remote_files_adapter import RemoteFilesAdapter, MAX_RESPONSE_BYTES

# Card Store: where cards live and how they are scoped and embedded
from quest_ai_runner.the_body.internal_context.card_store.file_context_store import FileContextStore
from quest_ai_runner.the_body.internal_context.card_store.qdrant_card_repository import QdrantCardRepository, QdrantCardVectorStore, scope_point_id
from quest_ai_runner.the_body.internal_context.card_store.qdrant_vector_store import QdrantVectorStore, make_voyage_embedder, voyage_embedder_id
from quest_ai_runner.the_body.internal_context.card_store.quest_api_card_repository import (
    QuestApiCardRepository, quest_card_settings_from_env, select_card_scope)
from quest_ai_runner.the_body.internal_context.card_store.vector_scopes import CARD_SEED_SCOPE

# Context Retriever (context-retriever): the assemblers and the personal lexicon behind them
from quest_ai_runner.the_intelligence.subconscious.context_retriever.claude_conversations_adapter import ClaudeConversationsAdapter
from quest_ai_runner.the_intelligence.subconscious.context_retriever.hybrid_context_assembler import HybridContextAssembler
from quest_ai_runner.the_intelligence.subconscious.context_retriever.vector_context_assembler import VectorContextAssembler
from quest_ai_runner.the_body.configuration.config import resolve_context_assembler
from quest_ai_runner.the_intelligence.subconscious.context_retriever.card_learning import turn_teaches_new_facts
from quest_ai_runner.the_intelligence.subconscious.context_retriever.personal_lexicon import (
    CombinedBackground, CorpusBackground, LanguageBaseline, LexiconTerm, build_personal_lexicon, lexicon_term_strings, terms_within_budget)

# Intuition (persist-predictions): the anticipation engine's learner, patterns and predictions
from quest_ai_runner.the_intelligence.subconscious.intuition.anticipation import (
    Anticipator, CONF_FLOOR, Pattern, Prediction, assemble_with_scope_tags, bundle_is_cross_quest_mixed, bundle_scope_tags, chips_for_now,
    extract_features, parse_refresh_response, pattern_from_dict, prediction_from_dict)

# Long-Term Memory (recall-recent-context): the scoped recent-context store
from quest_ai_runner.the_intelligence.higher_mind.long_term_memory.recent_context import (
    FileRecentContextStore, GLOBAL_SCOPE_KEY, conv_scope_key, quest_scope_key)

# Objectives (hold-the-intents): quests and goals as live objectives
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_field_instructions import ITEM_INSTRUCTIONS, field_instructions_text
from quest_ai_runner.the_intelligence.higher_mind.objectives.quest_judge import JUDGE_WINDOW, clip_text
# Place on a quest: the host hands the function that places a message on one of the person's quests (its standard function) to the orchestrator as ``quest_placement``
from quest_ai_runner.the_intelligence.higher_mind.objectives.place_on_a_quest.quest_placement import HostFunction

# Guidance: use-case instructions a host supplies
from quest_ai_runner.the_intelligence.higher_mind.guidance.interface import GuidanceCard
from quest_ai_runner.the_intelligence.higher_mind.guidance.guidance_card_manager import GuidanceCardManager
from quest_ai_runner.the_self.interface import GuidanceProviderBase

# Mind: the orchestrator loop, its configuration and its result
from quest_ai_runner.the_intelligence.mind.orchestrator import (
    Orchestrator, OrchestratorConfig, OrchestratorResult, DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION, DEFERRED_RUNNER_KEY, MODE_RELEASE_PROMPT,
    MODE_RELEASE_TOOL, QUEST_DATA_WRITE_SURFACE_INSTRUCTION, apply_quest_data_ladder_guard, unconfirmed_no_change_text)
from quest_ai_runner.the_intelligence.mind.response_writer.orchestrator_text import (
    card_update_store, deep_future_context, future_context_channel, normalize_future_context)
from quest_ai_runner.the_body.configuration.config import RunnerConfig
from quest_ai_runner.the_intelligence.mind.delegation.inbox import InMemoryInbox
from quest_ai_runner.the_intelligence.mind.planner.recipes import RecipeStore

# Response Quality: what a reply may and may not carry
from quest_ai_runner.the_intelligence.critical_thinking.response_quality.scaffolding import (
    FUTURE_CONTEXT_DELIMITER, sanitize_user_text, strip_future_context)

# Prompts: the layered prompt a host composes
from quest_ai_runner.the_self.integrity.prompts.prompt_layers import compose_layers

# Model Plumbing: tiers and the Claude Code provider
from quest_ai_runner.the_world.model_plumbing.model_tier_chooser.model_registry import ModelRegistry, TIERS, LEGACY_TIER_ALIASES
from quest_ai_runner.the_intelligence.mind.delegation.work_model import TIER_WORDS, RUNNABLE_MODELS
from quest_ai_runner.the_world.model_plumbing.vendor_model_caller.claude_cli_provider import ClaudeCliProvider

# Tools and Web Search: the tool registry and the web readers
from quest_ai_runner.the_world.tools.tools import ToolContext, ToolRegistry, ToolResult, ToolSpec
from quest_ai_runner.the_world.tools.tools_status import ToolStatus
# The milestone flow's tools over a host's own Quest client, and the shapes a host's client and metric reader return
from quest_ai_runner.the_body.hands.quest_operations.tools_milestone_flow import milestone_flow_tools
# The workflow tools over a host's own Quest client (list first; create reports an existing workflow as already there)
from quest_ai_runner.the_body.hands.quest_operations.tools_quest_ops import create_quest_workflow_spec, list_quest_workflows_spec
from quest_ai_runner.the_body.internal_context.quest.client.quest_client_support import QuestWriteError, WriteResult
from quest_ai_runner.the_intelligence.critical_thinking.esoi_application.tools_metric_source import MetricSourceError, checked_reading
from quest_ai_runner.the_world.tools.web_search.private_link_web import PrivateLinkWebResearch
from quest_ai_runner.the_world.tools.web_search.web_research import build_web_research_from_env

# Proactivity (gate-an-autopilot-pass): the markers autopilot tasks carry, read back by the host
from quest_ai_runner.the_intelligence.proactivity.autopilot_defaults import (
    AUTOPILOT_PASS_KIND, AUTOPILOT_WORK_KIND, BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS, BUNDLED_DEFAULT_QUEST_INSTRUCTIONS, PROPOSAL_TEXT_PREFIX)

__all__ = [
    "ACCEPTED",
    "AUTOPILOT_PASS_KIND",
    "AUTOPILOT_WORK_KIND",
    "AWAITING_ACCEPTANCE",
    "AWAITING_ANSWER",
    "Anticipator",
    "BLOCKED",
    "BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS",
    "BUNDLED_DEFAULT_QUEST_INSTRUCTIONS",
    "CARD_SEED_SCOPE",
    "CONF_FLOOR",
    "CachedDbAdapter",
    "ClaudeCliProvider",
    "ClaudeConversationsAdapter",
    "CombinedBackground",
    "CompositeRetrievalAdapter",
    "CorpusBackground",
    "DECISION_KIND_RE",
    "DECLINED",
    "DEFAULT_DRIVE_SCOPES",
    "DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION",
    "DEFERRED_RUNNER_KEY",
    "DONE",
    "Escalation",
    "EscalationSinkBase",
    "FUTURE_CONTEXT_DELIMITER",
    "FeedbackItem",
    "FeedbackLedger",
    "FileContextStore",
    "FileRecentContextStore",
    "FilesAdapter",
    "GLOBAL_SCOPE_KEY",
    "GoogleDriveAdapter",
    "GuidanceCard",
    "GuidanceCardManager",
    "GuidanceProviderBase",
    "HostFunction",
    "HybridContextAssembler",
    "IN_FORCE",
    "IN_PROGRESS",
    "ITEM_INSTRUCTIONS",
    "InMemoryInbox",
    "JUDGE_WINDOW",
    "KIND_CONTEXT",
    "KIND_QUESTION",
    "KIND_REQUEST",
    "KIND_STANDING",
    "KIND_UNKNOWN",
    "LEGACY_TIER_ALIASES",
    "LanguageBaseline",
    "LexiconTerm",
    "MAX_AWAITING_ACTION",
    "MAX_RESPONSE_BYTES",
    "MODE_RELEASE_PROMPT",
    "MODE_RELEASE_TOOL",
    "ModelRegistry",
    "NEEDS_REAPPLYING",
    "NOTED",
    "OPEN",
    "OPEN_STATES",
    "Orchestrator",
    "OrchestratorConfig",
    "OrchestratorResult",
    "OwnUpdates",
    "PARTIALLY_APPLIED",
    "PROPOSAL_TEXT_PREFIX",
    "Pattern",
    "Poller",
    "Prediction",
    "PrivateLinkWebResearch",
    "QUEST_DATA_WRITE_SURFACE_INSTRUCTION",
    "QUEST_SPEC_RECORDS",
    "QdrantCardRepository",
    "QdrantCardVectorStore",
    "QdrantVectorStore",
    "QuestApiCardRepository",
    "MetricSourceError",
    "QuestApiError",
    "QuestWriteError",
    "QuestClient",
    "RUNNABLE_MODELS",
    "RecipeStore",
    "RemoteFilesAdapter",
    "RunnerConfig",
    "SUPERSEDED",
    "StagedPlanRunner",
    "StreamSink",
    "TaskExecutor",
    "TIERS",
    "TIER_WORDS",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolStatus",
    "WriteResult",
    "checked_reading",
    "milestone_flow_tools",
    "create_quest_workflow_spec",
    "list_quest_workflows_spec",
    "VectorContextAssembler",
    "apply_quest_data_ladder_guard",
    "assemble_with_scope_tags",
    "build_personal_lexicon",
    "build_web_research_from_env",
    "bundle_is_cross_quest_mixed",
    "bundle_scope_tags",
    "card_update_store",
    "chips_for_now",
    "clip_text",
    "compose_layers",
    "conv_scope_key",
    "deep_future_context",
    "extract_features",
    "field_instructions_text",
    "future_context_channel",
    "guidance_writer_for",
    "lexicon_term_strings",
    "make_voyage_embedder",
    "names",
    "normalize_future_context",
    "open_items_block",
    "parse_drive_url",
    "parse_refresh_response",
    "pattern_from_dict",
    "prediction_from_dict",
    "quest_card_settings_from_env",
    "quest_scope_key",
    "record_run_account",
    "register",
    "resolve",
    "resolve_context_assembler",
    "run",
    "run_coro_sync",
    "run_detached",
    "sanitize_user_text",
    "scope_point_id",
    "select_card_scope",
    "service_account_token_provider",
    "strip_future_context",
    "terms_within_budget",
    "turn_teaches_new_facts",
    "unconfirmed_no_change_text",
    "voyage_embedder_id",
]
