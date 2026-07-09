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

### The solution: turn cards + the same hybrid retrieval stack

`quest_ai_runner.core.turn_context_store.TurnContextStore` stores conversation turns as JSON
card files (under `.quest-context/turns/` by default) using the SAME card format as
`FileContextStore`. It is a full `ContextAssembler` implementation: its `record()` writes a card
after each turn, and its `assemble()` retrieves relevant past turns by IDF keyword overlap --
the same algorithm as file cards. Because turn cards are real cards, any `VectorContextAssembler`
wired alongside them will also embed and semantically rank turn history.

**What a turn card contains:**
- `user` -- the verbatim user message
- `assistant_summary` -- the AI response, truncated to `max_assistant_chars` (default 400)
  with a trailing ``"…"``; only the rendered form is truncated, the full text drives indexing
- `description` -- the full ``User: ... / Assistant: ...`` text, for the vector arm to embed
- `keywords` -- deduplicated keywords extracted from both sides (stopwords stripped, length > 2)
- `files_consulted` -- the `rel_path` list from `outcome["files"]` (what the brain read)
- `created_at` -- ISO-8601 UTC timestamp

**Retrieval policy.** On each `assemble()` call:
1. The most recently stored card is always returned (floor of 1 recent), providing immediate
   conversational continuity regardless of keyword overlap.
2. Up to `max_older` (default 4) additional cards are selected by keyword overlap with the
   current message; cards with zero overlap are excluded entirely (not compressed -- just not
   sent).
3. Selected cards are restored to chronological order.

**Durable memory.** Unlike the old in-memory `TurnMemory`, turn cards survive across sessions.
They are stored on disk under the configured `turns_dir` and are NOT cleared by `/clear` -- they
are long-horizon memory, not a per-session transcript. `/clear` resets only the single-turn
buffer that supplies the `transcript=` argument (the immediately preceding exchange); the card
store continues to grow across all sessions.

### Immediate-turn buffer

For the single pair "what I just said / what the AI just replied," `InteractiveSession` also
maintains `_last_user` / `_last_assistant` instance variables. This provides guaranteed
continuity for the next turn WITHOUT waiting for the card to be written and retrieved. The
`transcript=` parameter to `orch.run_stream()` is built from this buffer:

```python
def _last_transcript(self) -> str:
    if not self._last_user:
        return ""
    asst = self._last_assistant
    if len(asst) > 400:
        asst = asst[:400].rstrip() + "..."
    return f"User: {self._last_user}\nAssistant: {asst}"
```

### CompositeContextAssembler

`quest_ai_runner.core.composite_assembler.CompositeContextAssembler` wraps multiple
`ContextAssembler` instances into one. It calls each on `assemble()` (concatenating non-empty
`context_view` strings with a double newline separator), merges `card_ids` and `stale` lists,
and calls each on `record()` best-effort (never raises). This is how turn cards compose with
file cards or any other assembler:

```python
from quest_ai_runner.core.turn_context_store import TurnContextStore
from quest_ai_runner.core.composite_assembler import CompositeContextAssembler
from quest_ai_runner.adapters.file_context_store import FileContextStore

file_store = FileContextStore(cards_dir=".quest-context/cards")
turn_store = TurnContextStore(turns_dir=".quest-context/turns")
assembler = CompositeContextAssembler([file_store, turn_store])

cfg = RunnerConfig(..., context_assembler=assembler)
```

`InteractiveSession` wires the turn store automatically: if a `context_assembler` is already set
in `RunnerConfig`, the session wraps it with `CompositeContextAssembler([existing, turn_store])`;
otherwise it sets the `TurnContextStore` directly. A consumer that never calls
`InteractiveSession` can wire both explicitly, as above.

## Warm recent-context fallback (no LLM)

