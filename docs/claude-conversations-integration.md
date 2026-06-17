# Integrating Claude Conversations into Your Corpus

When you ask quest-ai-runner a question, you want it to know about your Claude conversations — design decisions, implementation discussions, past insights — not just files and databases.

## Directory Structure

Store Claude conversations alongside your corpus files:

```
corpus/
├── docs/
│   ├── architecture.md
│   ├── api.md
│   └── guidelines.md
├── code/
│   ├── handler.py
│   └── utils.py
└── conversations/
    ├── design_decisions.json
    ├── implementation.json
    ├── troubleshooting.json
    └── team_sync_2026_06.json
```

## Conversation File Format

Each `.json` file in `conversations/` should be a Claude session export (what Claude Code saves):

```json
{
  "rep_name": "Joshua's AI",
  "turn_count": 5,
  "messages": [
    {
      "role": "user",
      "text": "How should we structure the error handling?"
    },
    {
      "role": "assistant",
      "text": "We should validate at system boundaries..."
    },
    ...
  ]
}
```

The file name becomes the conversation id (e.g., `design_decisions.json` → id `design_decisions`).

## Setup

### 1. Export Claude Conversations

From Claude Code, save your session (typically at `~/.claude/sessions/*.json`):

```bash
# Copy to your corpus
cp ~/.claude/sessions/my_conversation.json corpus/conversations/
```

Or use `quest-ai-runner chat` with the `--save` command to export directly.

### 2. Wire the Adapter

```python
from quest_ai_runner.adapters import (
    FilesAdapter,
    ClaudeConversationsAdapter,
    CompositeRetrievalAdapter,
)
from quest_ai_runner.config import RunnerConfig, build_orchestrator

# Point to your corpus root (conversations will auto-discover corpus/conversations/)
corpus_root = "/path/to/corpus"

retrieval = CompositeRetrievalAdapter([
    FilesAdapter(corpus_root),
    ClaudeConversationsAdapter(corpus_root=corpus_root),  # auto-discovers corpus/conversations/
])

cfg = RunnerConfig(
    retrieval=retrieval,
    model_provider=...,  # e.g., AnthropicProvider()
)

orch = build_orchestrator(cfg)
result = orch.run("How have we discussed authentication?")
```

### 3. Organize Conversations by Topic

Name conversations clearly so the brain can discover them:

- `design_decisions.json` — architecture and design choices
- `implementation_notes.json` — coding patterns and approaches
- `troubleshooting.json` — debugging sessions and solutions
- `team_sync_2026_06.json` — team meeting notes
- `performance_analysis.json` — profiling and optimization insights

## How It Works

When the orchestrator needs context:

1. **Discovery**: calls `list_sources()` and `describe_source(name)` to see what conversations exist
2. **Grep**: searches all conversations in parallel for relevant patterns
3. **Read**: loads full conversations when needed
4. **Dedup**: removes duplicate hits across sources
5. **Merge**: combines results from files, conversations, and databases

The brain sees conversation context alongside file context — no lossy summarization, full text available.

## Example Query

```python
# Ask the orchestrator something
result = orch.run(
    "What patterns did we use for error handling in past conversations? "
    "Show me both the design decision and the actual code."
)
```

The brain will:
- Grep through conversations for "error handling" mentions
- Read the matching conversations
- Cross-reference with code files for implementation
- Synthesize an answer grounded in all sources

## Tips

### Keep Conversations Fresh
Regularly export important Claude Code sessions to your corpus. Use a naming convention with dates:
- `design_auth_2026_06_10.json`
- `performance_review_2026_06_15.json`

### Structure Long Sessions
For long conversations, consider splitting into focused exports:
- **One per topic** (not one mega-conversation with everything)
- Makes grep results cleaner
- Brain can discover smaller, more relevant contexts

### Semantic Search (Future)
Currently, `query()` returns all conversations (no filtering). To add semantic search:

1. Extend `ClaudeConversationsAdapter.query()` with embeddings
2. Use a VectorStore to embed conversation summaries
3. Find semantically similar past conversations

## Example Corpus Structure

See [`examples/composite_retrieval_example.py`](../examples/composite_retrieval_example.py) for a working example with sample conversations and files.

Run it:
```bash
python3 examples/composite_retrieval_example.py
```

## Troubleshooting

### Conversations not found
- Check file naming: `conversation_id.json` must be a valid JSON file
- Check directory: `ClaudeConversationsAdapter(sessions_dir="path/to/conversations")`
- Verify JSON format: each file must have a valid `messages` or `turns` list

### Too many hits in grep
- Conversations contain lots of text; grep can return many results
- The orchestrator's re-plan loop will narrow down based on relevance
- Or manually filter in `query()` by adding a semantic search filter

### Performance
- `CompositeRetrievalAdapter` runs searches in parallel (ThreadPoolExecutor)
- Grep across many large conversations can take a few seconds
- Consider splitting very long conversations for faster search

## Next Steps

- Add more conversation sources (Slack transcripts, GitHub discussions, emails)
- Implement semantic search in `ClaudeConversationsAdapter.query()`
- Build a background indexer that auto-exports new Claude sessions to your corpus
