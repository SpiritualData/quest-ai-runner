"""The selection table: for every swappable solution (a ``docs/structure/solutions.csv`` row with a port),
which implementations exist and which one is the default. Data only, no logic and no environment reads.

Shape, keyed by ``solution_id``::

    "<solution_id>": {
        "default": "<implementation name>",
        "implementations": {
            "<name>": "<dotted.module>:<Attr>"                      # the class or factory
                    or {"target": "<module>:<Attr>", "config": "<module>:<ConfigAttr>"},
        },
    }

``core.resolve.resolve(solution_id, name)`` turns a name into that object, importing it lazily (so core
never imports an adapter at import time and an optional dependency is only needed when its implementation
is chosen). Switching an implementation is an edit to a ``default`` here, a ``name`` passed by the caller or
``RunnerConfig.selections``; never a new ``if``/``elif`` at the call site. Every implementation module must
be listed in that solution's ``implementations`` column; ``scripts/check_structure.py`` (S14) enforces both
that every port row appears here and that the two lists agree.
"""
from __future__ import annotations

_A = "quest_ai_runner.adapters."
_C = "quest_ai_runner.core."
_R = "quest_ai_runner.runner."

SELECTION = {
    "gather-evidence": {"default": "composite", "implementations": {
        "files": _A + "files_adapter:FilesAdapter",
        "cached_db": _A + "cached_db_adapter:CachedDbAdapter",
        "composite": _A + "composite_retrieval_adapter:CompositeRetrievalAdapter",
        "mcp": _A + "mcp_retrieval_adapter:MCPRetrievalAdapter",
        "notion": _A + "notion_adapter:NotionAdapter",
        "google_drive": _A + "google_drive_adapter:GoogleDriveAdapter",
        "google_chat": _A + "google_chat_adapter:GoogleChatAdapter",
        "quest": _A + "quest_retrieval_adapter:QuestRetrievalAdapter",
    }},
    "search-the-web": {"default": "web_research", "implementations": {
        "web_research": _A + "web_research:WebResearchAdapter",
        "web_search": _A + "web_search_adapter:WebSearchAdapter",
        "provider_web_search": _A + "provider_web_search_adapter:ProviderWebSearchAdapter",
    }},
    # The default is the keyless one; the env-driven chooser in cli_config picks by API keys first.
    "call-a-model": {"default": "claude_cli", "implementations": {
        "anthropic": _A + "anthropic_provider:AnthropicProvider",
        "gemini": _A + "gemini_provider:GeminiProvider",
        "openai": _A + "openai_provider:OpenAIProvider",
        "deepseek": _A + "deepseek_provider:DeepSeekProvider",
        "claude_cli": _A + "claude_cli_provider:ClaudeCliProvider",
    }},
    # "config" is the dataclass the runner is built from (working_dir and context_preamble are common to both).
    "run-deep-work": {"default": "subprocess", "implementations": {
        "subprocess": {"target": _C + "goal_runner:SubprocessGoalRunner", "config": _C + "goal_runner:SubprocessConfig"},
        "acp": {"target": _A + "acp_deep_runner:AcpDeepRunner", "config": _A + "acp_deep_runner:AcpConfig"},
        "fast_edit": _A + "fast_edit_runner:FastEditRunner",
        "mcp_write": _A + "mcp_write_runner:MCPOperationRunner",
    }},
    "write-files": {"default": "files", "implementations": {"files": _A + "files_writer:FilesWriter"}},
    "write-through-mcp": {"default": "mcp", "implementations": {"mcp": _A + "mcp_write_adapter:MCPWriteAdapter"}},
    "escalate-to-a-human": {"default": "quest_decision", "implementations": {
        "quest_decision": _R + "quest_client:QuestDecisionSink"}},
    "show-progress": {"default": "stream", "implementations": {
        "stream": _C + "adapters:StreamSink", "milestone": _C + "adapters:MilestoneSink"}},
    "remember-conversations": {"default": "session_file", "implementations": {
        "session_file": _A + "session_file_conversation_store:SessionFileConversationStore"}},
    "select-context": {"default": "hybrid", "implementations": {
        "file": _A + "file_context_store:FileContextStore",
        "vector": _A + "vector_context_assembler:VectorContextAssembler",
        "hybrid": _A + "hybrid_context_assembler:HybridContextAssembler",
        "bm25": _A + "bm25_content_store:BM25ContentStore",
        "rep": _A + "rep_context_assembler:RepContextAssembler",
        "composite": _C + "composite_assembler:CompositeContextAssembler",
    }},
    "persist-cards": {"default": "quest_api", "implementations": {
        "filesystem": _A + "card_repository:FilesystemCardRepository",
        "qdrant": _A + "qdrant_card_repository:QdrantCardRepository",
        "quest_api": _A + "quest_api_card_repository:QuestApiCardRepository",
    }},
    "index-by-meaning": {"default": "qdrant", "implementations": {"qdrant": _A + "qdrant_vector_store:QdrantVectorStore"}},
    "guide-by-feedback": {"default": "universal", "implementations": {
        "universal": _C + "guidance_provider:UniversalGuidanceProvider"}},
    "talk-over-a-channel": {"default": "openclaw", "implementations": {"openclaw": _A + "openclaw_channel:OpenClawChannel"}},
    "search-backends": {"default": "serper", "implementations": {
        "serper": _A + "web_search_backends:SerperBackend",
        "brave": _A + "web_search_backends:BraveBackend",
        "tavily": _A + "web_search_backends:TavilyBackend",
        "searxng": _A + "web_search_backends:SearxngBackend",
        "gemini": _A + "web_search_backends:GeminiGroundingBackend",
        "provider": _A + "web_search_backends:ProviderNativeBackend",
    }},
    "feed-context-sources": {"default": "base", "implementations": {"base": _R + "context_sources_base:ContextSource"}},
    "store-feedback": {"default": "ledger", "implementations": {"ledger": _R + "feedback_ledger:FeedbackLedger"}},
    "recall-recent-context": {"default": "file", "implementations": {"file": _C + "recent_context:FileRecentContextStore"}},
    "receive-input-mid-run": {"default": "in_memory", "implementations": {"in_memory": _C + "inbox:InMemoryInbox"}},
    "resolve-references": {"default": "note", "implementations": {"note": _A + "reference_resolver:NoteResolver"}},
    "cache-cards": {"default": "null", "implementations": {
        "quest_api": _A + "quest_api_card_repository:CardCache",
        "null": _A + "quest_api_card_repository:NullCardCache"}},
}
