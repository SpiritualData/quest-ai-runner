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

## Card-scoped learning: cross-session recall joins the card system

Cross-session recall used to be a disconnected side-channel. On every single turn,
`ClaudeConversationsAdapter.assemble()` re-scanned the user's ENTIRE conversation history,
keyword-gated it against just that turn's query text, ranked the survivors with TF-DF-IDF
(`select_representatives` — see [TF-DF-IDF Sampling](TF_DF_IDF_SAMPLING.md)), and returned an
ephemeral context view. Nothing about that pass persisted: no `last_used_ts`, no `use_count`, no
link to the card the user was actually working on. Every turn paid the full re-scan cost again,
even for a conversation it had already found relevant.

`assemble()` now participates in the card system the same way file and collection references
already do, whenever two things are true for the turn: it carries an ACTIVE card
(`meta["thread_card_id"]`, set by the `CARD_THREAD_GATE` decision — see
[Card assignment: the independent-recall test](context-assembly.md#card-assignment-the-independent-recall-test)),
and the adapter was constructed with a `card_store` (a `FileContextStore`-shaped object; wired
automatically in `config.py`'s default assembler stack).

**The gate widens; the learning narrows.** Two different term sets do two different jobs:

- **Union gate, for surfacing.** The relevance gate that decides which candidate conversations are
  even considered widens from "this turn's query terms" to "this turn's query terms UNION the
  active card's own topic terms" (`active_card_terms` + `gate_terms`, pulled from the card's
  `keywords` plus the natural-language terms of its `name` / `summary` / `description`). A
  conversation about the card's ongoing idea can now surface even when this turn's specific wording
  doesn't match it — the recall is scoped to the *card*, not just the sentence.
- **Intersection learn, for persistence.** Of the TF-DF-IDF-ranked survivors, only the ones that
  overlap BOTH the query terms AND the card terms get LEARNED onto the card (`learnable_candidates`
  + `learn_card_references`). A hit that only cleared the widened gate because of the card, but has
  nothing to do with this specific question, still helps answer this turn but is not written back —
  so a card never accumulates references that only ever mattered to a passing question.

**The logic is a shared, adapter-agnostic module — not private to this adapter.** The
union-gate / intersection-learn / usage-stamp behavior lives in
[`quest_ai_runner/adapters/card_scoped_learning.py`](../quest_ai_runner/adapters/card_scoped_learning.py),
which knows nothing about conversations. It exposes `active_card_terms(card_store, card_id)`,
`gate_terms(query_terms, card_terms)`, `learnable_candidates(candidates, terms_of, query, card)`, and
`learn_card_references(card_store, card_id, candidates, *, ref_type, locator_fn, why, now)`.
`ClaudeConversationsAdapter` is just the first consumer: it fixes only the three conversation-specific
choices (`ref_type="conversation"`, `locator_fn=lambda cid: {"conv_id": cid}`,
`why="cross-session recall match"`) and delegates the rest. Any other adapter can adopt the identical
behavior by supplying its own `ref_type` / `locator_fn` / `why` — no copy-pasting.

TF-DF-IDF selection itself (`select_representatives`) is unchanged by this work; what changed is
what happens to the ranked output, not how the ranking is computed.

**Same usage-recency mechanism files and collections already have.** A learned hit is attached as a
`conversation`-type content item via `FileContextStore.update_card` (the card system's existing
typed-reference schema — see [Card Fields](conversation-cards.md#card-fields)), then immediately
stamped through the existing `mark_sources_used` seam so its `last_used_ts` / `use_count` bump to
now — exactly the mechanism described in the "Per-source usage recency" bullet of
[context-assembly.md](context-assembly.md) (under "The reference implementation —
`FileContextStore`") for file and collection references. Re-selecting the same conversation on a later turn re-warms the
SAME reference (deduped by `conv_id`) instead of duplicating it. The practical effect: a
conversation recalled once keeps getting found by recency on later turns about the same card,
instead of QAR re-scanning the whole history from zero every time.

**Degrades exactly to the old behavior with no active card.** With no `thread_card_id` in `meta`,
or no `card_store` wired, `assemble()` runs the identical prior global keyword + TF-DF-IDF scan —
byte-for-byte what it did before this change.

**Google Chat: structurally ready, blocked on a resolver (out of scope).** `google_chat_adapter.py`
uses the same global keyword + TF-DF-IDF selection, so it could adopt `card_scoped_learning` with a
`card_store` ctor param exactly the way the Claude adapter does. It is deliberately NOT wired,
because a Google Chat thread has **no reference resolver** that can re-fetch it later: the only wired
`conversation` resolver reads local Claude session files, and Chat content is bounded by
`lookback_days` (it drops out of the window). Persisting a chat thread as a `conversation` reference
would leave a **dangling pointer**, which violates the card system's "everything must be resolvable"
principle. The real prerequisite is a chat-content resolver (re-fetch a thread/space by its
resource-name locator), not the learning wiring; that remains open. The adapter's own
`assemble()` docstring records this directly:

> CARD-SCOPED LEARNING (structurally ready, deliberately NOT wired): … blocked on a chat-content
> resolver (re-fetch a thread/space by its resource-name locator); that resolver, not this wiring, is
> the real prerequisite, and remains open/out of scope.

Full thread-to-card *topic assignment* (which thread belongs on which card) is a separate, deeper
problem — its own gate — also out of scope here.

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
