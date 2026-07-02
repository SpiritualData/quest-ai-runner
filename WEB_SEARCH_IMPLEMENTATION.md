# Web Search Implementation for quest-ai-runner

**Status:** IMPLEMENTED (2026-07-01). Web search now ships by default with NO extra key, via the
model provider's native tool (Claude `web_search` / Gemini Google Search grounding), in addition to
the Tavily path described below. Canonical user docs: [`docs/web-search.md`](docs/web-search.md).
The sections below are the original Tavily-only proposal, kept for history.

**Original status:** Proposal for extending quest-ai-runner with web search capability

**Date:** 2026-06-17

**Requester:** Joshua (via quicktask: "AI task asking for Amazon URL suggestion fails because no web search capability")

## The Problem

When users create AI tasks in Quest asking for web-based recommendations or current information (e.g., "suggest an Amazon product for X"), the task fails because:

1. The quest-ai-runner's orchestrator has no way to search the web
2. The default RetrievalAdapters (FilesAdapter, CachedDbAdapter) only access local/database sources
3. The LLM model may refuse to generate URLs without grounded, real-time data
4. There's no mechanism for the planner to discover and use web search as a data source

## Solution Overview

### Architecture

quest-ai-runner is designed to be **domain-free** with pluggable adapters. The core insight is that **web search is just another RetrievalAdapter** that implements the same interface as FilesAdapter and CachedDbAdapter.

**The adapter-based approach:**

```
RetrievalAdapter (Protocol)
├── FilesAdapter (local files)
├── CachedDbAdapter (live database)
├── QuestRetrievalAdapter (Quest data)
├── WebSearchAdapter (web search) ← NEW
└── CompositeRetrievalAdapter (combines all the above)
```

The CompositeRetrievalAdapter (already in quest-ai-runner) runs multiple RetrievalAdapters in parallel. The orchestrator's discovery system (list_sources / describe_source) already supports teaching the planner about available data sources.

### Implementation Plan

#### 1. Create WebSearchAdapter (`quest_ai_runner/adapters/web_search_adapter.py`)

**Key decisions:**

- **Web search service:** Use Tavily API (tavily.com)
  - Designed specifically for AI agents
  - Simple REST API with minimal setup
  - Supports semantic search
  - Free tier available (500 API calls/month)
  - Fallback: GoogleSearch API (via google-search-results) for organizations with existing credentials

- **Interface implementation:** WebSearchAdapter implements RetrievalAdapter protocol
  - `read_section(rel_path)` - interpret rel_path as a search query, return top N results
  - `grep(pattern)` - search for the pattern in web results
  - `query(spec)` - perform a structured search (e.g. {"search": "...", "max_results": 5})
  - `list_sources()` - advertise "web_search" as an available source
  - `describe_source()` - explain how to use web search
  - `list_operations()` - advertise available operations (search, filter_by_domain, etc.)
  - `describe_operation()` - explain operation signatures

- **Error handling:** Never raise; return Observation(kind="error", ...) on network failures
  
- **Rate limiting:** Configurable request throttling, retry logic

**Pseudocode:**

