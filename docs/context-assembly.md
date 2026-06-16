# Context assembly — guaranteed, reusable context for every run

> Status: design-of-record for the `ContextAssembler` capability. Implemented behind an
> adapter interface; **off by default** (a consumer opts in by wiring one). The brain stays
> domain-free (hard rule #2): no corpus layout, no card format, and no git logic lives in
> `core/orchestrator.py` — only the `ContextAssembler` Protocol does.

## The problem

The brain gathers context **reactively**: the planner asks "read what?", the `RetrievalAdapter`
greps/reads, the loop re-plans. That is correct for novelty, but it means every task **re-discovers
the same context from scratch** — the same files, the same conventions — at real token and latency
cost, and non-deterministically (a run may miss what a prior run already found).

Two existing mechanisms each fail the other's test:

- A static preamble / always-loaded rules file is **guaranteed applied** but **coarse**: identical
  bytes for every task, so it can't carry task-specific file pointers without bloating.
- Reactive exploration is **task-specific** but **expensive** and **thrown away** after each run.

The missing middle is **task-specific context that is also guaranteed and not re-discovered**. The
runner owns the one place that can deliver it: it assembles the prompt, so it — not the agent's
discretion — can pre-load the right context and pin the model tier before the loop starts.

## Why this is the right shape: Shannon's Data Processing Inequality

The `ContextAssembler` design follows directly from a constraint in information theory. Shannon's
**Data Processing Inequality** (DPI, 1948) states that for any Markov chain X → Y → Z:

```
I(X ; Z) ≤ I(X ; Y)
```

Processing can only destroy information; it cannot create it. Applied to agents: if an intermediate
agent summarizes a corpus and hands that summary to a reasoning agent, the reasoning agent is
strictly less informed than if it had seen the corpus directly — regardless of how good the
summarizer is.

The naive alternative to a context engine is a **"context finder" agent** that reads the corpus and
passes a summary forward. This is a lossy chain, and every handoff is a tax:

```
corpus → [context-finder agent → compressed summary] → reasoning agent
```

The context engine eliminates that agent-to-agent handoff entirely. Context delivery is library
code, not an agent call, so no information is lost at the retrieval seam:

```
corpus → [context engine (library code) → full relevant context] → reasoning agent
```

This also explains why **parallel fan-out is safe but sequential chaining is expensive**. When the
orchestrator fans out sub-questions, each branch receives the SAME original context (no handoff
loss). When it chains (plan → gather → re-plan), it accumulates original-content observations — it
never asks one step to summarize another step's findings for the next hop. Both patterns hold
because the raw sources are preserved at every step.

The practical consequence (verified empirically in `evaluation/`): Claude Code with correct
grounding confirms in 1 tool round instead of 3. Fewer rounds means less opportunity for the loop
to diverge from the original task's context.

## The shape

A `ContextAssembler` is a fifth, **optional** adapter role (alongside Retrieval / Model / Deep /
Escalation). One call, before the loop:

```python
@runtime_checkable
class ContextAssembler(Protocol):
    def assemble(self, task: dict) -> AssembledContext: ...
    def record(self, task: dict, outcome: dict) -> None: ...   # best-effort write-back
```

```python
@dataclass
class AssembledContext:
    context_view: str = ""             # pre-assembled context string, fed to run(context_view=…)
    model_tier_hint: Optional[str] = None   # "haiku" | "sonnet" | "opus" | None
    card_ids: List[str] = field(default_factory=list)   # cards that fed this view
    stale: List[str] = field(default_factory=list)      # cards/files found stale (re-derived)
```

**Guaranteed application.** `Orchestrator` takes an optional `context_assembler`. At the top of
`run()`, when one is wired **and the caller passed no explicit `context_view`**, the brain calls
`assemble(...)`, uses its `context_view`, and adopts `model_tier_hint` as the `model_hint` when the
caller gave none. After a terminal result the brain calls `record(...)` (best-effort, never raises).
Because this lives in `run()`, it applies to **every** task routed through the brain. A consumer
that wires no assembler gets exactly today's behavior.

## The reference implementation — `FileContextStore` (stdlib only)

Cards are the **source of truth**, as plain files; everything else is a rebuildable accelerator.
Matches the repo's zero-dependency rule (core + runner import only the stdlib).

- **Storage:** one JSON file per card under a configurable `cards_dir` (default `.quest-context/cards/`).
  Inspectable, diffable, versioned by git alongside the code it describes.
- **Card schema:**
  ```json
  {
    "id": "subsystem-or-hash",
    "keywords": ["chat", "ai", "conversation"],
    "summary": "what this subsystem is + how it's wired (a few lines)",
    "description": "full orientation text for the vector arm (module docstring + key defs)",
    "is_test": false,
    "weight": 1.0,
    "files": [
      {"path": "rel/path.py", "git_sha": "…", "mtime": 1700000000.0,
       "sha256": "…", "why": "entry point", "symbols": ["run", "execute"]}
    ],
    "conventions": ["pointer to a rule that applies"],
    "provenance": {"created_by_task": "…", "model": "…", "created_at": "…",
                   "last_verified_at": "…"},
    "usage_count": 0,
    "last_outcome": "met|failed|unknown"
  }
  ```
- **No-LLM summary (initial pass).** Bootstrap extracts the code's **own docstrings** — no
  LLM call.  For `.py` files: module docstring first line + top-level class/function names
  with their docstring first lines (``ast`` module only).  For `.md`/`.rst`/`.txt`: first
  heading + first paragraph.  For other code: leading block comment.  The result is stored
  in `summary` (compact, ~400 chars) and `description` (full orientation text for the vector
  arm to embed).  Test files (`tests/`, `test_*.py`, `*_test.py`) are stored with
  `is_test: true` and `weight: 0.5` so they rank below source files when both match the
  same query.
- **`assemble(task)`:** select cards by IDF-weighted keyword overlap with the task text.
  Test-file cards are down-weighted by their `weight` (0.5) so a source file ranks above its
  test file when both match.  For each pinned file, check freshness, then render fresh cards'
  `summary` + file list into `context_view`, flag stale ones.  No LLM call.
- **Staleness (no LLM):** per file, compare a stored fingerprint to the current one —
  **git blob SHA** (`git hash-object <path>` / last-commit lookup) for committed state, **mtime**
  and **sha256** (`hashlib`) for the working tree (catches other agents' uncommitted edits git
  doesn't see yet). Any mismatch → the card's file entry is stale; the store re-reads just that file
  and refreshes the entry. A `path -> cards` index makes invalidation O(1).
- **`record(task, outcome)`:** create or update the card for this task — re-pin file fingerprints,
  bump `usage_count`, set `last_outcome`. This is the learning loop: a one-time exploration becomes
  a durable card the next similar task reuses for free.

## The two prompt gates (doctrine, generic)

Centralized so every agent in the chain acts the same way. The brain's `PLANNER_PROMPT` carries them
(always applied for the orchestrator); the same text is exported for a consumer to prepend to a deep
runner's `context_preamble`.

- **Sufficiency gate (proceed vs explore):** a checklist, not a vibe — can I name and have I *read*
  the files I'll change? Are the applicable conventions in front of me? For "does X handle Y?" can I
  trace the real path or am I inferring? Is there a verification I can run? It is a **sufficiency**
  check ("can the assembled context plausibly complete and verify this task"), **not a presence**
  check. Explore in cheap passes; stop on the context-dry signal (a pass adds no new load-bearing
  file) or a budget; then escalate the model rather than looping.
- **Model-tier gate (cheap by default):** Haiku to find/gather, Sonnet for clear implementation,
  Opus for review/quality/ambiguity/irreversible — **and escalate one tier on a failed verification
  rather than re-running identically.**

## Three retrieval arms and how they divide the problem

The library ships three complementary ``ContextAssembler`` implementations. Each arm covers a
different retrieval need; together they form a complete sparse + dense + exact-content stack.

| Arm | What it indexes | What it finds well | What it misses |
|-----|----------------|--------------------|----------------|
| **``FileContextStore``** (stdlib-only) | Card summaries + symbol names (IDF over keyword metadata) | Files whose path segments or exported symbol names overlap the task text | Rare tokens, exact phrases buried in file bodies; semantic paraphrase |
| **``VectorContextAssembler``** (``[qdrant]`` extra) | Summaries / topics embedded as dense vectors | Semantic / paraphrase matches ("payment pipeline" → ``billing/``) | **Full file content is NOT embedded** -- rare identifiers, exact strings |
| **``BM25ContentStore``** (``[bm25]`` extra) | **Actual file content** -- every token in every file body | **Exact identifiers, rare tokens, specific phrases** in un-embedded content | Pure semantic paraphrase (no embeddings) |

### Why the sparse-content arm matters

The dense vector arm embeds *summaries and topics*, not full documents. A distinctive identifier
like ``XFCALLBACK_7Q2`` or a legacy constant that never appears in any summary is invisible to the
vector index. The IDF arm only sees card metadata (keyword fields and symbol names), not the raw
body of each file.

``BM25ContentStore`` fills that gap: it walks the corpus root, reads each file's ACTUAL TEXT, and
builds a BM25 index over the content. Any token that appears in a file body is searchable, making
the sparse arm the correct first responder for "find every file that uses identifier X" or "locate
the file containing this exact error string."

### Agentic parallel multi-query (BM25ContentStore)

When a ``ModelProvider`` is wired, ``BM25ContentStore.assemble()`` uses the LLM once to generate
``num_queries`` diverse keyword/phrase queries from the task text, then runs a BM25 search for
EACH query IN PARALLEL (``concurrent.futures.ThreadPoolExecutor``). Hits from all queries are
deduplicated by file path (best score wins) and fused. This gives higher recall than a single
query, because different phrasings surface different matching files.

```
Task text
   │
   ├─► [LLM, optional] Generate num_queries keyword/phrase queries (one cheap call)
   │
   ▼
All queries (raw task + LLM-generated)
   │
   ├─► BM25-search each query IN PARALLEL (ThreadPoolExecutor)
   │      ↓ deduplicate hits by file path, keep best score
   ▼
Candidate (path, score) pairs → confidence gate (score >= confidence_threshold)
   │
   ▼
top max_in_view hits, rendered as path + best-matching snippet → AssembledContext
```

When no provider is given, only the raw task text is searched (no LLM call, fully offline).

### Auto-update (content fingerprints)

On every ``assemble()`` call the index is lazily built on first use. Subsequent calls run an
auto-update pass: a cheap sha256 fingerprint check over every file determines which files changed
since the last index; only changed/new files are re-tokenized and the BM25 index is rebuilt over
the updated corpus. Unchanged files reuse their cached token lists. This keeps the index fresh
with no manual re-index step.

### Scope and the optional enrichment path

The stdlib `FileContextStore` (cards + git/mtime staleness + the gates + guaranteed injection +
write-back) is the complete default. **Richer retrieval is a separate, optional `ContextAssembler`
behind the same Protocol**, gated on optional extras, to add only when card volume or corpus size
justifies it (our survey's conclusion):

- repo mapping: tree-sitter + PageRank (aider-style), zero-LLM, to pick load-bearing files;
- retrieval: **bm25s** (pure-Python BM25, via ``BM25ContentStore``) + **FlashRank** (4 MB ONNX
  reranker), both infra-free; **Qdrant-native hybrid (dense + BM25 + RRF)** when a Qdrant
  deployment is already present;
- index-time enrichment: Anthropic-style contextual retrieval (a one-line Haiku blurb per chunk).

None of these enter `core` or the default install; they live behind the adapter, so the zero-dep
guarantee holds for everyone.

## Where the cards are stored (and git)

- **Config, not env (in the library).** `FileContextStore(cards_dir=...)` takes the path as a
  constructor argument. The core/runner never read environment variables (same discipline as
  `QUEST_API_URL`, which arrives via `RunnerConfig`/the consumer, never hardcoded). A consumer is
  free to source `cards_dir` from an env var in its own wiring (e.g. `QAR_CONTEXT_CARDS_DIR`); the
  library does not impose one.
- **Default convention:** `.quest-context/`. 
- **Gitignored by default.** Auto written cards are machine generated state that mutate on every
  run, so they are treated like `qar_state.json`: `.quest-context/` is in the repo `.gitignore`,
  and a consumer that points `cards_dir` into its own repo should gitignore that path too. The cards
  stay inspectable and diffable locally (plain JSON), they are just not committed. Durable knowledge
  is promoted deliberately to a committed CLAUDE.md context-map, which is the reviewed, source of
  truth subset, not the churn.

## Cold start and relevance (measured, zero LLM)

The store seeds itself the first time it is used: on the first `assemble()` over an empty store
with a known repo root, it bootstraps a per-module map (stdlib walk, symbols via the `ast` module
for Python and a regex set for other languages), so the very first task already has context.
Selection is IDF-weighted, not raw keyword overlap, so distinctive terms win.

Measured on this repo (`quest-ai-runner`) as the corpus, no API key, no model calls:

- **Cold start:** 19 module cards built from a fresh repo in ~260 ms, 61 files pinned, 177 symbols
  indexed, 0 LLM calls.
- **Relevance:** "the poller claims a task and reports" routes to the runner module pinning
  `poller.py` / `executor.py`; "fix the orchestrator planner loop" routes to the core module. The
  task lands on the right files before any grep.
- **Staleness over time:** editing a pinned file flips that card to stale on the next `assemble()`
  (`stale=['quest_ai_runner/core/orchestrator.py']`), deterministically, 0 LLM calls.

So the differentiating properties are concrete: instant grounding on a fresh repo, deterministic
freshness, and a context map that compounds as tasks run, all without spending a token to maintain it.

## Quest AI chat integration (both directions)

The assembler composes with the existing chat context paths rather than competing with them. In
`Orchestrator.run()` the final `context_view` is built in this order:

1. **Assembled cards** (this adapter) go first, and apply on EVERY run that has an assembler wired,
   including a Quest AI chat run that already passes its own `context_view` (the bound-quest
   context). Cards are prepended, the caller's context follows, so cards are guaranteed applied and
   never suppress the chat's own grounding.
2. **The caller's `context_view`** (e.g. the Quest AI chat's bound quest, imported quests).
3. **Panel context-docs and chat uploads** (`attachments`): the Documents/context tab in the Quest
   AI panel and inline message files flow through `prepare_attachments` and append to `context_view`
   (and ride the answer as native image blocks when vision-capable).

So one run grounds on cards + the chat's bound context + Quest panel docs together.

**Context added on Quest for an org / team / AI rep (the reverse direction).** Standing context a
team curates on Quest (a team knowledge base, an AI rep's persona/skill context) reaches a run two
ways, both already supported by the seam:

- per message / per panel, as `attachments` (above), which need no new code; and
- as standing context, by implementing a Quest backed `ContextAssembler` (a `QuestContextAssembler`)
  behind the SAME Protocol, which pulls the team/rep context from Quest and returns it as part of
  `AssembledContext.context_view`. Because both are `ContextAssembler`s, a consumer can wire BOTH
  (file cards for code + Quest context for org/team/rep) via a small composite assembler that
  concatenates their views. The brain stays unchanged: it calls one `assemble`, and the composition
  lives in the consumer.

## Transcript management across turns

### The problem: full history on every turn

A naive interactive session appends every "User: X / Assistant: Y" pair to a list and sends the
entire list as `transcript` on every new turn. After 20 turns that is 18 turns of context the
planner must re-read, many of them irrelevant to the current question, at real token and latency
cost. The planner is cheap, but the waste compounds: a long chat about varied topics sends the
whole history of unrelated topics on every single turn.

### The solution: relevant-turn selection (no compression)

`quest_ai_runner.core.turn_memory.TurnMemory` replaces raw accumulation. On each new turn it
builds the transcript from exactly two groups:

1. **Always-recent turns** (default: last 1). The immediately preceding exchange provides the
   conversational continuity that a reply always needs -- what was just said. These are included
   unconditionally. Raise ``always_recent`` if the consumer needs more guaranteed continuity.

2. **Relevant older turns** (default: up to 4). Each older turn is scored by keyword overlap
   with the current message (stopwords stripped, IDF not required). Turns with no overlap are
   excluded entirely -- they are not compressed, summarized, or truncated; they simply do not
   appear. The top-scoring older turns are included, restored to chronological order so the
   planner sees a coherent narrative.

There is no LLM call. There is no compression. Excluded turns are dropped clean, not degraded.
This is the right trade-off: a turn with zero keyword overlap with the current question is very
unlikely to be relevant, so excluding it is almost always correct; the cost of including an
irrelevant turn (wasted planner tokens, diluted context) is much higher than the cost of missing
a marginally relevant one (the planner still has the current context_view and gathered reads).

**Assistant-response truncation.** AI responses are often long, but the planner only needs to
know the gist of what was said -- not the full answer text. ``TurnMemory`` truncates the
assistant side of each rendered turn to ``max_assistant_chars`` characters (default 400) in the
transcript string, appending ``"…"`` when trimmed. The full text is still stored internally and
used for keyword extraction (so older turns remain discoverable by the relevance scorer). Pass
``max_assistant_chars=0`` to disable truncation.

### How to wire it

`InteractiveSession` uses `TurnMemory` by default. For a custom consumer:

```python
from quest_ai_runner.core.turn_memory import TurnMemory

mem = TurnMemory(always_recent=1, max_older=4)

while True:
    user_text = input()
    transcript = mem.relevant_transcript(user_text)
    result = orch.run(user_text, transcript=transcript, ...)
    mem.add(user_text, result.text or "")
```

`TurnMemory` is stdlib-only, has no external dependencies, and never calls a model. Construct it
once per session; call `clear()` to reset (e.g. on a `/clear` command).
