# Web search

`WebResearchAdapter` (`quest_ai_runner/adapters/web_research.py`) gives the orchestrator fast,
token-efficient access to the live web, the same way ChatGPT/Perplexity-style assistants stay
cheap: **snippet-first search, full-page reads only on request, everything cached.**

It is a small, deliberately asymmetric two-method surface, not a general `RetrievalAdapter`:

- `search(queries)` is cheap and always runs: title + URL + a trimmed (~240 char) snippet per
  hit, every hit citable inline as a markdown link. This is what rides in every planner/answer
  call that sees it, so it has to stay small.
- `fetch(url, focus)` is expensive and only runs when something explicitly asks to read one page
  in full. It extracts the page's main text and keeps only the passages relevant to `focus`,
  capped to a token budget (default ~800 tokens) -- never the whole page.

Neither method ever raises. Every failure comes back as `Observation(kind="error", error=...)`.

## Relationship to `WebSearchAdapter` / `ProviderWebSearchAdapter`

This repo also ships two older, simpler web-search `RetrievalAdapter`s: `WebSearchAdapter`
(Tavily) and `ProviderWebSearchAdapter` (the model provider's own native web search -- Claude's
`web_search` tool or Gemini grounding -- reusing the LLM key, no separate search key). Either one
is wired automatically today whenever a provider/key supports it, via the ordinary
`query`/`grep`/discovery `RetrievalAdapter` surface, and they still work exactly as before; nothing
here removes or changes them.

`WebResearchAdapter` is a separate, newer surface purpose-built for the planner's dedicated
`{"web": ...}` / `{"web_page": ...}` read shapes (see below), not a drop-in replacement: it adds
a pluggable multi-backend chain with fallback/cooldown, a real on-disk cache, an SSRF-checked page
fetch with focus-scored passage extraction, and a daily cost guard, all absent from the older
adapters. A consumer decides how the two coexist (e.g. `WebResearchAdapter` for the planner's
explicit web reads, falling back to or alongside the native/Tavily adapter for the general
retrieval stack) -- see `core/orchestrator.py`'s wiring for the decision actually shipped.

## The read shapes a consumer wires into its orchestrator

This library's `core/orchestrator.py` does not know about `WebResearchAdapter` -- a consumer
(or a sibling piece of `quest_ai_runner` wiring code) maps the planner's read-spec shapes onto
its two methods:

```python
# planner emits: {"web": "<query>"}  or  {"web": ["<query1>", "<query2>"]}
obs = web_research.search(spec["web"], fresh=spec.get("fresh", False))

# planner emits: {"web_page": "<url>", "focus": "<what you need>"}
obs = web_research.fetch(spec["web_page"], focus=spec.get("focus"), fresh=spec.get("fresh", False))
```

`fresh: true` bypasses the cache READ for that one call (it still writes the fresh result back
to the cache).

## Backends

Set via env (see `select_search_backend` in `adapters/web_search_backends.py` for the exact
selection order/logic):

| Backend | Env var(s) for the key/endpoint | Notes |
|---|---|---|
| Serper | `SERPER_API_KEY` | Google SERP proxy; fastest, cheapest dedicated option. |
| Brave Search API | `BRAVE_SEARCH_API_KEY` or `BRAVE_API_KEY` | Has a $5/month free credit. |
| Tavily | `TAVILY_API_KEY` (or legacy `WEB_SEARCH_API_KEY`) | Also used by `WebSearchAdapter`. |
| SearXNG | `SEARXNG_URL` | Self-hosted; no per-call API cost. |
| Gemini grounding | `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_AI_API_KEY` | Key-free beyond an LLM key you already have. |
| Provider-native | (needs `provider=` + `QAR_WEB_SEARCH_PROVIDER_MODEL`) | Wraps any `ModelProvider.web_search()` (e.g. `AnthropicProvider`). |

Selection:

- `WEB_SEARCH_ENABLED=false` (or `0`/`off`/`no`) disables web search entirely.
- `QAR_WEB_SEARCH_BACKEND=<name>` forces exactly one backend (`serper`, `brave`, `tavily`,
  `searxng`, `gemini`, `provider`). If its key/config is missing, web search is disabled (with a
  warning) rather than silently falling through to another backend.
- Otherwise ("auto"), every backend with its config present is collected, in the order in the
  table above, and wrapped in a `FallbackSearchBackend` when more than one is available: each
  search tries them in order, moving to the next on error or an empty response. A backend that
  returns 401/403/429 goes into a 60s cooldown so a failing primary isn't hammered on every
  subsequent search.