```python
class WebSearchAdapter(RetrievalAdapterBase):
    def __init__(self, api_key: str, *, max_results: int = 5, timeout: int = 10):
        self.api_key = api_key
        self.max_results = max_results
        self.timeout = timeout
    
    def read_section(self, rel_path, *, **kwargs) -> Observation:
        """Treat rel_path as a search query."""
        try:
            results = self._search(rel_path, self.max_results)
            text = self._format_results(results)
            return Observation(kind="read", rel_path=rel_path, 
                             locator="web_search", text=text)
        except Exception as e:
            return Observation(kind="error", error=str(e))
    
    def grep(self, pattern, **kwargs) -> Observation:
        """Search the web for pattern."""
        try:
            results = self._search(pattern, self.max_results * 2)  # Get more to filter
            hits = [{"url": r["url"], "title": r["title"], "snippet": r["snippet"]}
                   for r in results if pattern.lower() in r["snippet"].lower()]
            return Observation(kind="grep", pattern=pattern, hits=hits)
        except Exception as e:
            return Observation(kind="error", error=str(e))
    
    def query(self, spec: Dict[str, Any]) -> Observation:
        """Structured search: {"search": "...", "max_results": N, "domain_filter": "..."} """
        try:
            query = spec.get("search", "")
            max_results = spec.get("max_results", self.max_results)
            results = self._search(query, max_results)
            text = self._format_results(results)
            return Observation(kind="query", text=text, hits=[
                {"url": r["url"], "title": r["title"]} for r in results
            ])
        except Exception as e:
            return Observation(kind="error", error=str(e))
    
    def list_sources(self) -> Observation:
        return Observation(kind="query", locator="list_sources",
            text="web_search: real-time web search via Tavily API")
    
    def describe_source(self, name, **kwargs) -> Observation:
        if name != "web_search":
            return Observation(kind="error", error=f"source not found: {name}")
        return Observation(kind="query", locator="describe_source(web_search)",
            text="Web search source. Use read_section(query) or query({...}) to search.")
    
    def list_operations(self) -> Observation:
        return Observation(kind="query", locator="list_operations",
            text="- search(query): search the web for the query\n"
                 "- search_with_filters(query, domain, recency): advanced search")
    
    def _search(self, query: str, max_results: int) -> List[Dict]:
        """Call Tavily API and return results."""
        # POST to https://api.tavily.com/search
        # Returns: [{"url": "...", "title": "...", "snippet": "...", "score": 0.95}, ...]
        pass
    
    def _format_results(self, results: List[Dict]) -> str:
        """Format search results for the planner."""
        lines = []
        for r in results:
            lines.append(f"- {r['title']}")
            lines.append(f"  URL: {r['url']}")
            lines.append(f"  {r['snippet']}")
        return "\n".join(lines)
```

#### 2. Update RunnerConfig

Add optional web search configuration:

```python
@dataclass
class RunnerConfig:
    # ... existing fields ...
    
    # Optional web search adapter configuration
    web_search_api_key: Optional[str] = None    # Tavily API key (from env: WEB_SEARCH_API_KEY)
    web_search_enabled: bool = False             # Enable web search in composite retrieval
    web_search_max_results: int = 5              # Max results per query
```

#### 3. Update build_orchestrator() Factory

```python
def build_orchestrator(cfg: RunnerConfig) -> Orchestrator:
    """Build with web search if configured."""
    retrieval_adapters = []
    
    if cfg.retrieval:
        retrieval_adapters.append(cfg.retrieval)
    
    if cfg.web_search_enabled and cfg.web_search_api_key:
        web_search = WebSearchAdapter(
            api_key=cfg.web_search_api_key,
            max_results=cfg.web_search_max_results
        )
        retrieval_adapters.append(web_search)
    
    if len(retrieval_adapters) > 1:
        final_retrieval = CompositeRetrievalAdapter(retrieval_adapters)
    elif retrieval_adapters:
        final_retrieval = retrieval_adapters[0]
    else:
        final_retrieval = None
    
    # ... rest of orchestrator setup ...
```

#### 4. Update CLI and Environment

```bash
# .env for Spiritual Data's quest-ai-runner
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_xxxxx  # Tavily key
WEB_SEARCH_MAX_RESULTS=5
```

```python
# cli.py update
if os.getenv("WEB_SEARCH_ENABLED") == "true":
    cfg.web_search_enabled = True
    cfg.web_search_api_key = os.getenv("WEB_SEARCH_API_KEY")
    cfg.web_search_max_results = int(os.getenv("WEB_SEARCH_MAX_RESULTS", "5"))
```

#### 5. Documentation

Create `docs/web-search.md`:

```markdown
# Web Search in quest-ai-runner

The orchestrator can search the web using Tavily's API. When enabled, web search becomes 
another data source the planner can discover and use, just like files or database queries.

## Setup

1. Get a Tavily API key (free tier: https://tavily.com/api)
2. Set environment variable: `WEB_SEARCH_ENABLED=true WEB_SEARCH_API_KEY=tvly_...`
3. Restart the runner

## How the planner uses it

When enabled, the planner discovers web_search in `list_sources()` and can:
- Call `read_section("what is the best Amazon product for X?")` to search
- Call `query({"search": "...", "max_results": 5})` for structured queries
- Call `grep("pattern")` to find the pattern in web results

The planner automatically learns to use web search when:
- The user asks for current information ("latest prices", "today's news")
- The user asks for product recommendations ("best laptop for programming")
- The task requires real-time data not available locally

## Limitations

- Rate-limited: Tavily's free tier is 500 calls/month
- Network dependent: failures return graceful errors, never break the run
- Not for sensitive data: web search results are public
- Not for local-only tasks: web search adds ~1-2 seconds per search

## Example task

User: "Suggest a good Python book I can buy on Amazon"

Planner's decision: "I need current product recommendations. Call read_section('Python programming book Amazon')."
Result: Top Python books on Amazon with links and prices
Answer: Grounded recommendation with current pricing
```

