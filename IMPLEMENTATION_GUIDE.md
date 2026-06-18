# WebSearchAdapter Implementation Guide

This guide provides the detailed code structure for adding web search to quest-ai-runner.

## File Structure

```
quest_ai_runner/
├── adapters/
│   ├── web_search_adapter.py          (NEW - 200 lines)
│   ├── retry_utils.py                  (use existing, or import requests.adapters)
│   └── ...existing adapters...
├── config.py                           (update +20 lines)
├── cli.py                              (update +10 lines)
├── core/
│   └── orchestrator.py                 (no changes needed)
└── docs/
    └── web-search.md                   (NEW - ~150 lines)
```

## Step 1: WebSearchAdapter Implementation

**File: `quest_ai_runner/adapters/web_search_adapter.py`**

Key points:
- Subclass `RetrievalAdapterBase` (from `quest_ai_runner.core.adapters`)
- Implement all required methods (never raise)
- Use requests library (already a dependency for Anthropic integration)
- Parse Tavily JSON API responses
- Format results as markdown for the planner

**Interface contract:**

```python
class WebSearchAdapter(RetrievalAdapterBase):
    """
    Search the web using Tavily API (https://tavily.com).
    
    Implements RetrievalAdapter interface:
    - read_section(query): search and return formatted results
    - grep(pattern): search for pattern matches
    - query(spec): advanced queries
    - list_sources/describe_source: discovery
    - list_operations/describe_operation: operation discovery
    """
```

**Tavily API Call Pattern:**

```python
import requests

def _search(self, query: str, max_results: int = 5) -> list:
    """Call Tavily API. Returns list of dicts: {url, title, snippet, score}"""
    
    payload = {
        "api_key": self.api_key,
        "query": query,
        "max_results": min(max_results, 10),  # Tavily limit
        "include_answer": False,
        "include_raw_content": False,  # Keep response small
    }
    
    try:
        response = requests.post(
            "https://api.tavily.com/search",
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("results", [])
    except requests.RequestException:
        return []  # Return empty, never raise
```

**Method implementation examples:**

```python
def read_section(self, rel_path, *, start_line=None, end_line=None, 
                 heading=None, max_bytes=None) -> Observation:
    """Treat rel_path as a search query."""
    query = rel_path.strip()
    if not query or len(query) < 3:
        return Observation(kind="error", rel_path=rel_path,
                         error="query too short")
    
    results = self._search(query, self.max_results)
    if not results:
        return Observation(kind="error", rel_path=rel_path,
                         error="no results found")
    
    text = self._format_results(results)
    return Observation(kind="read", rel_path=rel_path,
                      locator="web_search", text=text)

def grep(self, pattern, *, scope=None, max_hits=None) -> Observation:
    """Search web for pattern, return matching results."""
    if not pattern or len(pattern) < 2:
        return Observation(kind="error", pattern=pattern,
                         error="pattern too short")
    
    max_hits = max_hits or 10
    results = self._search(pattern, max_hits)
    
    hits = []
    for r in results:
        # Check if pattern appears in snippet (case-insensitive)
        if pattern.lower() in r.get("snippet", "").lower():
            hits.append({
                "line": r["title"],
                "url": r["url"],
                "snippet": r["snippet"][:200] + "..."
            })
    
    if not hits:
        return Observation(kind="error", pattern=pattern,
                         error="pattern not found in results")
    
    return Observation(kind="grep", pattern=pattern, hits=hits)

def query(self, spec) -> Observation:
    """Advanced search: {"search": "...", "max_results": 5, "filter": "site:amazon.com"}"""
    query = spec.get("search", "").strip()
    if not query:
        return Observation(kind="error",
                         error="query spec missing 'search' field")
    
    # TODO: implement optional domain filtering if needed
    # For MVP, just use search query as-is
    
    max_results = min(spec.get("max_results", self.max_results), 10)
    results = self._search(query, max_results)
    
    if not results:
        return Observation(kind="error",
                         error=f"no results for: {query}")
    
    text = self._format_results(results)
    return Observation(kind="query", text=text,
                      hits=[{"url": r["url"], "title": r["title"]}
                           for r in results])

def list_sources(self) -> Observation:
    return Observation(kind="query", locator="list_sources",
        text="web: real-time web search via Tavily\n"
             "  Use read_section(query) to search for information")

def describe_source(self, name, *, path=None) -> Observation:
    if name not in ("web", "web_search"):
        return Observation(kind="error",
                         error=f"source not found: {name}")
    return Observation(kind="query", locator=f"describe_source({name})",
        text="Web search via Tavily API. Returns title, URL, and snippet "
             "for the top results matching your query. Use for current "
             "information, product recommendations, prices, etc.")

def list_operations(self) -> Observation:
    return Observation(kind="query", locator="list_operations",
        text="- search(query): search the web\n"
             "- grep(pattern): find pattern in web results")

def describe_operation(self, name) -> Observation:
    ops = {
        "search": "search(query) → web search results [title, URL, snippet]",
        "grep": "grep(pattern) → find pattern in web results",
    }
    if name not in ops:
        return Observation(kind="error",
                         error=f"operation not found: {name}")
    return Observation(kind="query", locator=f"describe_operation({name})",
                      text=ops[name])

def _format_results(self, results: list, max_bytes: int = 2000) -> str:
    """Format Tavily results for the planner."""
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', 'Untitled')}")
        lines.append(f"   URL: {r.get('url', 'N/A')}")
        snippet = r.get('snippet', '')[:150]
        if snippet:
            lines.append(f"   {snippet}...")
        lines.append("")
    
    text = "\n".join(lines)
    if len(text.encode()) > max_bytes:
        text = text[:max_bytes].rsplit("\n", 1)[0] + "\n...[truncated]"
    return text
```

