# QuickTask Analysis: Web Search for quest-ai-runner

**QuickTask:** "On Quest I made an AI task to suggest an Amazon URL for something to buy and it came back saying 'I can't generate Amazon URLs' - it was supposed to do a deep task (with claude code) on my external environment, which has access to web search, either that or an explicit web search retrieval adaptor or tool the task can use in quest-ai-runner."

**Date:** 2026-06-17

**Status:** Analysis Complete - Ready for Implementation

---

## Executive Summary

Joshua identified a gap: AI tasks in Quest fail when asked for web-based suggestions because quest-ai-runner (the executor for AI tasks) has no way to search the web. The solution is to create a **WebSearchAdapter** that extends quest-ai-runner with web search capability.

This is an ideal fit for quest-ai-runner's design because:
1. Web search is just another RetrievalAdapter (like FilesAdapter, CachedDbAdapter)
2. The orchestrator was designed to be pluggable - it doesn't care which source adapters talk to
3. CompositeRetrievalAdapter (already in the codebase) combines multiple sources seamlessly
4. The discovery system (list_sources/describe_source) teaches the planner what sources exist

No changes to Quest, no changes to the cockpit. Just a new adapter that plugs in via config.

---

## Root Cause

**Why the task failed:**

1. **No web search adapter:** quest-ai-runner's default RetrievalAdapter options are FilesAdapter (local files) and CachedDbAdapter (local DB). No web search.

2. **No web search knowledge:** The orchestrator's planner doesn't know it can search the web. It only plans based on available data sources (learned via list_sources).

3. **Model refusal:** The LLM model appropriately refuses to generate URLs without grounded, real-time data. It can't invent Amazon product links.

4. **Not a deep-run problem:** The issue isn't that Claude Code (deep runner) can't do web search - it has WebSearch/WebFetch tools. The issue is that the shallow orchestrator loop can't. And shallow is preferred for simple queries.

---

## Solution Architecture

```
┌─────────────────────────────────────────────────────────┐
│ AI Task in Quest: "Find an Amazon product for X"        │
└────────────────────┬────────────────────────────────────┘
                     │
                     ▼
         ┌──────────────────────┐
         │  Orchestrator Brain  │  (quest-ai-runner core)
         │ (plan→gather→replan) │
         └──────────┬───────────┘
                    │
      Calls: list_sources(), describe_source()
                    │
         ┌──────────▼───────────┐
         │ CompositeRetrieval   │
         │      Adapter         │
         │  (runs in parallel)  │
         └──────────┬───────────┘
                    │
         ┌──────────┴──────────────┬──────────────┐
         │                         │              │
    ┌────▼─────┐          ┌────────▼─┐      ┌───▼───────┐
    │   Files  │          │    DB    │      │ WEB SEARCH│  ← NEW
    │ Adapter  │          │ Adapter  │      │ Adapter   │
    └──────────┘          └──────────┘      └───────────┘
```

When the planner calls read_section("find an Amazon product"), it flows through CompositeRetrievalAdapter, which tries each adapter in order. WebSearchAdapter handles web queries, returns formatted results, and the planner grounds its answer in real, current data.

---

## Implementation Path

### Phase 1: WebSearchAdapter (Recommended)

**What:** Create a new `WebSearchAdapter` class that implements RetrievalAdapter interface

**How:**
1. Use Tavily API (designed for AI agents, simple, free tier available)
2. Implement required methods: `read_section()`, `grep()`, `query()`, `list_sources()`, `describe_source()`, `list_operations()`, `describe_operation()`
3. Never raise; return Observation(kind="error") on failures
4. Format results for the planner (markdown with title, URL, snippet)

**Files to create/modify:**
- `quest_ai_runner/adapters/web_search_adapter.py` (NEW, ~200 lines)
- `quest_ai_runner/config.py` (update +20 lines - add web_search_* fields)
- `quest_ai_runner/cli.py` (update +10 lines - read env vars)
- `docs/web-search.md` (NEW, ~150 lines)
- `tests/test_web_search_adapter.py` (NEW, ~80 lines)

**Effort:** ~400-500 lines of code, ~4-6 hours

**Testing:** 
- Unit tests (mock API responses)
- Integration test with actual Tavily (optional)
- Manual task: "Find a Python book on Amazon"

**Deploy:** 
```bash
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_xxxxx
```

---

### Phase 2: Deep-Runner Enhancement (Optional)

**Status:** Already working! Claude Code (the default DeepRunner) has WebSearch and WebFetch tools. When a task needs deep work (research, filtering, decisions), it can spawn Claude Code with full web access.

**No changes needed** for Phase 2. The deep runner's web capabilities are already functional.

---

## Why This Design

### 1. Follows Existing Architecture
- quest-ai-runner is built on **pluggable adapters** (RetrievalAdapter, ModelProvider, DeepRunner, EscalationSink)
- Each adapter is a small, well-defined interface
- Consumers (orgs, runners) combine adapters via config, never code changes

### 2. Composable
- CompositeRetrievalAdapter (already in the codebase) runs multiple adapters in parallel
- No special casing in the orchestrator
- Any org can add web search without touching the core library

