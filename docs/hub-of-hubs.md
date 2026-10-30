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
- **Seat moved: `think-critically` (reply honesty slice, 2026-10-30).** `hubofhubs.critique` holds the link guard, link detection, the abridged-read (sufficiency) gate, specificity scoring and the keyed JSON-lines ledger. The link guard allows no in-app scheme by default, so QAR passes `app-task` for a policy file that names none (`core/orchestrator_judges.py`). Not moved, and why: the metric record of a deep goal (`spec_slice`, `metric_meter`, `metric_results`, `goal_iteration`, `goal_metrics`) is ESOI mechanics (reading the repository's records, running `esoi run`, keeping before and after values) that belongs in the esoi library, not in the hub, and `spec_selection`, `reach_judge`, `answer_explanation` and `recipes` declare QAR prompt specs bound to QAR's records; the anticipation modules carry the cross-quest fence in their names and defaults.
- **Seat defined: `know-the-world`.** The hub now declares the seat as a Protocol (`describe`, `search`, `fetch` over `Observation`) with its web research adapter as the registered arm `web_research`; QAR's `web_with_support_check` stays a subclass declared under QAR's solution. No arm is selected.
- **Seats moved (earlier).** `watch-for-risk`: the overseer engine is `hubofhubs.risk` (`overseer`); QAR passes its prompt as data and assembles the seat in `core/overseer.py`. Not yet measured after the move: the paid pass owns `metric-watch-a-run` (`overseer_signals`, `judges` and the live arms).
- **Ports.** The Protocols that need no Quest knowledge live in the hub seat (`hubofhubs.ports.{scope,model,context}`); the ones
  that carry QAR's own value objects and events stay in `core/adapters_*.py`. `core/adapters.py` re-exports only the names the
  Quest backend imports from it (`tests/test_core_adapters_public_names.py`); every module in this repository names the module that
  defines what it uses (`CHANGELOG.md`).
- **Bindings.** `esoi/hub_seats.csv` says which QAR solution fills each area, the vendor terms to look for in `quest_ai_runner/core`
  and the code paths. The hub's registry rows for this consumer (`quest-ai-runner` in `hub_capabilities.csv` and
  `hub_integrations.csv`) say which hub connections are wired and which single services are hand-wired.
- **Measure.** `esoi run hub_of_hubs --impl registry_scan` hands those bindings to the hub's scorer; it is sub-metric QME9 of
  `qar_overall`. The component folder holds only the binding, the probes (one per area, each proving a fake hub can be added
  through that area's configuration entry) and the thin scoring hand-off.

## Status

Generated by `python3 evaluation/components/hub_of_hubs/render_docs.py` from the hub's registry and the scorer's reading of QAR.

<!-- hub-table:begin (generated by evaluation/components/hub_of_hubs/render_docs.py; do not edit) -->
| Area | Seat | Built on | Candidates | Researched | Freshness | Hand-wired where a hub covers it |
|---|---|---|---|---|---|---|
| agent_harness | act-in-the-world | Claude Code CLI, Agent Client Protocol agents (claude-agent-acp) | OpenCode; Goose; OpenHands; Codex CLI | 2026-10-29 | fresh | none |
| tools_mcp | act-in-the-world | Model Context Protocol Python SDK | Official MCP Registry; PulseMCP subregistry; GitHub MCP directory | 2026-10-29 | fresh | none |
| web_search | know-the-world | SearXNG, Serper, Brave Search API, Tavily, Gemini Google Search grounding | Firecrawl; Crawl4AI; Exa; self-hosted SearXNG with MCP | 2026-10-29 | fresh | none |
| world | take-in-the-world | none | Activepieces; Pipedream; Composio; n8n | 2026-10-29 | fresh | Notion, Google Drive, Google Chat |
| context_assembly | remember-and-recall | bm25s, Qdrant and fastembed | Haystack; LlamaIndex; LightRAG | 2026-10-29 | fresh | none |
| science | know-the-world | none | OpenAlex API; Semantic Scholar API; PaperQA2; community scholarly MCP servers | 2026-10-29 | fresh | none |
| model_providers | reason-and-answer | none | LiteLLM; Portkey gateway; OpenRouter | 2026-10-29 | fresh | Anthropic, Gemini, OpenAI, DeepSeek |
| memory | remember-and-recall | none | Mem0; Letta; Graphiti; Cognee | 2026-10-29 | fresh | none |
| evals | stay-current | esoi (evolvable-structure Python tools) | Inspect AI; promptfoo; DeepEval; Ragas | 2026-10-29 | fresh | none |
| voice | take-in-the-world | none | Pipecat; LiveKit Agents; TEN Framework | 2026-10-29 | fresh | none |

Reading of the hub's scorer on QAR's bindings:

- coverage (areas with a hub or a recorded decision): 1.00
- integration by configuration (share of wired hub connections that need no code): 0.38
- research freshness: share of areas researched within 90 days 1.00; stale: none
- reinvented integrations (single services hand-wired where a hub covers them, no waiver): 7 (3 more waived with a reason); areas free of them 0.80
- code: areas with one seam 0.80, selectable by metric 0.70, vendor branches in core 0
- score: 0.802
<!-- hub-table:end -->
