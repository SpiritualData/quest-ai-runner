# Context Card Schema

A **context card** is a discoverable unit of knowledge that can come from many sources: files, conversations, chat history, quest docs, etc.

## Schema

```json
{
  "id": "unique-card-id",
  "keywords": ["topic1", "topic2"],
  "summary": "One-line summary of what this card contains",
  
  "sources": [
    {
      "type": "file",
      "path": "src/handler.py",
      "sha256": "...",
      "git_sha": "...",
      "mtime": 1700000000.0,
      "why": "entry point"
    },
    {
      "type": "conversation",
      "id": "design_decisions",
      "path": "conversations/design_decisions.json",
      "sha256": "...",
      "why": "design rationale"
    },
    {
      "type": "chat_history",
      "session_id": "claude-20260617-abc123",
      "path": "sessions/claude-20260617-abc123.json",
      "message_count": 15,
      "why": "implementation walkthrough"
    },
    {
      "type": "quest_doc",
      "url": "https://docs.spiritualdata.org/quest/goals",
      "title": "Quest Goals Documentation",
      "why": "context for goal structure"
    }
  ],
  
  "conventions": ["pointer-to-applicable-rule"],
  
  "provenance": {
    "created_by_task": "task-name",
    "model": "claude-opus-4-8",
    "created_at": "2026-06-17T...",
    "last_verified_at": "2026-06-17T..."
  },
  
  "usage_count": 3,
  "last_outcome": "met|failed|unknown"
}
```

## Source Types

### `file`
A code or documentation file in the corpus.
```json
{
  "type": "file",
  "path": "src/handler.py",
  "sha256": "abc123...",
  "git_sha": "def456...",
  "mtime": 1700000000.0,
  "why": "entry point / implementation / documentation"
}
```

### `conversation`
A Claude conversation (chat session).
```json
{
  "type": "conversation",
  "id": "auth_design",
  "path": "conversations/auth_design.json",
  "sha256": "...",
  "turn_count": 5,
  "why": "design rationale / decision discussion"
}
```

### `chat_history`
Chat history from an interaction with QAR or Claude Code.
```json
{
  "type": "chat_history",
  "session_id": "claude-20260617-abc123",
  "path": "sessions/claude-20260617-abc123.json",
  "message_count": 15,
  "created_at": "2026-06-17T...",
  "why": "implementation walkthrough / debugging session"
}
```

### `quest_doc`
Quest documentation or external reference.
```json
{
  "type": "quest_doc",
  "url": "https://docs.spiritualdata.org/quest/goals",
  "title": "Quest Goals Documentation",
  "why": "context for goal structure"
}
```

### `example`
Reference implementation or pattern example.
```json
{
  "type": "example",
  "path": "examples/adapter_pattern.py",
  "description": "Example RetrievalAdapter implementation",
  "why": "reference pattern"
}
```

### `test`
Test file that demonstrates expected behavior.
```json
{
  "type": "test",
  "path": "tests/test_handler.py",
  "test_count": 12,
  "why": "acceptance criteria / behavior spec"
}
```

## Card Workflow

### Creation
Cards are created by **CardBuilders** specific to each source type:

```python
from quest_ai_runner.adapters import (
    FileCardBuilder,
    ConversationCardBuilder,
    ChatHistoryCardBuilder,
)

# Build cards from different sources
file_cards = FileCardBuilder(cards_dir).build_all_from_directory("src/")
conv_cards = ConversationCardBuilder(cards_dir).build_all_from_directory("conversations/")
chat_cards = ChatHistoryCardBuilder(cards_dir).build_all_from_directory("sessions/")

# All written to same cards_dir, indexed by FileContextStore
```

### Discovery
**FileContextStore** discovers cards by:
1. Listing all card JSON files
2. Matching keywords against task text
3. Loading relevant sources for context assembly

```python
# Planner asks: "How should we handle authentication?"
# FileContextStore finds:
#   - auth_handler.py (file source)
#   - auth_design conversation (conversation source)
#   - auth_implementation chat (chat_history source)
# All in one card because they're related
```

### Multi-Source Cards
A single card can reference multiple sources of different types:

```json
{
  "id": "authentication",
  "keywords": ["auth", "jwt", "security"],
  "summary": "Complete authentication design and implementation",
  "sources": [
    {"type": "file", "path": "src/auth.py"},           // Code
    {"type": "conversation", "id": "auth_design"},     // Design discussion
    {"type": "chat_history", "session_id": "..."},     // Implementation walkthrough
    {"type": "test", "path": "tests/test_auth.py"},    // Behavior spec
    {"type": "quest_doc", "url": "..."}                // External docs
  ]
}
```

When the planner selects this card, it gets **all sources** together:
- The implementation (file)
- The design rationale (conversation)
- The implementation process (chat history)
- The tests (test)
- Any external docs (quest_doc)

**No compression, no summarization** — full context from all sources.

## Built-in CardBuilders

| Builder | Source Type | Creates |
|---------|-------------|---------|
| `FileCardBuilder` | file | From code + docs directories |
| `ConversationCardBuilder` | conversation | From `conversations/` dirs |
| `ChatHistoryCardBuilder` | chat_history | From `sessions/` dirs |
| `TestCardBuilder` | test | From test files |
| `ExampleCardBuilder` | example | From `examples/` dirs |

Each builder:
- Extracts keywords intelligently
- Generates summaries
- Computes file hashes / timestamps
- Writes card JSON to `cards_dir`

## Why Multiple Sources in One Card?

A card might combine sources because they're **conceptually related**:

```json
{
  "id": "error_handling",
  "sources": [
    {"type": "file", "path": "src/errors.py", "why": "error definitions"},
    {"type": "conversation", "id": "error_strategy", "why": "design discussion"},
    {"type": "test", "path": "tests/test_errors.py", "why": "error cases"},
    {"type": "chat_history", "session_id": "...", "why": "debugging example"},
    {"type": "quest_doc", "url": "error-handling-guide", "why": "best practices"}
  ]
}
```

When the planner finds this card for "How do we handle errors?", it gets:
- What errors exist (file)
- Why we made those design decisions (conversation)
- What errors we care about (test)
- How we debugged a real error (chat history)
- Best practices (quest_doc)

**All together, no lossy summarization.**

## Benefits

1. **Complete Context** — all related sources available, not compressed
2. **Flexible Source Types** — add new types as needed (Slack transcripts, GitHub issues, etc.)
3. **Trackable Outcomes** — cards record whether they helped on the task
4. **Discoverable** — keywords + summary make cards findable by topic
5. **Composable** — one card can pull from many sources

## Next Steps

- [ ] Extend FileContextStore to handle multiple source types
- [ ] Create ChatHistoryCardBuilder for QAR sessions
- [ ] Create TestCardBuilder for test files
- [ ] Create ExampleCardBuilder for reference implementations
- [ ] Add source-type-specific rendering (e.g., test → list of test cases)
