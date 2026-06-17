# Multi-Source Retrieval: Query Files, Databases, and Conversations in Parallel

By default, `quest-ai-runner` takes a single `RetrievalAdapter` that grounds on one source (files, a database, etc.). **You can now wire multiple adapters to query them all in parallel** — files AND databases AND Claude conversations AND task memory, all at once.

## Quick Start

```python
from quest_ai_runner.adapters import (
    FilesAdapter,
    CachedDbAdapter,
    ClaudeConversationsAdapter,
    CompositeRetrievalAdapter,
)
from quest_ai_runner.config import RunnerConfig, build_orchestrator

# Compose multiple sources
retrieval = CompositeRetrievalAdapter([
    FilesAdapter("/path/to/corpus"),
    CachedDbAdapter(db_url="...", sources={...}),
    ClaudeConversationsAdapter(sessions_dir="~/.claude/sessions"),
])

# Wire it into the orchestrator
cfg = RunnerConfig(
    retrieval=retrieval,
    model_provider=...,
)
orch = build_orchestrator(cfg)

# When you run a query, it grounds on all three sources in parallel
result = orch.run("How have we discussed this topic in past conversations?")
```

## How It Works

`CompositeRetrievalAdapter` runs all adapters in parallel using a thread pool:

- **`read_section(path)`** — tries adapters in order until one succeeds (no error).
- **`grep(pattern)`** — searches all adapters in parallel, deduplicates hits by line + source.
- **`query(spec)`** — queries all adapters in parallel, combines results with source attribution (`_source` field on each hit).
- **Discovery methods** — `list_sources()`, `describe_source()`, etc. merge results from all adapters.

## Available Adapters

### FilesAdapter
Query a filesystem hierarchy (markdown docs, code, etc.).

```python
files = FilesAdapter("/path/to/corpus")
```

### CachedDbAdapter
Query a live database with short-TTL caching (no file sync).

```python
db = CachedDbAdapter(
    db_url="postgresql://...",
    sources={
        "users": "user table with name, email, role",
        "goals": "goal table with title, owner, status",
    },
)
```

### ClaudeConversationsAdapter
Read Claude Code session transcripts as retrieval sources.

```python
conversations = ClaudeConversationsAdapter(
    sessions_dir="~/.claude/sessions"  # defaults to this
)
```

This loads all `.json` session files from the directory, extracting conversation text. When the brain needs context, it can grep for patterns or read entire conversations by id.

### Custom Adapters
Implement the `RetrievalAdapter` protocol to add any other source:

```python
class MyCustomAdapter:
    def read_section(self, rel_path, *, start_line=None, end_line=None, heading=None, max_bytes=None) -> Observation:
        # Return an Observation with the section text
        ...
    
    def grep(self, pattern, *, scope=None, max_hits=None) -> Observation:
        # Return hits across the source
        ...
    
    def query(self, spec) -> Observation:
        # Structured lookup
        ...
    
    def list_sources(self) -> Observation:
        ...
    
    # ... other discovery methods
```

## Configuration

### max_workers
Control the thread pool size:

```python
retrieval = CompositeRetrievalAdapter(
    adapters=[files, db, conversations],
    max_workers=4,  # default
)
```

Higher concurrency = faster parallel queries, but more resource use.

## Information Flow

When the orchestrator's planner needs context:

1. **Plan phase**: calls `list_sources()` + `describe_source()` on all adapters to learn what's available.
2. **Gather phase**: calls `read_section()` / `grep()` / `query()` on all adapters in parallel.
3. **Results**: merged with source attribution (files from `FilesAdapter`, db rows from `CachedDbAdapter`, conversation excerpts from `ClaudeConversationsAdapter`).
4. **Replan**: if more context is needed, repeats the gather phase.

This follows Shannon's **Data Processing Inequality** — no lossy summarization step between sources and reasoning. The brain sees the full relevant context from all sources together.

## Example: Claude Conversations as Context

Ask about past decisions or design discussions:

```python
orch.run(
    "What design patterns did we discuss in our conversations?",
    retrieval=CompositeRetrievalAdapter([
        files,                              # the codebase
        ClaudeConversationsAdapter(),       # past conversations
    ])
)
```

The brain will:
- grep through all conversations for "design pattern" mentions
- read the full conversations that match
- cross-reference with the codebase files
- synthesize an answer grounded in both

## Known Limitations

- **ClaudeConversationsAdapter** is read-only and doesn't support mutations.
- **Semantic search** (the `query` method) on conversations is a placeholder; it returns all conversations (no filtering). Extend it with embeddings for proper semantic search.
- **Threading**: all adapters block on I/O. For CPU-bound operations, consider wrapping in subprocess runners.

## Next Steps

- Extend `ClaudeConversationsAdapter.query()` with embedding-based semantic search.
- Add adapters for Slack transcripts, GitHub issues, emails, or other conversational sources.
- Benchmark parallel vs. serial retrieval on your corpus.
