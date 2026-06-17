# Conversation Cards: Making Conversations Discoverable

When you have Claude conversations in your corpus, you can turn them into **context cards** so the orchestrator's planner can discover and select them during context assembly — not just raw text retrieval.

## The Problem

Without cards, conversations are just text you `grep()` through. With cards, they become:
- **Indexed** by keyword + summary
- **Discoverable** by the planner ("what contexts apply to this task?")
- **Trackable** by outcome ("did this conversation help?")
- **Ranked** by relevance + prior usage

## The Solution: ConversationCardBuilder

Convert conversations to cards that FileContextStore can index:

```python
from quest_ai_runner.adapters import ConversationCardBuilder
from quest_ai_runner.adapters import FileContextStore

# Build cards from all conversations in corpus/conversations/
builder = ConversationCardBuilder(
    cards_dir="corpus/.quest-context/cards",
    corpus_root="corpus",
)
builder.build_all_from_directory("corpus/conversations")

# Now use FileContextStore to index them
context_store = FileContextStore(
    cards_dir="corpus/.quest-context/cards",
    repo_root="corpus",  # optional: for git SHAs
)

# When you call orch.run("question"), the planner will:
# 1. See what cards are available (FileContextStore.list_sources)
# 2. Match by keyword relevance
# 3. Load relevant conversation cards
# 4. Include them in the context_view before planning
```

## Card Schema

Each conversation becomes a JSON card:

```json
{
  "id": "auth_design",
  "keywords": ["authentication", "jwt", "password", "security"],
  "summary": "JWT-based auth system with bcrypt hashing",
  "files": [
    {
      "path": "conversations/auth_design.json",
      "sha256": "...",
      "why": "Claude conversation",
      "symbols": ["authentication", "jwt", "password"]
    }
  ],
  "conventions": [],
  "provenance": {
    "created_by_task": "conversation_card_builder",
    "model": "claude-opus-4-8",
    "created_at": "2026-06-17T...",
    "last_verified_at": "2026-06-17T..."
  },
  "usage_count": 0,
  "last_outcome": "unknown"
}
```

### Card Fields

| Field | Purpose |
|-------|---------|
| `id` | Unique identifier (e.g., conversation filename) |
| `keywords` | Extracted from conversation text for semantic search |
| `summary` | First user question + response count + rep name |
| `files[].path` | Path to the conversation JSON file |
| `files[].symbols` | Top keywords as "symbols" for planner discovery |
| `provenance` | Track when card was created, by what model, and if it helped |
| `usage_count` | How many times this card was selected (auto-tracked) |
| `last_outcome` | Whether the last task using this card succeeded/failed |

## Workflow

### 1. Generate Cards from Existing Conversations

```python
from quest_ai_runner.adapters import ConversationCardBuilder

# One-time: build cards from all conversations in corpus
builder = ConversationCardBuilder(
    cards_dir="corpus/.quest-context/cards",
    corpus_root="corpus",
)

# Generate cards for conversations in corpus/conversations/
cards_written = builder.build_all_from_directory("corpus/conversations")
print(f"Generated {len(cards_written)} cards")
```

### 2. Integrate with FileContextStore

```python
from quest_ai_runner.adapters import FileContextStore
from quest_ai_runner.config import RunnerConfig, build_orchestrator

# Create a store that indexes both file docs AND conversation cards
context_store = FileContextStore(
    cards_dir="corpus/.quest-context/cards",
    repo_root="corpus",
)

cfg = RunnerConfig(
    retrieval=...,
    context_assembler=context_store,
    model_provider=...,
)

orch = build_orchestrator(cfg)
```

### 3. Ask Questions — Conversations Are Auto-Selected

```python
# The planner now sees both file docs and conversation cards
result = orch.run("How should we authenticate users?")

# Behind the scenes:
# 1. FileContextStore lists all available cards (files + conversations)
# 2. Matches by keyword: "authenticate" → finds "auth_design" card
# 3. Loads the conversation card into context_view
# 4. Planner sees full conversation history + documentation
# 5. Tracks: did this conversation help? (outcome)
```

## Automatic Updates

Cards are generated once, then **stay in sync**:

```python
# Conversations updated? Regenerate cards:
builder = ConversationCardBuilder("corpus/.quest-context/cards", "corpus")
builder.build_all_from_directory("corpus/conversations")

# FileContextStore auto-detects changes via file SHA
# Next query will use updated conversations
```

## Keyword Extraction

The builder intelligently extracts keywords from conversation text:

**Sources:**
- **Capitalized words** (topics, proper nouns): "Authentication", "JWT"
- **Bracketed terms** `[like_this]`
- **Words after key phrases**: "discuss", "about", "focus on"
- **Frequently repeated words** (3+ mentions): "token", "hash"

Example: conversation about authentication → keywords: `["authentication", "jwt", "bcrypt", "password", "security"]`

## Custom Metadata

Want to add custom keywords or summaries? Edit the card JSON after generation:

```json
{
  "id": "auth_design",
  "keywords": ["authentication", "jwt", "bcrypt", "password", "oauth2"],  // ← add custom keywords
  "summary": "JWT + bcrypt auth system. Also covers OAuth2 migration.",    // ← customize summary
  "files": [...],
  "provenance": {...}
}
```

## Next Steps

- **Embedding**: Extend keyword matching with semantic search (vectors)
- **Outcomes tracking**: Let the planner report whether conversations helped
- **Auto-refresh**: Hook conversation discovery into CI/post-checkin
- **Multi-turn sessions**: Build cards from entire Claude Code chat sessions