## Step 2: Configuration Updates

**File: `quest_ai_runner/config.py`**

Add to the `RunnerConfig` dataclass:

```python
@dataclass
class RunnerConfig:
    # ... existing fields ...
    
    # Optional web search configuration
    web_search_api_key: Optional[str] = None
    web_search_enabled: bool = False
    web_search_max_results: int = 5
    web_search_timeout_seconds: int = 10
```

Update `build_orchestrator()`:

```python
def build_orchestrator(cfg: RunnerConfig) -> Orchestrator:
    """Build orchestrator, optionally adding web search to retrieval."""
    
    # Start with user-supplied retrieval (or None)
    retrieval_adapters = []
    if cfg.retrieval:
        retrieval_adapters.append(cfg.retrieval)
    
    # Optionally add web search
    if cfg.web_search_enabled and cfg.web_search_api_key:
        try:
            from .adapters.web_search_adapter import WebSearchAdapter
            web_search = WebSearchAdapter(
                api_key=cfg.web_search_api_key,
                max_results=cfg.web_search_max_results,
                timeout=cfg.web_search_timeout_seconds,
            )
            retrieval_adapters.append(web_search)
        except ImportError:
            # WebSearchAdapter not available (e.g., requests not installed)
            log.warning("web_search_enabled but WebSearchAdapter not available")
    
    # Compose adapters if we have multiple
    if len(retrieval_adapters) > 1:
        from .adapters.composite_retrieval_adapter import CompositeRetrievalAdapter
        final_retrieval = CompositeRetrievalAdapter(retrieval_adapters)
    elif retrieval_adapters:
        final_retrieval = retrieval_adapters[0]
    else:
        final_retrieval = None
    
    # ... rest of orchestrator setup, use final_retrieval ...
```

## Step 3: CLI Updates

**File: `quest_ai_runner/cli.py`**

In the `main()` or environment-loading section:

```python
import os

def _load_runner_config_from_env() -> RunnerConfig:
    """Load config from environment variables."""
    cfg = RunnerConfig(
        quest_base_url=os.getenv("QUEST_BASE_URL", ""),
        quest_api_key=os.getenv("QUEST_API_KEY", ""),
        # ... other fields ...
    )
    
    # Web search configuration
    cfg.web_search_enabled = os.getenv("WEB_SEARCH_ENABLED", "").lower() == "true"
    cfg.web_search_api_key = os.getenv("WEB_SEARCH_API_KEY", "")
    if api_key := os.getenv("WEB_SEARCH_MAX_RESULTS"):
        cfg.web_search_max_results = int(api_key)
    
    return cfg
```

Update `.env.example`:

```bash
# Web search (optional)
WEB_SEARCH_ENABLED=false
# Get key from https://tavily.com
WEB_SEARCH_API_KEY=tvly_your_api_key_here
WEB_SEARCH_MAX_RESULTS=5
```