### How It Integrates with Claude Code (Deep Runs)

When a task needs real work (e.g., to compare products, fetch detailed info), the orchestrator calls the DeepRunner with a `/goal` contract. Claude Code (the default SubprocessGoalRunner) **already has WebSearch and WebFetch tools available**, so deep runs have first-class web access.

**Example flow:**
1. Shallow task: "What's a good laptop?" → orchestrator tries to answer with local knowledge + web search → may escalate to deep
2. Deep task: "/goal Find the best budget laptop on Amazon under $500" → Claude Code runs with WebSearch/WebFetch tools → detailed research

No changes needed to the deep runner; web search there is already functional.

---

## Why This Works

1. **Generic adapter interface:** WebSearchAdapter is just another RetrievalAdapter. The orchestrator doesn't care which source it is.

2. **Composable:** CompositeRetrievalAdapter lets orgs mix and match sources (files + DB + web + etc.) in a single config.

3. **Discoverable:** The planner learns what sources exist by calling list_sources/describe_source. No hardcoded knowledge.

4. **Safe:** Adapters never raise; all errors are Observation(kind="error"). Network failures don't break the loop.

5. **Already designed for this:** The orchestrator's plan -> gather -> re-plan loop was designed to support any retrieval source.

---

## Implementation Checklist

- [ ] Create `quest_ai_runner/adapters/web_search_adapter.py`
- [ ] Add WebSearchAdapter tests
- [ ] Update `quest_ai_runner/config.py` with web_search_* fields
- [ ] Update `build_orchestrator()` factory to wire web search
- [ ] Update `quest_ai_runner/cli.py` to read web search env vars
- [ ] Create `docs/web-search.md` documentation
- [ ] Update `README.md` to mention web search as a feature
- [ ] Add example in `examples/` showing web search usage
- [ ] Update `CHANGELOG.md` with the new capability

---

## For Spiritual Data Specifically

**Personal lane (Joshua's quest-ai-runner):**
```bash
# ~/.claude/personal/.env
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_[Tavily key for personal use]
```

**Team runner (the cockpit's quest-ai-runner):**
```bash
# /home/joshua/hq/stories/spiritual_data/product/launch_code/quest-ai-runner/.env
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_[Tavily key for SD team]
WEB_SEARCH_MAX_RESULTS=5
```

Once configured, AI tasks can search the web. No code changes to the cockpit, no Quest API changes. Just a new adapter.

---

## Alternative Approaches Considered (and why we chose this)

### Option A: Hardcode web search in the orchestrator
**Why not:** Violates the generic boundary (rule #2 in CLAUDE.md). Would require all orgs to have the same web search service.

### Option B: Use Claude Code for all web searches
**Why not:** Overkill for simple queries; wastes the deep-runner for shallow info needs. Also, not all runners use Claude Code (some use subprocess, mock, etc.).

### Option C: Add web search to ModelProvider (let the LLM handle it)
**Why not:** The planner can't call tools; it only makes a structured decision. The orchestrator executes that decision via adapters.

### Why we chose Option A (WebSearchAdapter + CompositeRetrievalAdapter):
- **Follows the existing design:** The orchestrator was designed for pluggable retrieval sources.
- **Composable:** Works alongside local data, no forced replacement.
- **Discoverable:** The planner learns what's available, not hardcoded knowledge.
- **Reusable:** Any org can use it; any org can swap the service.
- **Proven:** The CompositeRetrievalAdapter is already in the codebase and tested.

---

## Open Questions for Joshua

1. **Web search service:** Tavily vs. GoogleSearch API vs. other? (Recommend Tavily for simplicity + cost)
2. **Free tier sufficient?** Tavily's 500 calls/month is enough for moderate use; we can discuss paid plans if needed.
3. **Rate limiting:** Should we throttle per-org, per-runner, or globally?
4. **Privacy:** Web search results are public; OK to store in logs/history?
