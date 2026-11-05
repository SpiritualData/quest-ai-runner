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
  names it (see [the ACP runner](../how-to/acp-deep-runner.md)).
- **Reach any model an OpenAI-compatible gateway routes** (LiteLLM, vLLM, Ollama): `OPENAI_BASE_URL`, and
  `QAR_OPENAI_MODEL_PREFIXES` to route its model ids.
- **Add a search API** that answers JSON by describing it in the file named by `QAR_WEB_SEARCH_APIS_FILE` ([Web search](../how-to/web-search.md),
  field list in the hub's `docs/reference/search-api-entries.md`). One service per entry, so it is a convenience and not a hub connection.

## Whose tokens (decided)

Decision `teamdec_6255a298dc8a`, approved by Payal as recommended: the consuming product holds users' integration tokens, encrypted,
using the MCP-spec OAuth flow; a third-party aggregator is used only for apps with no official server. QAR itself keeps no
long-lived provider tokens: a lane receives a token through the environment variable named by `token_env`.

## How QAR uses the hub

- **Import.** `hubofhubs` is a required dependency (`pyproject.toml`). `quest_ai_runner/the_self/assembler/hub.py` imports it once, at startup, and
  registers the parts QAR owns under names QAR owns, only where the hub has a seat (Protocol) for them: today the card store, as
  `qar-card-repository` (a hub `CardStore` over QAR's `CardRepository`, `subconscious/hub_card_store.py`). The web search
  backend is an external seat that QAR passes in `provided`; its model seat is its own router arm of `vendor-model-caller`. For dev and tests a sibling checkout beside this
  repository works instead of an install (pytest `pythonpath`; the `hub_of_hubs` component also puts it on the path).
- **Seats moved.** `web-search` (web search): `hubofhubs.the_world.tools.web_search` holds the backends, cache, limiter, fetch and extraction, and the research adapter; QAR keeps the `search-the-web` rows, the `QAR_*` setting names and the one-call constructor (`science/web_search_backends.py`, `web_research.py`). Not yet measured after the move: the paid pass owns `metric-search-the-web` and `metric-know-the-world`.
- **Seat moved: `vendor-model-caller` (the model-provider slice, 2026-10-30).** The vendor provider arms (Anthropic, Gemini, OpenAI, DeepSeek), the prefix router (`MultiProvider`), retry and outage helpers, family-name resolution and the prompt-layer helpers are `hubofhubs.the_world.model_plumbing.vendor_model_caller`; the vendor libraries are per-arm extras there and settings are constructor arguments. Which provider and model serves each tier is the hub's tier selector (`hubofhubs.the_world.model_plumbing.model_tier_chooser.tiers.choose`, data in `registry/tier_*.csv`: prices with official sources and dates, the Anthropic monthly allowance counted as cost, measurements); `core/model_registry.py` asks it for the providers that hold a key, and `QAR_MODEL_*` pins still win. QAR keeps `providers.py` (the environment variable names, the prefix table extended by `QAR_OPENAI_MODEL_PREFIXES`, the daily-limit message, spend tagging by prompt spec), `ClaudeCliProvider` (QAR's binding of the hub's keyless `claude_cli` arm, now the hub's default for the seat), the tier names and `pricing.py` (bootstrap estimates read from the hub's price table). No measurement exists yet, so the selector holds its defaults; the paid pass owns `metric-call-a-model` and `metric-choose-a-model-tier` after the move.
- **Seat moved (partly): `the-subconscious`, the retrieval arms (the vector and BM25 slice, 2026-10-30).** `hubofhubs.the_intelligence.subconscious.context_retriever` (`assemblers`, `context_cards`) and `hubofhubs.the_body.internal_context.card_store` hold the Qdrant vector store and its embedder factories, the read-only reader for a mounted store's points on another backend, the vector context assembler, the BM25 content store, the hybrid assembler and the card selection passes with their selection memo; `ContextAssemblerBase` and `VectorStoreBase` are `hubofhubs.the_body.internal_context.context`. The vendor libraries (`qdrant-client`, `fastembed`, `voyageai`, `bm25s`) are per-arm extras there, imported lazily, and nothing in the hub reads the environment. QAR keeps one thin binding per module at the same import path: the `qar_ctx` prefix, the `.quest-context/qdrant` folder, the `qar_point_class` seed-scope key (kept because its digest is in every stored point), the `QAR_*` names, the skip set, the `@implementation` declarations and the prompts as registered specs. Not moved, and why: `spec_selection` and `unified_search` declare QAR's solutions and prompt specs and read the ESOI specification index, which is esoi mechanics. Not yet measured after the move: the paid pass owns `metric-index-by-meaning` and `metric-select-context`.
- **Seat moved: `critical-thinking` (reply honesty slice, 2026-10-30).** `hubofhubs.the_intelligence.critical_thinking` (`response_quality`, `goal_completion`) holds the link guard, link detection, the abridged-read (sufficiency) gate, specificity scoring and the keyed JSON-lines ledger. The link guard allows no in-app scheme by default, so QAR passes `app-task` for a policy file that names none (`core/orchestrator_judges.py`). Not moved, and why: the metric record of a deep goal is ESOI mechanics (reading the repository's records, running `esoi run`, keeping before and after values) and now lives in the esoi library (`esoi.spec_slice`, `esoi.metric_meter`, `esoi.optimization.keep_metric_record.metric_results`, `esoi.iteration`), not in the hub; `goal_iteration` and `goal_metrics` stay as QAR's arms and gate, and `spec_selection`, `reach_judge`, `answer_explanation` and `recipes` declare QAR prompt specs bound to QAR's records; the anticipation modules carry the cross-quest fence in their names and defaults.
- **Seat moved (partly): the act-in-the-world slice, now `file-editing`, `connected-system-operations` and `delegated-agent-sessions` (the Body slice, 2026-10-30).** The deep-work ports are `hubofhubs.the_world.tools.deep` (`DeepResult`, `ProgressEvent`, the `DeepRunner`, `FileWriter`, `OperationWriter` and `ProgressSink` Protocols, their bases, the `WRITE_SURFACE_*` and `FUTURE_CONTEXT_*` constants) and `hubofhubs.the_world.tools.events` (`Mode` and the `EVENT_*` vocabulary), plus `hubofhubs.the_world.tools.delegated_agent_sessions.control` (`RunControl`). `hubofhubs.the_world.tools` holds the arms `fast_edit` and `mcp_operations` (registered under the hub's `file-editing` and `connected-system-operations` seats, the retired `act-in-the-world` seat's successors), the edit-block parser (vendored from Aider, attribution in the hub's `NOTICE`), the MCP protocol client (its `PATH` is a constructor argument; QAR hands it the injected process environment's), the keyed tool-call ledger and the per-turn execution record. QAR keeps the wiring: `config_resolvers.resolve_fast_edit_runner` and `resolve_mcp_write_runners` are the `run-deep-work` implementations `fast_edit` and `mcp_write` (they build the hub arms with a writer and `ModelRegistry`), and `MCPWriteAdapter` stays because it is the selected `write-through-mcp` implementation. Not moved, and why: `Escalation`, `EscalationSink` and the sink base carry a quest id and `open_decision_ids_for_quest`; `prompt_budget` and `managed_blocks` carry the context-updates block and its receipt wording; `attachments` registers QAR prompt specs; `tools*` is the Quest tool catalog.
- **Seat defined: `the-science`.** The hub now declares the seat as a Protocol (`describe`, `search`, `fetch` over `Observation`) with its web research adapter as the registered arm `web_research`; QAR's `web_with_support_check` stays a subclass declared under QAR's solution. No arm is selected.
- **Seat moved (partly): `the-mind`, the loop's generic leaves (2026-10-30).** `hubofhubs.the_intelligence.mind` (`planner`, `delegation`, `context_intake`, `response_writer`) holds staged plans (`staged_plan`), the default bounds and the two structural result checks (`limits`), status text helpers (`text`), `PlanDecision` (`plan`), the cascade's escalation rule and review folding (`cascade`), tool-schema compaction and closed-vocabulary normalizing (`schema`), the per-turn context cache and event router (`turn`) and the overseer wake-up gate (`watch`). The old QAR modules keep their import paths and names as bindings; prompt text, tool vocabularies and settings are passed in or stay here. Classification of the rest of the orchestrator, module by module. QAR-specific (prompt text or a Quest/QAR vocabulary the loop reads inline, so moving them is a rewrite, not a move): `planner_prompt`, `auxiliary_prompts`, `planner_doctrine`, `planner_context` (renders QAR's own observation, link and escalation wording), `orchestrator_decide_tool` (the decide schema is the planner's contract with QAR's rubric), `orchestrator_decisions` (normalizes against QAR's tool names and write-surface text), `orchestrator_types` (`OrchestratorConfig` carries QAR's settings), `orchestrator_capabilities`, `orchestrator_deep_*`, `orchestrator_judges`, `orchestrator_cards`, `orchestrator_recipes`, `orchestrator_understanding`, `orchestrator_preflight`. `orchestrator_staged_plan`, `orchestrator_constants`, `orchestrator_turn` and `planner_cascade` are now partly bindings of the hub. Generic in shape but not moved yet, because the loop still reads QAR's prompts, config and `Orchestrator` attributes through them and a safe move needs the prompts and settings injected through a binding first (a rewrite, not a zero-change move): the mixins `orchestrator_planning`, `_answering`, `_reads`, `_oversee`, `_run`, `_run_begin`, `_run_loop`, `_run_answer`, `_run_close`. Moved since, with the prompts and wording injected: `orchestrator_oversee` (`hubofhubs.the_intelligence.mind.delegation.overseer.oversee`, QAR supplies four hooks), `orchestrator_answering` (`hubofhubs.the_intelligence.mind.response_writer.answer`, QAR supplies an `AnswerKit`) and the phase driver of `orchestrator_run` (`hubofhubs.the_intelligence.mind.phases`). Left, each with its reason. `orchestrator_reads`: the flow is bound to QAR's read-spec vocabulary (cards, tools, web, guidance, discovery) with about twenty inline observation sentences, to `ToolContext` and the tool registry in `core/tools.py`, and to `core/tools_status.py`, whose `classify_payload` encodes the Quest backend's answer shape (HTTP 202, `pending_approval`, `decision_id`), so it moves only when the tool types do. `orchestrator_planning`: it assembles the planner prompt from QAR's prompt modules under about forty `cfg` switches, which is consumer prompt content by the hub's rule. `orchestrator_run_begin`, `_run_loop`, `_run_answer` and `_run_close`: the phases share one run-local namespace `rs` of 40 to 66 fields each and interleave QAR decisions (quest ids, decision requests, milestones, card threads, personas) with the control flow, so a move is a rewrite of the phase boundaries and cannot be done with zero behaviour change in a slice. They are not flagged by Q3 (they carry Quest-domain words), so there is no waiver row; unconfirmed: for Payal to confirm.
- **Seat moved (partly): the deep-work runners, now `delegated-agent-sessions` (2026-10-30).** `hubofhubs.the_world.tools.delegated_agent_sessions` holds the pure argv builder (`launch`), the frozen `GoalSettings` and its two collaborators (`settings`), the goal contract (`contract`), the reading of the worker's own session record (`session`), the one-shot and stream-json Claude Code runners (`subprocess_runner`, `stream_runner`) and the ACP runner (`acp_*`), registered as the hub arms `subprocess_goal_runner`, `stream_json_goal_runner` and `acp_deep_runner` of `delegated-agent-sessions`. The hub reads no environment: every setting is an argument, and the standard liveness-review and headless-run prompt texts are the hub's. QAR keeps one thin binding per module at the same import path: `goal_settings()` (built from `core_settings()` on every call, with `usage_limit`, `prompt_budget`, the standing instructions and the requester environment), the `QAR-ESCALATED:` marker, the process-wide model translator, `QAR_TOOL_RUN_ID`, the `QAR_ACP_*` fallbacks, `AcpConfig.escalation_quest_id` with the `Escalation` value, and the `run-deep-work` declarations as subclasses. `usage_limit`, `prompt_budget` and `standing_instructions` stay QAR's (the lane-wide limit state, the context-updates wording and the instruction files) and are handed in. Not yet measured after the move: the paid pass owns `metric-run-deep-work`.
- **Generic helpers moved (Q3 backlog, 2026-10-30).** `hubofhubs.the_world.tools.secret_redaction` (QAR's `adapters/files_redaction.py` adds only the `qsk_` Quest key shape through `extra_patterns`), `hubofhubs.the_world.model_plumbing.signature_probe` and `hubofhubs.the_intelligence.subconscious.cards_from_files.conversation_card_builder` are the hub's, and so are the prompt-spec mechanics (`hubofhubs.the_world.model_plumbing.prompt_spec`; `core/prompt_specs.py` binds QAR's tiers and usage log into it); the old QAR import paths are thin bindings. Stayed in QAR and waived with a reason in `esoi/waivers.csv`: `cli_serve_files` (reads `QAR_FILES_*` from the environment), the terminal UI modules (`textual_ui_prompt`, `textual_ui_transcript`, `interactive_session_render`). The `interactive_session_store` re-export was deleted (importers name `adapters.conversation_session_store`).
- **Seats moved (earlier).** `nervous-system`: the overseer engine is `hubofhubs.the_intelligence.mind.delegation.overseer.risk` (`overseer`); QAR passes its prompt as data and builds `RunOverseer` (a helper of Delegation, no longer an arm of `nervous-system`) in `core/overseer.py`. Not yet measured after the move: the paid pass owns `metric-watch-a-run` (`overseer_signals`, `judges` and the live arms).
- **Ports.** The Protocols that need no Quest knowledge live in the hub seat (`hubofhubs.the_world.model_plumbing.model`, `hubofhubs.the_body.internal_context.context` and the `scope` module of Internal Context); the ones
  that carry QAR's own value objects and events stay in `core/adapters_*.py`. `core/adapters.py` re-exports only the names the
  Quest backend imports from it (`tests/test_core_adapters_public_names.py`); every module in this repository names the module that
  defines what it uses (`CHANGELOG.md`).
- **Bindings.** The registry's 72 areas are research categories, mostly arms or tools inside the hub's 12 top-level seats (`seat` in the hub's `hubs.csv`; 11 own areas). The hub's scorer checks seam, selectable, by configuration, vendor branches and fake hub once per seat, so QAR does not bind or wire areas one by one: `esoi/hub_seats.csv` says which QAR solution fills a seat's arms, the vendor terms to look for in `quest_ai_runner/core` and the code paths, and its rows are unchanged. An adopt or coexist area no QAR arm covers is an arm gap (listed below), meaning research to build or connect through the hub's defaults, not a missing QAR binding. The hub's registry rows for this consumer (`quest-ai-runner` in `hub_capabilities.csv` and `hub_integrations.csv`) say which hub connections are wired and which single services are hand-wired.
- **Measure.** `esoi run hub_of_hubs --impl registry_scan` hands those bindings to the hub's scorer; it is sub-metric QME9 of
  `qar_overall`. The component folder holds only the binding, the probes (one per area, each proving a fake hub can be added
  through that area's configuration entry) and the thin scoring hand-off.

## Status

Generated by `python3 evaluation/components/hub_of_hubs/render_docs.py` from the hub's registry and the scorer's reading of QAR.

<!-- hub-table:begin (generated by evaluation/components/hub_of_hubs/render_docs.py; do not edit) -->
The registry's 74 areas are research categories, mostly arms or tools inside the hub's seats; the seam, selectable, by-configuration and fake-hub checks are made once per seat, and only for the 11 seats with an adopt or coexist area. QAR's arms cover 11 areas today. An adopt or coexist area no arm covers is an arm gap: research to build or connect through the hub's defaults, not a missing binding (the hub's `docs/reference/registry.md` lists every area).

| Seat | Areas | Should connect | Arms cover | Arm gaps | Seam | Selectable | By config | Fake hub |
|---|---|---|---|---|---|---|---|---|
| delegated-agent-sessions | 8 | 4 | 1 | 3 | yes | yes | yes | yes |
| connected-system-operations | 10 | 5 | 1 | 5 | yes | yes | yes | yes |
| file-editing | 2 | 0 | 0 | 0 | - | - | - | - |
| the-world | 7 | 3 | 3 | 2 | yes | yes | no | yes |
| response-quality | 5 | 2 | 0 | 2 | no | no | no | no |
| nervous-system | 5 | 2 | 0 | 2 | no | no | no | no |
| the-science | 10 | 8 | 2 | 6 | yes | yes | no | yes |
| the-mind | 8 | 5 | 1 | 4 | yes | yes | yes | yes |
| the-learning | 3 | 2 | 1 | 1 | yes | yes | no | yes |
| system-integrity | 3 | 3 | 0 | 3 | no | no | no | no |
| the-assembler | 3 | 1 | 0 | 1 | no | no | no | no |
| the-subconscious | 10 | 7 | 2 | 5 | yes | yes | no | yes |

The areas QAR's arms cover:

| Area | Seat | Built on | Candidates | Researched | Freshness | Hand-wired where a hub covers it |
|---|---|---|---|---|---|---|
| channels | the-world | OpenClaw |  | 2026-10-30 | fresh | none |
| voice | the-world | none | Pipecat; LiveKit Agents; OpenAI Realtime API; faster-whisper; whisper.cpp; Deepgram; TEN Framework | 2026-10-30 | fresh | none |
| web_search | the-science | SearXNG, Serper, Brave Search API, Tavily, Gemini Google Search grounding | Exa; Parallel; Perplexity Search API; Brave LLM Context; Anthropic web search server tool | 2026-10-30 | fresh | none |
| evals | the-learning | esoi (evolvable-structure Python tools) | Inspect AI; promptfoo; DeepEval; DSPy and GEPA | 2026-10-30 | fresh | none |
| model_providers | the-mind | none | Bifrost; OpenRouter; Vercel AI Gateway; Portkey gateway; any-llm; LiteLLM | 2026-10-30 | fresh | Anthropic, Gemini, OpenAI, DeepSeek |
| agent_harness | delegated-agent-sessions | Claude Code CLI, Agent Client Protocol agents (claude-agent-acp) | Codex CLI; mini-swe-agent; Goose; OpenCode; OpenHands software-agent-sdk; Cline; Aider | 2026-10-30 | fresh | none |
| context_assembly | the-subconscious | bm25s, Qdrant and fastembed, bge-small-en-v1.5 | Qdrant native weighted RRF hybrid; Qwen3-Embedding-0.6B; EmbeddingGemma-300m; contextual retrieval prefix; LanceDB; PyLate; Haystack; LlamaIndex | 2026-10-30 | fresh | none |
| memory | the-subconscious | QAR card store and conversation recall | Graphiti; Mem0; Hindsight; Cognee; Letta; Supermemory; LangMem; A-MEM | 2026-10-30 | fresh | none |
| science_find_evidence | the-science | none | OpenAlex API; cyanheads/openalex-mcp-server; Asta Scientific Corpus MCP; Semantic Scholar API; PubMed MCP server; Europe PMC REST API; Crossref REST API; Elicit API v2; Undermind; Consensus hosted MCP; SYNERGY screening datasets | 2026-10-30 | fresh | none |
| integration_aggregators | the-world | none | Composio; Activepieces; Pipedream Connect; Nango; Arcade; Klavis AI; n8n; Zapier MCP | 2026-10-30 | fresh | Notion, Google Drive, Google Chat |
| mcp_client | connected-system-operations | Model Context Protocol Python SDK | FastMCP; Pydantic AI; Claude Code native MCP client | 2026-10-30 | fresh | none |

Reading of the hub's scorer on QAR:

- coverage (areas with a hub or a recorded decision): 1.00
- integration by configuration (share of wired hub connections that need no code): 0.38
- research freshness: share of areas researched within 90 days 1.00; stale: none
- reinvented integrations (single services hand-wired where a hub covers them, no waiver): 7 (3 more waived with a reason); areas free of them 0.97
- code, once per seat: of the 11 seats with an adopt or coexist area, share with one seam 0.64 and selectable by metric 0.64; vendor branches in core 0
- seats with no seam: nervous-system, response-quality, system-integrity, the-assembler
- arm gaps (adopt or coexist areas no arm covers; research to build or connect, not missing bindings): 34 of 42
- score: 0.821
<!-- hub-table:end -->
