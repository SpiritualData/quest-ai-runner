# Integrating Claude Conversations into Your Corpus

When you ask quest-ai-runner a question, you want it to know about your Claude conversations — design decisions, implementation discussions, past insights — not just files and databases.

## Directory Structure

Store Claude conversations anywhere within your corpus. `ClaudeConversationsAdapter` recursively discovers them in:
- `conversations/` directories at any level
- `.claude/` directories at any level
- Any `*.json` conversation files within these special directories

```
corpus/
├── docs/
│   ├── .claude/
│   │   ├── design_decisions.json
│   │   └── api_review.json
│   ├── architecture.md
│   └── api.md
├── code/
│   ├── conversations/
│   │   ├── implementation.json
│   │   └── troubleshooting.json
│   ├── handler.py
│   └── utils.py
└── conversations/              # or top-level
    ├── team_sync_2026_06.json
    └── release_planning.json
```

The adapter will find all conversations regardless of nesting depth.

## Conversation File Format

Each `.json` file in `conversations/` should be a Claude session export (what Claude Code saves):

```json
{
  "rep_name": "Alex's AI",
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

### 3. Organize Conversations by Topic and Location

Conversations are discovered recursively, so you can organize by topic/location:

```
corpus/
├── docs/.claude/
│   ├── api_design_2026_06.json      — API design discussion
│   ├── security_review.json         — Security analysis
│   └── data_model.json              — Data modeling decisions
├── code/conversations/
│   ├── performance.json             — Profiling and optimization
│   ├── error_handling.json          — Error handling patterns
│   └── refactoring_plan.json        — Code cleanup strategy
└── conversations/                    — Top-level discussions
    ├── architecture.json            — System architecture
    └── team_sync_2026_06.json       — Team meeting notes
```

**Naming conventions (optional):**
- Include dates for time-based discovery: `api_design_2026_06_10.json`
- Group by topic in directory names: `docs/.claude/`, `code/conversations/`
- Keep names descriptive: `error_handling_patterns.json` not `conv123.json`

## How It Works

When you provide a corpus with conversations, the adapter:

### Discovery Phase
1. **Recursive scan** for `.claude/` and `conversations/` directories at any depth
2. **Format validation**: skips non-conversation JSON files
3. **Metadata extraction**: reads rep_name, turn_count, model used
4. **Path-based IDs**: unique conversation ids based on location (e.g., `docs:.claude:design`)

### Query Phase (Orchestrator)
1. **Discovery**: `list_sources()` and `describe_source(name)` enumerate conversations
2. **Grep**: searches all conversations in parallel for relevant patterns
3. **Read**: loads full conversation text with metadata header when needed
4. **Merge**: combines results from files, conversations, and databases

### Conversation Format
The adapter recognizes Claude conversations by structure:
```json
{
  "rep_name": "Alex's AI",             # optional: AI name
  "turn_count": 5,                     # optional: for quick reference
  "messages": [                        # required: array of message objects
    {
      "role": "user",                  # required: "user" or "assistant"
      "text": "Your question here"     # required: message content
    },
    {
      "role": "assistant",
      "text": "Response here"
    }
  ]
}
```

The brain sees conversation context alongside file context — no lossy summarization, full conversation history available.

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