Fresh assembly (Step 2, above) runs `assemble()` in a background thread with a 5 second budget so
corpus search never stalls an interactive turn. That is the right tradeoff for latency, but it has
a cost: a slow or timed-out assemble() leaves that turn with NO cards at all, even ones a prior
turn in the SAME conversation already found relevant a moment ago. `core/recent_context.py` closes
that gap with a small, synchronous, no-LLM fallback: the cards a conversation's own recent turns
selected, carried forward and gated by a cheap lexical check so an unrelated new question never
drags in stale context.

**`RecentContextStore` (a tiny Protocol, `core.recent_context`).** Two methods, both
best-effort and NEVER raise:
- `record(key, cards, user_text)` -- persists this turn's selected `card_metadata` for
  conversation `key` (`conv_id` or `quest_id`, whichever the caller supplied).
- `load(key)` -- returns the recent records for that key, most-recent-first, deduped by card id
  (a duplicate id keeps its newest occurrence). Each record is stamped with `turn_index` (0 = the
  immediately preceding turn, 1 = the one before that, ...).

`FileRecentContextStore` is the reference implementation: one JSON file per conversation under
`<root_dir>/recent/<sha1(key)[:16]>.json`, written atomically (temp file + `os.replace`, the same
pattern `FileContextStore` uses). A file holds at most `max_turns` turns (default 8) and
`max_cards` unique card ids (default 24, newest wins when trimming); each turn older than
`max_record_age_days` (default 14) is pruned on write. Each stored card is a compact preview
(id, title, adapter, relevance_score, keywords, files, a preview capped at 1500 characters), not
the full rendered card: a consumer wanting a fresh render gets one on the next turn's normal
assembly path anyway.

**`filter_relevant(records, query_text, *, is_followup, max_cards)` -- the relevance gate.** A
record passes when EITHER:
- `is_followup` is true and the record came from the immediately preceding turn
  (`turn_index == 0`), so a genuine follow-up ("what about that?", "ok continue") gets the
  previous turn's cards for free, no lexical check needed; or
- its keywords/title have real lexical overlap with the current message: a stopword-filtered
  token-overlap ratio of at least 0.15, or at least 2 distinct informative tokens in common.