## Step 4: Testing

**File: `tests/test_web_search_adapter.py`**

Basic test structure (no actual API calls):

```python
import pytest
from quest_ai_runner.adapters.web_search_adapter import WebSearchAdapter
from quest_ai_runner.core.adapters import Observation

@pytest.fixture
def adapter():
    """Mock adapter for testing (no real API calls)."""
    return WebSearchAdapter(api_key="test_key")

def test_read_section_empty_query(adapter):
    """Empty query returns error."""
    obs = adapter.read_section("")
    assert obs.kind == "error"

def test_grep_empty_pattern(adapter):
    """Empty pattern returns error."""
    obs = adapter.grep("")
    assert obs.kind == "error"

def test_list_sources(adapter):
    """list_sources returns web source."""
    obs = adapter.list_sources()
    assert obs.kind == "query"
    assert "web" in obs.text.lower()

def test_describe_source_unknown(adapter):
    """Unknown source returns error."""
    obs = adapter.describe_source("unknown")
    assert obs.kind == "error"

def test_describe_source_web(adapter):
    """Web source is describable."""
    obs = adapter.describe_source("web")
    assert obs.kind == "query"

# Note: For integration testing with actual Tavily API,
# use a separate test suite with @pytest.mark.integration
```

## Step 5: Dependencies

Check that `requests` is available (it should be, since Anthropic SDK uses it):

```python
# In setup.py or pyproject.toml, requests is already a dependency
# via anthropic >= 0.7.0
```

If not already present, add to optional dependencies:

```python
extras_require={
    "web-search": ["requests>=2.28.0"],
}
```

## Step 6: Documentation

Create `docs/web-search.md` (see separate file)

Update `README.md` Features section:

```markdown
- **Web Search Integration** — Optional Tavily-backed web search adapter. 
  When enabled, the planner can search the web for current information, 
  product recommendations, prices, etc. Configure via environment variables.
```

Update `CHANGELOG.md`:

```markdown
## [Unreleased]

### Added
- **WebSearchAdapter** — New RetrievalAdapter for searching the web via Tavily API. 
  When enabled in RunnerConfig, the orchestrator's planner can discover and use web 
  search as a data source alongside local files and databases. Composable with other 
  adapters via CompositeRetrievalAdapter. Graceful error handling ensures network 
  failures never break the run.
  - Supports read_section (search), grep (pattern matching), query (advanced)
  - Discovery methods teach the planner about available sources
  - Configuration: WEB_SEARCH_ENABLED, WEB_SEARCH_API_KEY, WEB_SEARCH_MAX_RESULTS
  - See docs/web-search.md for setup and usage
```

## Integration Example (for Spiritual Data)

**Personal lane (Joshua's runner):**

```bash
# ~/.claude/personal/.env (or in the quest-ai-runner .env)
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_abc123xyz789
WEB_SEARCH_MAX_RESULTS=5
```

**Team runner (cockpit's runner):**

```bash
# /home/joshua/hq/stories/spiritual_data/product/launch_code/quest-ai-runner/.env
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_team_key_here
WEB_SEARCH_MAX_RESULTS=5
```

Once set, any task can ask questions like:
- "What's a good gift for someone who likes coding?"
- "Find an Amazon link to a quality mechanical keyboard under $150"
- "What are the latest Python testing frameworks?"

The orchestrator will automatically search the web and ground its answer in current data.

---

## MVP Scope (Minimum Viable Product)

To get web search working end-to-end with minimal code:

1. Create `WebSearchAdapter` with `read_section`, `grep`, `query`, `list_sources`, `describe_source`
2. Update `RunnerConfig` with three fields
3. Update `build_orchestrator()` to wire in the adapter
4. Update `cli.py` to read env vars
5. Test manually with a simple task
6. Document in `docs/web-search.md`

**Estimate:** ~400 lines of code, ~2-4 hours implementation

## Future Enhancements

- Domain filtering: `query({"search": "...", "domain": "amazon.com"})`
- Cached results: avoid duplicate searches in the same run
- Advanced filtering: recency, language, image results
- Fallback services: GoogleSearch API as backup
- Hybrid retrieval: combine web results with local knowledge
- Cost tracking: monitor Tavily API usage/costs
