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
`web_search` tool or Gemini grounding -- reusing the LLM key, no separate search key). Both still
work if a consumer wires them, but `build_orchestrator` no longer reaches for them first: the
`WebSearchAdapter` (Tavily) fold-in was removed from `cli.py` (Tavily is now a backend here), and
the `ProviderWebSearchAdapter` fold-in into `CompositeRetrievalAdapter` is only a FALLBACK for the
case where no `WebResearchAdapter` could be built, so no deployment loses web capability.

`WebResearchAdapter` is a separate, newer surface purpose-built for the planner's dedicated
`{"web": ...}` / `{"web_page": ...}` read shapes (see below), not a drop-in replacement: it adds
a pluggable multi-backend chain with fallback/cooldown, a real on-disk cache, an SSRF-checked page
fetch with focus-scored passage extraction, and a daily cost guard, all absent from the older
adapters. A consumer decides how the two coexist (e.g. `WebResearchAdapter` for the planner's
explicit web reads, falling back to or alongside the native/Tavily adapter for the general
retrieval stack) -- see `core/orchestrator.py`'s wiring for the decision actually shipped.

## The read shapes, and how they are wired

`core/orchestrator.py` dispatches these two read shapes itself, through the generic
`core/adapters.WebResearch` Protocol (`Orchestrator.web`) -- never through
`CompositeRetrievalAdapter`, which broadcasts every `grep`/`query` to every member adapter and so
would fire a paid web search on an ordinary corpus read. `build_orchestrator` sets
`Orchestrator.web` from `RunnerConfig.web_research`, which it auto-builds with
`build_web_research_from_env` when the consumer left it unset (an adapter the consumer DID set is
never overwritten). The dispatch in `Orchestrator._exec_one_read` is, in effect:

```python
# planner emits: {"web": "<query>"}  or  {"web": ["<query1>", "<query2>"]}
obs = web_research.search(spec["web"], fresh=spec.get("fresh", False))

# planner emits: {"web_page": "<url>", "focus": "<what you need>"}
obs = web_research.fetch(spec["web_page"], focus=spec.get("focus"), fresh=spec.get("fresh", False))
```

With no web adapter wired, the planner is told nothing about the web (no WEB prompt block, no
`web`/`web_page`/`focus`/`fresh` fields in the decide schema), so the prompt is byte-for-byte what
it was before this feature existed; a stray `{"web": ...}` spec from a model that invented one
comes back as a named "not configured" error rather than a crash or a silent drop.

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
| `QAR_WEB_SEARCH_DAILY_LIMIT` | unset (no limit) | Max REAL, paid calls per UTC day (searches plus fallback fetches); see below. |

## Daily cost guard

`QAR_WEB_SEARCH_DAILY_LIMIT` caps the number of REAL, PAID calls per UTC day: backend searches
and `url_fetch_fallback` (Gemini `url_context`) fetches that are actually issued. Cache hits and
direct HTML fetches stay free and never count. It's a cost guard, not a hard quota: each
`search()` call checks the counter before calling the backend and returns `Observation(kind=
"error", error="Web search daily limit reached for this deployment; answer from what you know
and say the information may be out of date.")` once it's reached, without calling the backend.
`fetch()` checks the same counter before calling the fallback: once reached, it skips the
fallback and keeps whatever the free direct fetch got (a thin page is still returned), or, if
the direct fetch got nothing at all, returns an error naming the limit as the cause ("Page needs
a rendering fetch, but the web daily limit is reached for this deployment.") -- never a raise.
The count is in-memory by default, or persisted as one small JSON file under `QAR_WEB_CACHE_DIR`
(when that's configured) so a process restart doesn't reset it mid-day.

The counter is shared by every process that points at the same `QAR_WEB_CACHE_DIR`: `record()`
takes a cross-process advisory file lock, re-reads the persisted count, rolls the day if it's
stale, increments, and writes back atomically, so two runner lanes, a terminal session, and a
web backend all sharing one directory add up to one real spend instead of each keeping (and
undercounting against) its own in-memory copy. `exhausted()` re-reads the file too, so one
process sees another's spend without needing its own call to trip the limit. **Set ONE cache
dir per API key/quota** -- the limit is enforced per `QAR_WEB_CACHE_DIR`, not per process, so
giving two independent quotas the same directory would wrongly cap them together, and giving one
quota two directories would let it spend twice. Without `QAR_WEB_CACHE_DIR` (memory-only cache),
the counter is back to per-process, same as before. On a platform without `fcntl` (Windows), or
if the lock can't be acquired within ~2s, the guard degrades to per-process counting for that
call rather than blocking the turn; it is a cost guard, not a quota enforcer.

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

When that structured pass yields under ~200 characters, a salvage pass runs instead: script/style
blocks removed, block tags turned into breaks, remaining tags stripped. It exists because real
HTML frequently never closes a chrome element (and a document truncated at the 2 MB read cap never
closes anything), which used to leave the chrome-skipping parser skipping the entire rest of the
page and returning nothing at all.

A page fetch refuses non-`http(s)` schemes and any host (literal IP or resolved hostname) that is
loopback/private/link-local/reserved (`check_url_is_safe`) before opening a socket, and refuses it
again on **every redirect hop** (the default fetcher follows redirects by hand, up to 5, so a
public URL cannot 302 the fetch onto `127.0.0.1` or a cloud metadata endpoint). Page bytes are
decoded with the charset the response header or the document's own `<meta>` declares, falling back
to UTF-8 with replacement. A PDF (or
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
