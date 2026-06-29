# Lessons from Claude Code Source

Reviewed the Claude Code CLI source (codeaashu/claude-code, ~512k lines TypeScript, leaked
March 2026). Twelve concrete patterns with direct QAR applicability, ordered by implementation
priority.

Source files referenced below are paths within that repo.

---

## 1. Diminishing-Returns Detection in the Goal Loop

**Source:** `src/query/tokenBudget.ts`

After 3+ consecutive loop iterations, if token output delta drops below ~500 tokens, the model
is spinning — regenerating without adding substance. This is detected independently from the
"budget exhausted" condition, because they have different fixes: spinning calls for escalation
to deep, not just a hard stop.

Additionally, the model receives a nudge message ("You are at 75% of your context budget. Be
concise.") at each continuation so it can self-regulate.

**QAR application:** The goal-verification loop (`answer_goal_max_iterations`) has a max-turns
guard but no spinning detector. Add: track token delta per iteration; if 3+ iterations produce
< 400 token deltas, stop retrying on the fast path and escalate to deep instead. Also inject a
context-budget nudge into the steering text when the context view exceeds a token threshold.

---

## 2. Typed Loop Transition Taxonomy

**Source:** `src/query/transitions.ts`

All reasons a loop continues or exits are discriminated-union types, not implicit control flow.
`Continue` reasons: `tool_use`, `reactive_compact_retry`, `max_output_tokens_recovery`,
`max_output_tokens_escalate`, `token_budget_continuation`. `Terminal` reasons: `completed`,
`prompt_too_long`, `max_turns`, `stop_hook_prevented`, `aborted_tools`.

This makes the loop a state machine: testable, observable, and extensible without reading the
loop body to understand exit semantics.

**QAR application:** Replace the current mix of string/boolean returns from the goal loop with an
explicit dataclass:

```python
@dataclass
class GoalLoopOutcome:
    outcome: Literal[
        "answered", "escalated_to_deep", "max_turns",
        "context_pressure", "diminishing_returns",
        "verification_failed", "aborted"
    ]
    reason: str = ""
```

The narration layer speaks different lines per outcome. The runner makes routing decisions
(retry vs escalate vs fail) from the typed value rather than ad-hoc string checks.

---

## 3. Multi-Tier Context Compression Pipeline

**Source:** `src/services/compact/autoCompact.ts`, `microCompact.ts`, `sessionMemoryCompact.ts`

Four tiers tried in order before escalating to the next:

1. **Time-based micro-compact** (free): if time since last assistant message exceeds threshold,
   the server cache is cold anyway — replace old tool results with sentinel text, no LLM call.
2. **Cache-editing micro-compact**: use the API's cache-editing endpoint to delete old tool
   outputs without invalidating the cached prefix. Only tool outputs (grep, bash, reads) are
   cleared — conversation text is never touched.
3. **Session-memory compaction**: replace history before `lastSummarizedMessageId` with the
   incremental rolling summary; keep recent messages intact (min 10K tokens, 5+ text messages).
4. **Full LLM compaction**: last resort, sends entire history to a summarization agent.

Circuit breaker: after 3 consecutive compaction failures, skip all future attempts rather than
retrying endlessly. Buffer: stop 13K tokens below the hard limit; reserve 20K tokens for the
compaction output itself.

**QAR application:**

- Tier 1: If > 10 minutes since the last answer, drop context card raw content and keep only
  title/summary before the next iteration.
- Tier 2: After each deep run, strip the deep run's tool outputs from context passed back to the
  goal loop — keep only the final answer the deep agent produced.
- Tier 3: QAR's rolling goal-notes is the equivalent of session memory; make it the primary
  compaction target rather than an appendage.
- Circuit breaker: if `ContextAssembler.assemble()` fails 3 times in a row, skip assembly and
  run with minimal context rather than retrying indefinitely.

---

## 4. Tool Result Clearing with Stable Sentinel Text

**Source:** `src/services/compact/microCompact.ts`

Clearable tool types are explicitly enumerated: file reads, bash, grep, glob, web fetch. The
replacement is a stable sentinel string (`'[Old tool result content cleared]'`). Only older
results are cleared; the most recent N are kept intact. Token estimates are padded by 4/3 to be
conservative.

Non-clearable: conversation text blocks, error messages. The model can read and reason about
the sentinel string.

**QAR application:** QAR context cards are the direct analogue. After the answer loop runs once,
replace the raw content of context cards older than the current iteration with
`'[Context card content cleared — topic: {topic}]'`, keeping the last 3 intact. Card metadata
(source, title, type, file refs) stays so the model still knows what exists.

---

## 5. Dual-Threshold Background Summarization

**Source:** `src/services/SessionMemory/sessionMemory.ts`

Summarization fires only when BOTH are true: (1) token growth since last extraction exceeds
minimum, AND (2) tool call count since last extraction exceeds minimum. OR: token threshold met
AND the current turn has no tool calls (a natural break point).

The forked summarization agent is given write permission to exactly one file and nothing else.
Extraction runs through a sequential lock — no concurrent summarization runs.

**QAR application:** QAR's post-answer notes update should use the same dual threshold: update
rolling goal-notes only when (token delta > 5K OR a deep run completed) AND at least 2 tool
outputs have been processed since the last update. Wrap the notes-updater in an async lock.
Give it write access to only the goal's notes file.

---

## 6. Identical Fork Prefix for Prompt Cache Hits

**Source:** `src/tools/AgentTool/forkSubagent.ts`

When spawning parallel sub-agents, all children get identical conversation history up through
the parent's last assistant message. All tool result blocks in the fork prefix use an identical
placeholder string. Only the final directive differs per child. This brings the cache hit rate
from ~0% to near 100% for parallel forks.

Guard: `isInForkChild()` scans for a boilerplate tag to block recursive forking. The fork
boilerplate includes explicit non-negotiable rules: no sub-agents, use tools directly, commit
changes, report under 500 words, begin with "Scope:".

**QAR application:** When QAR runs parallel retrieval adapters, all agents share the same
quest/goal context prefix. Only the "search for X in Y" directive at the end differs. Enforce
a structured report format on retrieval agents (`Scope / Found / Files`) so QAR's context
assembler can parse without regex heuristics.

---

## 7. Agent-Type Context Stripping

**Source:** `src/tools/AgentTool/runAgent.ts`

Read-only agents (Explore, Plan) have two expensive context sections stripped before spawn:
CLAUDE.md (up to 15K tokens) and git status (up to 40K tokens). These are always stale in
read-only agents and stripping them saves significant token volume at scale.

Before passing parent context to any sub-agent: `filterIncompleteToolCalls()` removes assistant
messages that have tool_use blocks without corresponding tool_result blocks. Without this,
forking mid-conversation causes API errors.

Each sub-agent gets its own read-file cache clone so it never sees the parent's stale reads.

**QAR application:**

- Strip goal-notes and prior-run history from fast-path retrieval agents (they only need the
  current query and the context card list).
- Before passing QAR conversation history to a Claude Code deep run, filter out any partial
  tool exchanges — assistant blocks with tool_use but no corresponding tool_result.
- Give each parallel retrieval adapter its own isolated working state rather than sharing
  QAR's global context object.

---

## 8. Stop Hooks as Composable Turn-End Slots

**Source:** `src/query/stopHooks.ts`

After each LLM turn, a hook chain fires in two categories:

- **Fire-and-forget (non-blocking):** memory extraction, prompt suggestion, dream generation.
  These run in background and do not delay the next turn.
- **Blocking (can veto continuation):** registered stop hooks that return `{blockingError}` or
  `{preventContinuation: true}`.

A single `isBareMode()` guard (set for `-p` scripted calls) skips all background bookkeeping.
The abort signal is checked after each hook batch — user interrupts terminate the chain
immediately.

**QAR application:** Add `post_answer_hooks` as a list of async callables on
`OrchestratorConfig`. Fire-and-forget: narration update, context card eviction, goal-notes
update. Blocking (can veto): goal verification, quality check. Add a `bare_mode` flag (set
when QAR runs in batch/CI or as a subprocess) that skips narration and notes updates,
cutting latency for non-interactive runs.

---

## 9. Exit-Code Semantics by Command

**Source:** `src/tools/BashTool/commandSemantics.ts`

A `COMMAND_SEMANTICS` map overrides per-command exit code interpretation: `grep` exit 1 =
"no matches" (not an error), `diff` exit 1 = "files differ" (not an error), `test`/`[`
exit 1 = "condition false" (not an error). For piped commands, the heuristic uses the LAST
command's exit code.

Without this, every empty grep result registers as a tool failure, polluting the model's error
state and triggering unnecessary retries.

**QAR application:** When QAR's retrieval adapters return empty results, treat "no results
found" as a valid answer, not as a failure requiring retry. More broadly: define explicit
`RetrievalOutcome` semantics — `found`, `empty` (valid, not an error), `error` (retry-eligible).
"Empty" should flow to the goal verifier as context ("searched X, found nothing") not as a
missing input.

---

## 10. Compaction Boundary Integrity

**Source:** `src/services/compact/sessionMemoryCompact.ts` — `adjustIndexToPreserveAPIInvariants()`

When slicing conversation history for compaction, an extra scan ensures no orphaned tool pairs
cross the boundary. If a kept `tool_result` ID is found, its originating `tool_use` (which may
be in the dropped range) is pulled into the kept range. Similarly, streaming decomposes one
logical assistant turn into 2-3 separate message objects with the same `message.id`; these are
found and merged before slicing.

**QAR application:** When assembling context for a Claude Code deep run, never pass a
`tool_result` without its corresponding `tool_use`. Also: when QAR truncates its own context
cards to fit a context window, never cut a card mid-reference-list (a card that references
files should either be kept whole or dropped entirely).

---

## 11. Immutable Config Snapshot at Run Entry

**Source:** `src/query/config.ts`

All feature gates, model settings, and config values are snapshotted once at query entry into
an immutable `QueryConfig` object. The code comment: "Separating these from the per-iteration
State struct makes the loop testable as a pure reducer: `step(state, event, config)` where
config is plain data."

This prevents mid-run config drift (a feature flag flip mid-session causing inconsistent
behavior) and makes the loop replayable for testing.

**QAR application:** QAR reads env vars and `OrchestratorConfig` inside the loop body. Extract
all config at goal-run entry into a frozen dataclass:

```python
@dataclass(frozen=True)
class GoalRunConfig:
    model: str
    max_turns: int
    answer_goal_max_iterations: int
    bare_mode: bool
    retrieval_adapters: tuple
    narration_enabled: bool
```

Pass it through the loop without mutation. Enables replaying a goal run with a known config in
tests, and prevents the loop from observing config changes mid-run.

---

## 12. Async Agent Cancellation Isolation

**Source:** `src/tools/AgentTool/runAgent.ts`

Async (background) agents get their own `AbortController` — they run independently and are not
cancelled when the parent is cancelled. Sync agents share the parent's controller. A
`killShellTasksForAgent(agentId)` call in `finally` ensures background shell tasks die with
their spawning agent even when the agent itself exits normally.

For deeply nested agents writing shared state: there is an explicit `rootSetAppState` alias
that always routes to the true root store, because writes through a nested context reference
silently no-op.

**QAR application:** QAR's parallel retrieval agents share the parent's `asyncio.Event`.
Separate this: retrieval agents that should outlive a goal-loop restart (pre-warming the next
iteration's context) get their own cancellation event. Deep agents (which should be cancelled
when the goal loop exits) share the parent's event. Add a `cleanup()` method to each retrieval
adapter that terminates background fetches on normal exit.

---

## Implementation Priority

| # | Lesson | Effort | Impact |
|---|--------|--------|--------|
| 1 | Typed loop transitions (L2) | 1 day | High — enables narration differentiation + testability |
| 2 | Empty-result semantics (L9) | 2 hours | High — stops false-error retries from retrieval |
| 3 | filterIncompleteToolCalls before deep spawn (L7) | 2 hours | High — prevents a class of context-passing errors |
| 4 | Diminishing-returns detection (L1) | 0.5 day | Medium-high — stops spinning, triggers escalation |
| 5 | Post-answer hooks + bare_mode (L8) | 1 day | Medium — decouples narration from blocking verification |
| 6 | Config snapshot at run entry (L11) | 0.5 day | Medium — testability + drift prevention |
| 7 | Context card clearing (L4) | 0.5 day | Medium — token efficiency in long goal loops |
| 8 | Identical prefix for parallel retrievals (L6) | 1 day | Medium — cache hit rate for parallel context assembly |
| 9 | Multi-tier compression pipeline (L3) | 2 days | Medium — handles long-running / large-context runs |
| 10 | Compaction boundary integrity (L10) | 0.5 day | Low-medium — prevents edge-case context corruption |
| 11 | Dual-threshold summarization (L5) | 1 day | Low-medium — more efficient notes updates |
| 12 | Async cancellation isolation (L12) | 1 day | Low-medium — cleaner parallel retrieval lifecycle |