Rough cost per 1,000 searches (check each provider's current pricing before relying on this):
Serper ~$1/1k (down to ~$0.30/1k at volume); Brave $5/1k ($5/month free credit); Gemini 2.5
Flash-Lite grounding: 1,500 grounded prompts/day free, then ~$35/1k; Gemini 3.x grounding: 5,000
grounded prompts/month free, then ~$14/1k.

## Cache

`WebCache` (`adapters/web_cache.py`) is a thread-safe in-memory LRU with an optional on-disk tier
(one JSON file per key, atomic writes, pruned by age once the disk tier grows past
`max_disk_entries`). Search results and fetched page text are cached separately:

- Search key: `f"{backend_name}|{max_results}|{normalize_query(query)}"`.
  `normalize_query` lowercases, collapses whitespace, and strips surrounding punctuation/quotes
  so trivially-different phrasings of the same question share a cache entry.
- Page key: the canonical URL (fragment and `utm_*` params dropped). The FULL extracted page
  text is cached; passage selection for a given `focus` re-runs on every `fetch()` call, so two
  different `focus` values against the same cached page don't re-fetch or re-extract.

A failed search or fetch is never cached.

## Token budget

- Search results are trimmed before they're ever assembled: each snippet to ~240 chars, any
  backend-provided synthesized answer to ~400 chars. Five results plus a summary comfortably
  stays under ~600 estimated tokens (chars / 4).
- A fetched page is split into ~paragraph passages (merging tiny ones, splitting huge ones to
  ~120 words), scored against `focus` with a tiny from-scratch BM25 (no index, just the one
  page's passages), and the top-scoring passages are kept, in document order, up to
  `page_token_budget` (default 800). An empty `focus` keeps the leading passages instead of
  scoring.

## Env vars (tuning, read by `build_web_research_from_env`)

| Var | Default | Meaning |
|---|---|---|
| `WEB_SEARCH_MAX_RESULTS` | 5 | Max results per search call. |
| `QAR_WEB_PAGE_TOKEN_BUDGET` | 800 | Max tokens kept from one fetched page. |
| `QAR_WEB_SEARCH_TTL_SECONDS` | 86400 (1 day) | Search-result cache TTL. |
| `QAR_WEB_PAGE_TTL_SECONDS` | 604800 (7 days) | Fetched-page cache TTL. |
| `QAR_WEB_CACHE_DIR` | unset (memory-only) | On-disk cache directory. |
| `QAR_WEB_SEARCH_MODEL` | `gemini-2.5-flash-lite` | Gemini grounding model (and the fallback-fetch model). |
| `QAR_WEB_SEARCH_PROVIDER_MODEL` | unset | Model id for the `provider`-native backend option. |
| `QAR_WEB_SEARCH_DAILY_LIMIT` | unset (no limit) | Max REAL backend search calls per UTC day; see below. |

## Daily cost guard

`QAR_WEB_SEARCH_DAILY_LIMIT` caps the number of REAL backend search calls per UTC day (cache hits
never count against it). It's a cost guard, not a hard quota: each `search()` call checks the
counter before calling the backend and returns `Observation(kind="error", error="Web search
daily limit reached for this deployment; answer from what you know and say the information may
be out of date.")` once it's reached, without calling the backend. The count is in-memory by
default, or persisted as one small JSON file under `QAR_WEB_CACHE_DIR` (when that's configured)
so a process restart doesn't reset it mid-day. `fetch()` is never limited by this guard.

For a deployment on Gemini 2.5 Flash-Lite grounding (1,500 free grounded prompts/day), a sensible
value is something like **1400**, leaving headroom below the free quota for non-web-search
grounded calls.

## Extracting pages: built-in vs. `trafilatura`

`extract_main_text` (`adapters/web_page_extract.py`) tries `trafilatura` first if it happens to
be installed (it is an OPTIONAL dependency, never required), falling back to a small stdlib
`html.parser`-based extractor that drops `script`/`style`/`nav`/`header`/`footer`/`aside`/`form`/
`iframe` and anything whose `class`/`id` looks like navigation/ads/cookie-banner/etc., and
prefers `<article>`/`<main>` content when it's substantial. The built-in extractor has to be
good on its own -- `trafilatura` is a bonus, not a requirement.

A page fetch refuses non-`http(s)` schemes and any host (literal IP or resolved hostname) that is
loopback/private/link-local/reserved (`check_url_is_safe`) before opening a socket. A PDF (or
any other non-text content-type) comes back as an error suggesting a deep run instead. When
direct extraction yields too little text (a JS-rendered shell, a 403, a block), and a
`url_fetch_fallback` callable is configured, that's tried next -- `build_web_research_from_env`
wires one using Gemini's `url_context` tool (server-side fetch) whenever a Gemini key happens to
be configured, regardless of which backend is doing the searching.

## Plugging in a custom backend

Any object with a `name: str` attribute and a `search(query: str, *, max_results: int = 5) ->
SearchResponse` method (raising `SearchBackendError` on failure) satisfies the `SearchBackend`
protocol and can be passed straight to `WebResearchAdapter`:

```python
from quest_ai_runner.adapters import WebResearchAdapter, SearchHit, SearchResponse

class MyBackend:
    name = "my-backend"

    def search(self, query, *, max_results=5):
        hits = [SearchHit(title="...", url="...", snippet="...", date="...")]
        return SearchResponse(hits=hits, answer="", queries_issued=[query])

web_research = WebResearchAdapter(MyBackend())
```

Wrap it with other backends in a `FallbackSearchBackend([...])` for automatic fallback chaining,
the same way `select_search_backend`'s "auto" mode does.
