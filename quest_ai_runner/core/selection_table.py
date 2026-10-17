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
be listed in that solution's ``implementations`` column; ``esoi check`` (S14) enforces both
that every port row appears here and that the two lists agree.
"""
from __future__ import annotations

ADAPTERS = "quest_ai_runner.adapters."
CORE = "quest_ai_runner.core."
RUNNER = "quest_ai_runner.runner."

SELECTION = {
    "gather-evidence": {"default": "composite", "implementations": {
        "files": ADAPTERS + "files_adapter:FilesAdapter",
        "cached_db": ADAPTERS + "cached_db_adapter:CachedDbAdapter",
        "composite": ADAPTERS + "composite_retrieval_adapter:CompositeRetrievalAdapter",
        "mcp": ADAPTERS + "mcp_retrieval_adapter:MCPRetrievalAdapter",
        "notion": ADAPTERS + "notion_adapter:NotionAdapter",
        "google_drive": ADAPTERS + "google_drive_adapter:GoogleDriveAdapter",
        "google_chat": ADAPTERS + "google_chat_adapter:GoogleChatAdapter",
        "quest": ADAPTERS + "quest_retrieval_adapter:QuestRetrievalAdapter",
    }},
    "search-the-web": {"default": "web_research", "implementations": {
        "web_research": ADAPTERS + "web_research:WebResearchAdapter",
        "web_search": ADAPTERS + "web_search_adapter:WebSearchAdapter",
        "provider_web_search": ADAPTERS + "provider_web_search_adapter:ProviderWebSearchAdapter",
        # The search backends are this solution's implementations too (select_search_backend resolves them here).
        "serper": ADAPTERS + "web_search_backends:SerperBackend",
        "brave": ADAPTERS + "web_search_backends:BraveBackend",
        "tavily": ADAPTERS + "web_search_backends:TavilyBackend",
        "searxng": ADAPTERS + "web_search_backends:SearxngBackend",
        "gemini": ADAPTERS + "web_search_backends:GeminiGroundingBackend",
        "provider": ADAPTERS + "web_search_backends:ProviderNativeBackend",
    }},
    # Every model call goes through MultiProvider (prefix routing, tier fallback, retry): build_orchestrator wraps
    # whichever primary vendor below the environment picks (cli_config._model_provider_from_env: API keys first, the
    # keyless claude_cli last) in it. So the router is the default and the vendors are the primaries it wraps.
    "call-a-model": {"default": "multi_provider", "implementations": {
        "multi_provider": ADAPTERS + "multi_provider:MultiProvider",
        "anthropic": ADAPTERS + "anthropic_provider:AnthropicProvider",
        "gemini": ADAPTERS + "gemini_provider:GeminiProvider",
        "openai": ADAPTERS + "openai_provider:OpenAIProvider",
        "deepseek": ADAPTERS + "deepseek_provider:DeepSeekProvider",
        "claude_cli": ADAPTERS + "claude_cli_provider:ClaudeCliProvider",
    }},
    # "config" is the dataclass the runner is built from (working_dir and context_preamble are common to both).
    "run-deep-work": {"default": "subprocess", "implementations": {
        "subprocess": {"target": CORE + "goal_runner:SubprocessGoalRunner", "config": CORE + "goal_runner:SubprocessConfig"},
        "acp": {"target": ADAPTERS + "acp_deep_runner:AcpDeepRunner", "config": ADAPTERS + "acp_deep_runner:AcpConfig"},
        "fast_edit": ADAPTERS + "fast_edit_runner:FastEditRunner",
        "mcp_write": ADAPTERS + "mcp_write_runner:MCPOperationRunner",
    }},
    "write-files": {"default": "files", "implementations": {"files": ADAPTERS + "files_writer:FilesWriter"}},
    "write-through-mcp": {"default": "mcp", "implementations": {"mcp": ADAPTERS + "mcp_write_adapter:MCPWriteAdapter"}},
    "escalate-to-a-human": {"default": "quest_decision", "implementations": {
        "quest_decision": RUNNER + "quest_client:QuestDecisionSink"}},
    "show-progress": {"default": "stream", "implementations": {
        "stream": CORE + "adapters:StreamSink", "milestone": CORE + "adapters:MilestoneSink"}},
    "remember-conversations": {"default": "session_file", "implementations": {
        "session_file": ADAPTERS + "session_file_conversation_store:SessionFileConversationStore"}},
    "select-context": {"default": "hybrid", "implementations": {
        "file": ADAPTERS + "file_context_store:FileContextStore",
        "vector": ADAPTERS + "vector_context_assembler:VectorContextAssembler",
        "hybrid": ADAPTERS + "hybrid_context_assembler:HybridContextAssembler",
        "bm25": ADAPTERS + "bm25_content_store:BM25ContentStore",
        "rep": ADAPTERS + "rep_context_assembler:RepContextAssembler",
        "composite": CORE + "composite_assembler:CompositeContextAssembler",
    }},
    # operate-a-quest: the pre-route may carry out a clear tool request without the planner;
    # "orchestrator" is the planner path alone. The default is the quest_ops winner.
    "operate-a-quest": {"default": "pre_route", "implementations": {
        "orchestrator": CORE + "pre_route:NoPreRoute",
        "pre_route": CORE + "pre_route:PreRouter",
    }},
    "persist-cards": {"default": "quest_api", "implementations": {
        "filesystem": ADAPTERS + "card_repository:FilesystemCardRepository",
        "qdrant": ADAPTERS + "qdrant_card_repository:QdrantCardRepository",
        "quest_api": ADAPTERS + "quest_api_card_repository:QuestApiCardRepository",
    }},
    "index-by-meaning": {"default": "qdrant", "implementations": {"qdrant": ADAPTERS + "qdrant_vector_store:QdrantVectorStore"}},
    "guide-by-feedback": {"default": "universal", "implementations": {
        "universal": CORE + "guidance_provider:UniversalGuidanceProvider"}},
    "talk-over-a-channel": {"default": "openclaw", "implementations": {"openclaw": ADAPTERS + "openclaw_channel:OpenClawChannel"}},
    "feed-context-sources": {"default": "base", "implementations": {"base": RUNNER + "context_sources_base:ContextSource"}},
    "store-feedback": {"default": "ledger", "implementations": {"ledger": RUNNER + "feedback_ledger:FeedbackLedger"}},
    "recall-recent-context": {"default": "file", "implementations": {"file": CORE + "recent_context:FileRecentContextStore"}},
    "persist-predictions": {"default": "file", "implementations": {"file": CORE + "anticipation_store:FilePredictionStore"}},
    "receive-input-mid-run": {"default": "in_memory", "implementations": {"in_memory": CORE + "inbox:InMemoryInbox"}},
    "resolve-references": {"default": "note", "implementations": {"note": ADAPTERS + "reference_resolver:NoteResolver"}},
    "cache-cards": {"default": "file", "implementations": {
        "file": ADAPTERS + "card_cache_file:FileCardCache",
        "null": ADAPTERS + "quest_api_card_repository:NullCardCache"}},
}