### 3. Discoverable
- The orchestrator's discovery system (list_sources / describe_source) teaches the planner what sources exist
- No hardcoded knowledge - the planner learns at runtime
- When web search is added, the planner automatically learns to use it

### 4. Graceful Degradation
- Adapters never raise exceptions
- Network failures return Observation(kind="error")
- The loop continues; broken adapters don't break the run

### 5. Public / Open-Source
- quest-ai-runner is published on GitHub (Apache 2.0)
- Any org can adopt this code
- Keeps consumer-specific secrets (API keys) in config/env, never in the repo

---

## For Spiritual Data Specifically

### Personal Lane (Joshua's quest-ai-runner)
Located at: `~/.claude/personal/personal_runner.py` (uses SD's published quest-ai-runner)

Once WebSearchAdapter is ready:
```bash
# ~/.claude/personal/.env
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_joshua_key
```

### Team Runner (Cockpit's quest-ai-runner)
Located at: `/home/joshua/hq/stories/spiritual_data/product/launch_code/quest-ai-runner/`

Once WebSearchAdapter is ready:
```bash
# .env in that directory
WEB_SEARCH_ENABLED=true
WEB_SEARCH_API_KEY=tvly_team_key
WEB_SEARCH_MAX_RESULTS=5
```

### No Changes Needed To:
- Quest backend (no API changes)
- Quest frontend (no UI changes)
- Cockpit (ai-feedback-system) - uses the runner as-is
- Deep runner (Claude Code) - already has web tools

---

## Assumptions & Decisions Made

### 1. Use Tavily API (not Google Search)
- **Why Tavily:** Designed for AI agents, simple REST API, minimal setup, free tier (500 calls/month)
- **Alternative:** Google Search API (would work too, more features, slightly more complex setup)
- **Decision:** Recommend Tavily for MVP; can add GoogleSearch as fallback later

### 2. Use CompositeRetrievalAdapter (not a single "hybrid" adapter)
- **Why:** Keeps each adapter focused and testable. The composition layer already exists and is proven.
- **Alternative:** Create a new "HybridAdapter" that merges all sources. More complex, duplicates logic.
- **Decision:** Use CompositeRetrievalAdapter - it's the right tool for the job.

### 3. Never raise from adapters
- **Why:** Keeps the orchestrator loop robust. Network failures don't break the run.
- **Alternative:** Let adapters raise, catch in orchestrator. More noisy error handling.
- **Decision:** Follow the existing adapter contract - return Observation(kind="error") on any failure.

### 4. Make web search optional, not required
- **Why:** Not every org needs web search. Some prefer air-gapped or local-only.
- **Default:** WEB_SEARCH_ENABLED=false
- **Opt-in:** Set env vars to enable

---

## Risks & Mitigation

| Risk | Mitigation |
|------|-----------|
| **API costs** (Tavily usage explodes) | Free tier capped at 500/month. Monitor with logs. Easy to disable. Paid tier available if needed. |
| **Rate limiting** (tasks slow down) | Tavily returns results in ~1sec. Acceptable. Add per-task caching if needed later. |
| **Network failures** | Adapters never raise; errors are graceful. Loop continues with local sources. |
| **Stale / incorrect results** | Results are clearly attributed (URL + source). Model can note limitations. |
| **Privacy** (web search results visible in logs) | Web search results are public. OK to log. Document policy in docs. |

---

## Open Questions for Joshua

1. **Tavily vs. GoogleSearch?** (Recommend Tavily for simplicity; can add GoogleSearch as fallback later)
2. **Free tier sufficient?** Tavily's 500 calls/month OK, or do we need higher? (Discuss budget)
3. **When?** Implement Phase 1 immediately? After other priorities?
4. **Domain?** Should web search be "web" or "web_search" in list_sources? (Recommend "web" - shorter, clearer)

---

## Files Produced by This Analysis

1. **WEB_SEARCH_IMPLEMENTATION.md** — Full architecture & rationale
2. **IMPLEMENTATION_GUIDE.md** — Detailed code structure & examples
3. **This summary** — Executive overview

---

## Next Steps for Implementation

1. **Confirm design with Joshua** (this document)
2. **Get Tavily API key** (free tier at tavily.com, takes 5 minutes)
3. **Implement WebSearchAdapter** (~4-6 hours)
4. **Test with a sample task** (e.g., "Find a mechanical keyboard on Amazon under $150")
5. **Merge to main** (update quest-ai-runner repo)
6. **Deploy** (enable in .env files for Joshua's personal lane and team runner)
7. **Communicate** (brief Joshua + team that web search is available)

---

## Conclusion

This is a straightforward extension of quest-ai-runner's existing adapter architecture. The design is proven (CompositeRetrievalAdapter is already in the codebase), the scope is small (~500 lines), and the value is immediate (AI tasks can now search the web for current information).

Once implemented, Joshua can ask for Amazon product links, current prices, latest research, anything web-based, and the AI task will ground its answer in real-time data.
