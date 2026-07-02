# Web search in quest-ai-runner

The runner can ground answers on the **live web** as a standard retrieval source, so ordinary AI
tasks ("find marathon events near Portland", "suggest a product under $50", "what's the current
price of X") return current, cited results instead of "I couldn't find anything".

There are two ways web search is provided, and the runner picks the best available automatically:

## 1. Native provider web search (default, no extra key)

**This is the default.** It reuses the same LLM API key the runner already has, so there is nothing
extra to configure and no external environment or Claude Code subprocess is needed:

- **Anthropic** (`AnthropicProvider`) uses Claude's built-in `web_search` server tool.
- **Gemini** (`GeminiProvider`) uses Google Search grounding.

`build_orchestrator` detects `provider.supports_web_search()` and wires a
`ProviderWebSearchAdapter` into the retrieval stack. The planner then discovers `web` as a source
(via `list_sources` / `describe_source`) and calls it like any other retrieval source. A single call
returns a synthesized answer plus the source pages (title + URL), which is fast and token-efficient.

To **opt out**, set `WEB_SEARCH_ENABLED=false`.

To pin which model tier runs the search (default `balanced`), set `WEB_SEARCH_TIER=fast|balanced|quality|best`.
Cap the sources returned with `WEB_SEARCH_MAX_RESULTS` (default 5).

## 2. Tavily web search (optional, needs a key)

If you prefer a dedicated search API, set a [Tavily](https://tavily.com) key and it takes
precedence over the native path:

```bash
WEB_SEARCH_API_KEY=tvly_xxx        # enables the Tavily WebSearchAdapter
WEB_SEARCH_MAX_RESULTS=5           # optional
```

Tavily additionally supports fetching a single page's full text via `read_section(url)`; the native
path synthesizes across pages instead, so it does not fetch a single URL.

## How the planner uses it

Both adapters expose the same `RetrievalAdapter` surface, so the planner treats `web` uniformly:

- `query({"q": "search terms", "max_results": 5, "scope": "site:example.com"})` — search + synthesize.
- `grep("search terms", scope="site:example.com")` — return matching pages (title + URL).

The planner emits varied/nested query shapes; `adapters/web_query_spec.py` (`coerce_web_query`)
flattens them, so `{"query": {"operation": "web_search", "params": {"query": "..."}}}` and
`{"q": "..."}` both work.

## Capability reporting

`derive_capabilities()` reports `web: true` when a web adapter (native or Tavily) is wired, or when
the deep runner (Claude Code) can browse. The runner's heartbeat carries this so the routing
classifier knows this runner can do web research.

## Safety

Every web method is graceful: network failures, an unconfigured key, or a provider without web
support return an `Observation(kind="error", ...)` and never raise, so a failed search degrades to a
non-web answer rather than breaking the run.