An unrelated new question (no overlap, and not the immediately previous turn) is dropped. That is
the whole point: recent cards are used only when they are still relevant to the CURRENT input.
Passing records are ranked by (forced-pass or overlap ratio) plus a recency tie-break (7 day
half-life, the same shape as `TurnContextStore`'s own scoring) and capped to `max_cards`.
`is_followup` itself comes from the Orchestrator's existing cheap, no-LLM
`_needs_context_to_understand` check, so this adds no extra latency or model call.

**How the Orchestrator wires it in.** `Orchestrator(recent_context=...)` plus
`OrchestratorConfig.recent_context_enabled` (default True) and
`recent_context_max_cards` (default 6, the cap passed to `filter_relevant`). On each `run()`:
1. `load()` the conversation's recent records, keyed by `conv_id` or `quest_id`.
2. Run them through `filter_relevant()` against the derived goal condition + the literal message.
3. Wait for fresh assembly (Step 2) as usual.
4. Merge: any surviving recent card whose id was NOT re-found by fresh assembly this turn is
   rendered (`render_recent_cards`) and folded into `context_view` and `EVENT_CONTEXT`'s
   `card_metadata`, tagged `adapter: "recent"` so a UI can visually tell carried-over context
   apart from freshly assembled cards. A card fresh assembly DID re-find wins outright; the
   recent-turn copy is dropped rather than duplicated.
5. `record()` the MERGED selection (fresh + surviving recent) back to the store, so the warm set
   follows the conversation forward turn by turn.

Step 4 is what makes this a genuine fallback, not just a cache: `EVENT_CONTEXT` fires whenever
EITHER fresh assembly ran OR a recent card survived, so a turn where the assembler is unwired,
times out, or raises still gets its conversation's own recent context instead of none at all.

Disable it with `QAR_RECENT_CONTEXT=0` (env, read in `cli.py`), or by passing
`OrchestratorConfig(recent_context_enabled=False)` directly; either turns off BOTH the load and
the write-back, leaving `run()` exactly as it behaves with no recent-context store wired.
`QAR_RECENT_CONTEXT_MAX_CARDS` overrides the per-turn cap. `build_orchestrator` resolves the
store via `resolve_recent_context_store`, rooted alongside the card store
(`<cards_dir>/recent`) and wired independently of which `ContextAssembler` (if any) a consumer
chose, since it is keyed purely by `conv_id`/`quest_id`.

## User Input Understanding (Step 1) and the `ConversationStore`

The `transcript`/`TurnContextStore` machinery above answers "what context goes to the run." Before
that, a first-class **User Input Understanding** step answers a prior question: "what does the user
actually mean?" A short or anaphoric message ("ok do it", "the first one", "yes") cannot be acted on
literally, and selecting context off it is wasteful. So Step 1 optionally resolves the message into a
**goal condition** (a self-contained statement of what would satisfy the request), and only then does
context selection (Step 2) run off that goal condition.

```
user_message --> STEP 1: Understand input --> goal_condition --> STEP 2: select context --> plan/answer/deep
                  (pulls conversation context, only as needed)   (assemble + retrieval off the goal)
```

**`ConversationStore` (a storage-agnostic adapter, `core.adapters`).** Conversation history lives in
different places for different consumers (local Claude session JSON under `~/.claude/sessions`; a
Mongo collection in a server). The store hides that behind two methods, both of which NEVER raise:
- `current_slice(conv_id, query, *, recent_turns=4, max_chars)` — a relevant slice of THE current
  conversation.
- `related_slices(query, scope, *, exclude_conv_id, max_convs, max_chars)` — relevant slices from
  OTHER conversations within `scope` (`{user_id, team_ids, since, participant_id}`, interpreted by
  the implementation).

`SessionFileConversationStore` (local files) is the reference implementation; a consumer can supply
any other backend. Wire it via `RunnerConfig.conversation_store`; `run()` takes `conv_id` +
`conv_scope`.

**Selection policy (each message is one TF-DF-IDF document).** Recency alone never admits context:
- **Always in** = the last USER turn only (the anchor; guaranteed present even after `max_chars`
  truncation).
- **Considered, not auto-in** = the recent window plus older turns, all ranked by relevance.
- USER turns are **preferred** (a x1.5 score boost) and rendered verbatim; AI turns earn inclusion by
  relevance even when latest, and are **compacted** by `conversation_format.compact_message`
  (beginning + end + the most salient middle sentences via sentence-level TF-DF-IDF), so a long AI
  answer cannot dominate df/idf. Per-turn scores are length-normalized. The pure algorithm lives in
  `conversation_format.select_current_slice` / `select_related` so any backend ranks identically.

**Fast by construction.** A cheap no-LLM gate (`_needs_context_to_understand`) skips Step 1 entirely
for self-contained messages, so the common case adds zero latency. When the gate fires, ONE cheap
resolve call produces the goal condition; if it reports `MORE_CONTEXT_NEEDED` the store widens
(current → related), and if it still cannot resolve it returns `CLARIFY: <question>` and `run()`
short-circuits to a confirm asking the user. The moment the goal condition is set, an
`EVENT_UNDERSTANDING` event streams it ("Understood as: …").

## Per-goal context and verifier-driven iteration (deep runs)

When the planner dispatches deep work with N goals, each goal is treated as its own goal condition
with its OWN selected context: `_run_deep` builds a per-goal block from
`context_assembler.assemble(goal)` plus a `conversation_store.current_slice(conv_id, goal)` and
threads it into that goal's `context_preamble`. The goal verifier returns, alongside `met`, three
fields that close the loop: `need_more_context` (+ a `context_query` naming what is missing) and an
optional `next_tier`. When a goal is not met and more context is requested, the next iteration pulls
**more** (a fresh assembler read, wider `related_slices`, a targeted retrieval grep — strictly more
than the prior round) and runs at `registry.resolve_tier(next_tier)`, bounded by the existing
`deep_goal_max_iterations` / token budget. This makes the reviewer, not a fixed plan, decide both
what additional context the expensive step needs and how much model to spend on it.
