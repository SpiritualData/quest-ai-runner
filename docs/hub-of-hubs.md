# QAR is a hub of hubs

Most tools an AI needs already exist as open-source projects: model gateways, tool servers, search engines, memory layers,
agent harnesses, paper indexes. Building each one into QAR by hand would never finish, and every copy would drift from the
project it copied. So QAR sits on top of those hubs and picks up what they support. The aim is **no code per integration**:
when a hub gains a capability, a lane gains it by editing a configuration file.

The registry of hubs, the research behind it, the scorer and the guides live in the public
[ai-hub-of-hubs-structure](https://github.com/SpiritualData/ai-hub-of-hubs-structure) repository (package `hubofhubs`), a required
dependency that QAR imports at startup. QAR is Quest's connector and the production consumer of it. This page is what is specific to QAR.

## What you can do by configuration today

- **Reach any tool or data source an MCP server offers.** One `[[mcp_servers]]` table in the lane file; prefer one aggregator
  over one entry per app (the hub's `hubofhubs/examples/lane-world-hub.toml` is the shape). The token stays in an
  environment variable named by `token_env`.
  ```toml
  [[mcp_servers]]
  alias = "world-hub"
  transport = "http"
  url = "https://your-aggregator.invalid/mcp"
  token_env = "WORLD_HUB_TOKEN"
  ```
- **Add a deep-work agent.** Any agent that speaks the Agent Client Protocol runs the deep work when `QAR_ACP_AGENT_COMMAND`
  names it (see [the ACP runner](acp-deep-runner.md)).
- **Reach any model an OpenAI-compatible gateway routes** (LiteLLM, vLLM, Ollama): `OPENAI_BASE_URL`, and
  `QAR_OPENAI_MODEL_PREFIXES` to route its model ids.
- **Add a search API** that answers JSON by describing it in the file named by `QAR_WEB_SEARCH_APIS_FILE` ([Web search](web-search.md),
  field list in the hub's `docs/search-api-entries.md`). One service per entry, so it is a convenience and not a hub connection.

## Whose tokens (decided)

Decision `teamdec_6255a298dc8a`, approved by Payal as recommended: the consuming product holds users' integration tokens, encrypted,
using the MCP-spec OAuth flow; a third-party aggregator is used only for apps with no official server. QAR itself keeps no
long-lived provider tokens: a lane receives a token through the environment variable named by `token_env`.

## How QAR uses the hub

- **Import.** `hubofhubs` is a required dependency (`pyproject.toml`). `quest_ai_runner/hub.py` imports it once, at startup, and
  registers the parts QAR owns under names QAR owns, only where the hub has a seat (Protocol) for them: today the card store, as
  `qar-card-repository` (a hub `CardStore` over QAR's `CardRepository`, `adapters/hub_card_store.py`). The model call and the
  web search backend are external seats that QAR passes in `provided`. For dev and tests a sibling checkout beside this
  repository works instead of an install (pytest `pythonpath`; the `hub_of_hubs` component also puts it on the path).
- **Seats moved.** `search-backend` (web search): `hubofhubs.websearch` holds the backends, cache, limiter, fetch and extraction, and the research adapter; QAR keeps the `search-the-web` rows, the `QAR_*` setting names and the one-call constructor (`adapters/web_search_backends.py`, `web_research.py`). Not yet measured after the move: the paid pass owns `metric-search-the-web` and `metric-know-the-world`.
- **Seat moved: `call-a-model` (the model-provider slice, 2026-10-30).** The vendor provider arms (Anthropic, Gemini, OpenAI, DeepSeek), the prefix router (`MultiProvider`), retry and outage helpers, family-name resolution and the prompt-layer helpers are `hubofhubs.models`; the vendor libraries are per-arm extras there and settings are constructor arguments. Which provider and model serves each tier is the hub's tier selector (`hubofhubs.tiers.choose`, data in `registry/tier_*.csv`: prices with official sources and dates, the Anthropic monthly allowance counted as cost, measurements); `core/model_registry.py` asks it for the providers that hold a key, and `QAR_MODEL_*` pins still win. QAR keeps `providers.py` (the environment variable names, the prefix table extended by `QAR_OPENAI_MODEL_PREFIXES`, the daily-limit message, spend tagging by prompt spec), `ClaudeCliProvider`, the tier names and `pricing.py` (bootstrap estimates read from the hub's price table). No measurement exists yet, so the selector holds its defaults; the paid pass owns `metric-call-a-model` and `metric-choose-a-model-tier` after the move.
- **Seat moved (partly): `remember-and-recall`, the retrieval arms (the vector and BM25 slice, 2026-10-30).** `hubofhubs.recall` holds the Qdrant vector store and its embedder factories, the read-only reader for a mounted store's points on another backend, the vector context assembler, the BM25 content store, the hybrid assembler and the card selection passes with their selection memo; `ContextAssemblerBase` and `VectorStoreBase` are `hubofhubs.ports.context`. The vendor libraries (`qdrant-client`, `fastembed`, `voyageai`, `bm25s`) are per-arm extras there, imported lazily, and nothing in the hub reads the environment. QAR keeps one thin binding per module at the same import path: the `qar_ctx` prefix, the `.quest-context/qdrant` folder, the `qar_point_class` seed-scope key (kept because its digest is in every stored point), the `QAR_*` names, the skip set, the `@implementation` declarations and the prompts as registered specs. Not moved, and why: `spec_selection` and `unified_search` declare QAR's solutions and prompt specs and read the ESOI specification index, which is esoi mechanics. Not yet measured after the move: the paid pass owns `metric-index-by-meaning` and `metric-select-context`.
- **Seat moved: `think-critically` (reply honesty slice, 2026-10-30).** `hubofhubs.critique` holds the link guard, link detection, the abridged-read (sufficiency) gate, specificity scoring and the keyed JSON-lines ledger. The link guard allows no in-app scheme by default, so QAR passes `app-task` for a policy file that names none (`core/orchestrator_judges.py`). Not moved, and why: the metric record of a deep goal is ESOI mechanics (reading the repository's records, running `esoi run`, keeping before and after values) and now lives in the esoi library (`esoi.spec_slice`, `esoi.metric_meter`, `esoi.metric_results`, `esoi.iteration`), not in the hub; `goal_iteration` and `goal_metrics` stay as QAR's arms and gate, and `spec_selection`, `reach_judge`, `answer_explanation` and `recipes` declare QAR prompt specs bound to QAR's records; the anticipation modules carry the cross-quest fence in their names and defaults.
- **Seat moved (partly): `act-in-the-world` (the Body slice, 2026-10-30).** The deep-work ports are `hubofhubs.ports.deep` (`DeepResult`, `ProgressEvent`, the `DeepRunner`, `FileWriter`, `OperationWriter` and `ProgressSink` Protocols, their bases, the `WRITE_SURFACE_*` and `FUTURE_CONTEXT_*` constants) and `hubofhubs.ports.events` (`Mode` and the `EVENT_*` vocabulary), plus `hubofhubs.ports.control` (`RunControl`). `hubofhubs.act` holds the arms `fast_edit` and `mcp_operations` (registered under the hub's `act-in-the-world` seat, none selected), the edit-block parser (vendored from Aider, attribution in the hub's `NOTICE`), the MCP protocol client (its `PATH` is a constructor argument; QAR hands it the injected process environment's), the keyed tool-call ledger and the per-turn execution record. QAR keeps the wiring: `config_resolvers.resolve_fast_edit_runner` and `resolve_mcp_write_runners` are the `run-deep-work` implementations `fast_edit` and `mcp_write` (they build the hub arms with a writer and `ModelRegistry`), and `MCPWriteAdapter` stays because it is the selected `write-through-mcp` implementation. Not moved, and why: `Escalation`, `EscalationSink` and the sink base carry a quest id and `open_decision_ids_for_quest`; `prompt_budget` and `managed_blocks` carry the context-updates block and its receipt wording; `attachments` registers QAR prompt specs; `tools*` is the Quest tool catalog.
- **Seat defined: `know-the-world`.** The hub now declares the seat as a Protocol (`describe`, `search`, `fetch` over `Observation`) with its web research adapter as the registered arm `web_research`; QAR's `web_with_support_check` stays a subclass declared under QAR's solution. No arm is selected.
- **Seat moved (partly): `act-in-the-world`, the deep-work runners (2026-10-30).** `hubofhubs.deep` holds the pure argv builder (`launch`), the frozen `GoalSettings` and its two collaborators (`settings`), the goal contract (`contract`), the reading of the worker's own session record (`session`), the one-shot and stream-json Claude Code runners (`subprocess_runner`, `stream_runner`) and the ACP runner (`acp_*`), registered as the hub arms `subprocess_goal_runner`, `stream_json_goal_runner` and `acp_deep_runner`. The hub reads no environment: every setting is an argument, and the standard liveness-review and headless-run prompt texts are the hub's. QAR keeps one thin binding per module at the same import path: `goal_settings()` (built from `core_settings()` on every call, with `usage_limit`, `prompt_budget`, the standing instructions and the requester environment), the `QAR-ESCALATED:` marker, the process-wide model translator, `QAR_TOOL_RUN_ID`, the `QAR_ACP_*` fallbacks, `AcpConfig.escalation_quest_id` with the `Escalation` value, and the `run-deep-work` declarations as subclasses. `usage_limit`, `prompt_budget` and `standing_instructions` stay QAR's (the lane-wide limit state, the context-updates wording and the instruction files) and are handed in. Not yet measured after the move: the paid pass owns `metric-run-deep-work`.
- **Seats moved (earlier).** `watch-for-risk`: the overseer engine is `hubofhubs.risk` (`overseer`); QAR passes its prompt as data and assembles the seat in `core/overseer.py`. Not yet measured after the move: the paid pass owns `metric-watch-a-run` (`overseer_signals`, `judges` and the live arms).
- **Ports.** The Protocols that need no Quest knowledge live in the hub seat (`hubofhubs.ports.{scope,model,context}`); the ones
  that carry QAR's own value objects and events stay in `core/adapters_*.py`. `core/adapters.py` re-exports only the names the
  Quest backend imports from it (`tests/test_core_adapters_public_names.py`); every module in this repository names the module that
  defines what it uses (`CHANGELOG.md`).
- **Bindings.** `esoi/hub_seats.csv` says which QAR solution fills each area QAR uses, the vendor terms to look for in `quest_ai_runner/core`
  and the code paths. The registry breaks each seat into sub-solution areas (72 on 2026-10-30), and QAR binds only the ones it has a
  seam for (11 today; the successors of the earlier `tools_mcp` are `mcp_client`, of `world` are `integration_aggregators` and `channels`,
  of `science` is `science_find_evidence`). An area not in the file is unbound: it has no seam and no probe, so the scorer reads it as
  not yet connected, but only an area whose `call` in the hub registry is adopt or coexist counts as a gap (seam, selectable, by configuration and fake-hub checks skip the keep, watch and avoid areas; QME9 reads the same cases). Binding another area means adding its row when QAR has a real seam for it. The hub's registry rows for this consumer (`quest-ai-runner` in `hub_capabilities.csv` and
  `hub_integrations.csv`) say which hub connections are wired and which single services are hand-wired.
- **Measure.** `esoi run hub_of_hubs --impl registry_scan` hands those bindings to the hub's scorer; it is sub-metric QME9 of
  `qar_overall`. The component folder holds only the binding, the probes (one per area, each proving a fake hub can be added
  through that area's configuration entry) and the thin scoring hand-off.

## Status

Generated by `python3 evaluation/components/hub_of_hubs/render_docs.py` from the hub's registry and the scorer's reading of QAR.

<!-- hub-table:begin (generated by evaluation/components/hub_of_hubs/render_docs.py; do not edit) -->
QAR binds 11 of the registry's 72 areas. Each area carries a `call` from its recommendation (adopt, coexist, keep, watch or avoid). The checks that need a connection (by configuration, one seam, selectable, a fake hub) count only the 42 areas called adopt or coexist; an area called keep, watch or avoid is not a gap. An unbound area that should be connected has no seam or probe, so the scorer reads it as not yet connected: that is a real gap (the hub's `docs/registry.md` lists every area).

| Seat | Areas | Bound by QAR | Should connect (adopt or coexist) | Real gaps (no seam) | Research stale | Hand-wired where a hub covers it |
|---|---|---|---|---|---|---|
| act-in-the-world | 20 | 2 | 9 | 8 | 0 | none |
| take-in-the-world | 7 | 3 | 3 | 2 | 0 | Google Chat, Google Drive, Notion |
| think-critically | 4 | 0 | 2 | 2 | 0 | none |
| watch-for-risk | 5 | 0 | 2 | 2 | 0 | none |
| know-the-world | 10 | 2 | 8 | 6 | 0 | none |
| hold-the-intents | 1 | 0 | 1 | 1 | 0 | none |
| stay-current | 3 | 1 | 2 | 2 | 0 | none |
| keep-it-generic | 3 | 0 | 3 | 3 | 0 | none |
| reason-and-answer | 6 | 1 | 4 | 3 | 0 | Anthropic, DeepSeek, Gemini, OpenAI |
| assemble-the-whole | 3 | 0 | 1 | 1 | 0 | none |
| remember-and-recall | 10 | 2 | 7 | 5 | 0 | none |

The bound areas:

| Area | Seat | Built on | Candidates | Researched | Freshness | Hand-wired where a hub covers it |
|---|---|---|---|---|---|---|
| channels | take-in-the-world | OpenClaw |  | 2026-10-30 | fresh | none |
| voice | take-in-the-world | none | Pipecat; LiveKit Agents; OpenAI Realtime API; faster-whisper; whisper.cpp; Deepgram; TEN Framework | 2026-10-30 | fresh | none |
| web_search | know-the-world | SearXNG, Serper, Brave Search API, Tavily, Gemini Google Search grounding | Exa; Parallel; Perplexity Search API; Brave LLM Context; Anthropic web search server tool | 2026-10-30 | fresh | none |
| evals | stay-current | esoi (evolvable-structure Python tools) | Inspect AI; promptfoo; DeepEval; DSPy and GEPA | 2026-10-30 | fresh | none |
| model_providers | reason-and-answer | none | Bifrost; OpenRouter; Vercel AI Gateway; Portkey gateway; any-llm; LiteLLM | 2026-10-30 | fresh | Anthropic, Gemini, OpenAI, DeepSeek |
| agent_harness | act-in-the-world | Claude Code CLI, Agent Client Protocol agents (claude-agent-acp) | Codex CLI; mini-swe-agent; Goose; OpenCode; OpenHands software-agent-sdk; Cline; Aider | 2026-10-30 | fresh | none |
| context_assembly | remember-and-recall | bm25s, Qdrant and fastembed, bge-small-en-v1.5 | Qdrant native weighted RRF hybrid; Qwen3-Embedding-0.6B; EmbeddingGemma-300m; contextual retrieval prefix; LanceDB; PyLate; Haystack; LlamaIndex | 2026-10-30 | fresh | none |
| memory | remember-and-recall | QAR card store and conversation recall | Graphiti; Mem0; Hindsight; Cognee; Letta; Supermemory; LangMem; A-MEM | 2026-10-30 | fresh | none |
| science_find_evidence | know-the-world | none | OpenAlex API; cyanheads/openalex-mcp-server; Asta Scientific Corpus MCP; Semantic Scholar API; PubMed MCP server; Europe PMC REST API; Crossref REST API; Elicit API v2; Undermind; Consensus hosted MCP; SYNERGY screening datasets | 2026-10-30 | fresh | none |
| integration_aggregators | take-in-the-world | none | Composio; Activepieces; Pipedream Connect; Nango; Arcade; Klavis AI; n8n; Zapier MCP | 2026-10-30 | fresh | Notion, Google Drive, Google Chat |
| mcp_client | act-in-the-world | Model Context Protocol Python SDK | FastMCP; Pydantic AI; Claude Code native MCP client | 2026-10-30 | fresh | none |

Reading of the hub's scorer on QAR's bindings:

- coverage (areas with a hub or a recorded decision): 1.00
- integration by configuration (share of wired hub connections that need no code): 0.38
- research freshness: share of areas researched within 90 days 1.00; stale: none
- reinvented integrations (single services hand-wired where a hub covers them, no waiver): 7 (3 more waived with a reason); areas free of them 0.97
- code: of the 42 areas whose call is adopt or coexist, share with one seam 0.17 and selectable by metric 0.14; vendor branches in core 0
- real gaps (call adopt or coexist, no seam): agent_orchestration, answer_verification, assembly_observability, background_memory, browser_use, code_context, command_sandbox, config, deep_research, embeddings, evals, inbound_mail, injection_defense, intents, judging, mcp_registry, model_pricing, observability, page_extraction, prompt_optimization, prompts, reranking, science_graph, science_provenance, science_read_papers, science_run_analysis, science_statistics, science_verify_claims, secrets_hygiene, structured_output, subagents, tool_observability, tool_sandbox, tool_schema, vendor_mcp
- score: 0.757
<!-- hub-table:end -->
