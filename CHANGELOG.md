# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Context assembly timeout no longer throws away work that finished in time: partial results
  are used.** The turn-start assembly collect had an all-or-nothing budget: when the assembler
  overran `QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS` (default 5s), ALL fresh context was dropped for
  the turn even if one retrieval arm had long since completed. The orchestrator now threads a
  soft deadline (slightly under the hard collect timeout) to the assembler via
  `meta["assembly_deadline"]` (a `time.monotonic()` timestamp), and `HybridContextAssembler`
  honors it: an arm that has not finished by the deadline is skipped and the completed arm(s)
  are fused as a partial result, marked with the new additive `AssembledContext.partial` flag.
  The consolidating LLM pass is bypassed when the result is partial or the remaining budget
  could not absorb it (fails never worse, same philosophy as `core/card_filter.py`). Using a
  partial result stays as loud as the timeout it replaces: a WARNING names the degradation and
  `EVENT_CONTEXT` carries a structured `assembly_partial` marker alongside the existing
  `assembly_timed_out`. The true zero-results case is unchanged: when NEITHER arm finished by
  the deadline the hybrid blocks for both exactly as before (an early empty return would read
  as "assembly found nothing" and poison the shared turn cache), so the hard-timeout path, the
  warm recent-card fallback, and the `TurnCardCache` late-recovery contract (a timed-out
  turn-start future is still recoverable mid-loop) all hold. Assemblers that ignore the meta
  hint, and callers that pass no deadline, behave byte-for-byte as before.

- **`cli send` no longer acknowledges tasks that were never enqueued.** Found by live testing:
  `create_task` defaulted to `source="cli"`, which the Quest API rejects (its enum is
  chat / reflection / review), and the client swallowed the 400 into `{}` - so `send` printed a
  confident "I'm looking into it" for a task that would never run. The default source is now
  `"chat"`, `create_task` raises on failure instead of swallowing it, and `send` refuses to ack
  unless the API returned a task id.

### Changed
- **The learned-notes always-recent floor is relevance-gated.** `NoteContextStore.assemble`
  unconditionally included the 2 most recent notes in every turn's context, which bled the
  previous topic into an unrelated next turn. A floored note must now clear a minimal relevance
  bar against the current query, reusing the store's existing keyword-overlap scoring: a single
  shared meaningful keyword keeps it, and when either side yields no keywords the note is kept
  (cannot judge relevance, so do not drop). Genuinely related recent notes still always make
  it; only a clearly unrelated one is dropped. When nothing (floor included) relates to the
  query, `assemble` now returns an empty context instead of a header with stale notes. Set
  `QAR_NOTE_FLOOR_RELEVANCE_GATED=0` to restore the old unconditional floor.

### Added
- **Conscious overseer sees full context: goal verification now carries the turn's L2 context
  layer.** `Orchestrator._verify_goal` judged a worker's output with an EMPTY context layer, so the
  verifier never saw the cards/grounding the worker or answer call actually had (see
  `docs/HANDS_FREE_QUEST_AI_DESIGN.md` sections 4 and 6). It now takes a `context_layer` parameter
  and both call sites thread the turn's ALREADY-RENDERED L2 block through unchanged: the
  deep-goal loop passes that goal's own `per_goal_context` (the same block the worker received in
  its `context_preamble`, kept stable across retry attempts so the cache benefit holds across
  them), and the answer-verification loop passes `grounding_context_layer(context_view)` (the same
  L2 the answer call it is judging used, computed once so it stays stable across regenerate
  iterations). Because the block is byte-identical to what the paired call already sent, the
  marginal cost to a cached lineage is a cache read, not a fresh write. The flattened
  non-layered fallback prompt gains a clearly labeled `CONTEXT AVAILABLE TO THE WORKER` section
  placed before the output-to-judge, so a provider without the layered surface sees the same
  information. A new `QAR_VERIFY_CONTEXT_MAX_CHARS` env var (default 24000) caps the block;
  `truncate_verify_context` drops only the TAIL (keeping the stable head/prefix) and notes the cut
  when the cap is exceeded. Purely additive: an absent `context_layer` renders byte-for-byte the
  old prompt/layers shape, and the unverified-verdict contract (verdict `None` means unverified,
  never a silent trust of the worker's own outcome) is unchanged either way.
- **WS4 (cache economics): one shared prompt-layering path + provider cache wiring.** A new
  `core/prompt_layers.py` primitive (`PromptLayers`, `compose_layers`, `turn_prompt_head`) splits
  every turn-call's prompt into three layers: a stable L1 head (persona + standards), a stable L2
  context/cards layer, and a volatile L3 tail (the step instruction + new message + gathered). The
  plan / re-plan / answer / verify call sites in `core/orchestrator.py` now build their prompts
  through this helper, so calls built from the same turn constants render a byte-identical L1 + L2
  prefix and differ only in the tail. Because provider prompt caches are PREFIX caches, this lets a
  lineage re-read the shared prefix from cache instead of re-sending it (measured baseline: today's
  differently-shaped prompts cache zero tokens). The `ModelProvider` surface gains an additive,
  backward-compatible `layers` kwarg on `plan`/`answer` (an ordered list of
  `{"text": str, "cache": bool}` blocks): `AnthropicProvider` renders the cached blocks as a
  `system` array with `cache_control: {"type": "ephemeral"}` breakpoints (capped at four, via
  `cache_control_indices`), and `GeminiProvider` passes the L1 head as a native `system_instruction`
  with the context + tail as stable-ordered `contents` (which is what lets Gemini implicit caching
  hit; the old flatten-everything-to-one-string shape cached nothing). `MultiProvider` passes
  `layers` through untouched; every other provider accepts and ignores it. Callers always pass a
  faithful flattened `prompt`/`messages` too, and `layers` is only sent to a provider whose method
  accepts it, so a provider (or a consumer's own `ModelProvider`) without the layered surface is
  byte-for-byte unchanged. Also closes the residual within-card leak: a card's content items are now
  RENDERED in a stable order (by item id) while SELECTION stays relevance-driven (the relevance rank
  survives as `priority_rank` metadata), mirroring the card-level `stable_card_order` fix so a card
  whose item relevance drifts turn to turn no longer defeats caching for its layer.
- **WS3: a real deep-worker escalation ladder, generically.** The deep worker is always Claude
  Code (Claude models only), so "goal not met -> a stronger model" only ever does something when
  the ladder actually contains a Claude-runnable id. `QAR_DEEP_MODELS` now means a comma-separated
  list of real Claude model ids/aliases (weak -> strong) and is left UNSET by default, rather than
  defaulting to bare semantic tier names (`fast,balanced,quality,best`) that were never
  Claude-runnable and made the ladder silently inert on a non-Claude deployment.
  `Orchestrator._deep_models` now builds a fallback ladder generically when no explicit ladder is
  configured: it starts from the resolved session model and, if that is not Claude-runnable,
  extends the ladder with any Claude-runnable id it can resolve from the "quality"/"best" tiers
  (e.g. an operator override like `QAR_MODEL_BEST=claude-opus-4-8`), so escalation does something
  real even on a Gemini/OpenAI-primary deployment. The resolved ladder is logged at INFO once per
  deep run; a non-pinned ladder that still comes out length <= 1 logs a WARNING naming the fix.
- **WS3: intent judgment owned by a structured LLM call, regex demoted to prefilter.** The
  force-deep "the user's message requests a change but the turn only proposed it" fallback used a
  regex net (`_message_requests_change`) as its sole judge. The regex is now a cheap PREFILTER,
  decisive (and free) for the common cases; only in the AMBIGUOUS band it leaves undecided (a
  change-verb/wrongness signal fired, but an interrogative opener or a bare "?" ending overrode
  it -- see `_message_change_signal_ambiguous`) does ONE structured LLM judgment
  (`Orchestrator._judge_execution_directive`, tool schema `{is_execution_directive, reason}`) step
  in, at the cheap "balanced" tier (`OrchestratorConfig.intent_judge_tier`), hard-timeout-guarded
  (`QAR_INTENT_JUDGE_TIMEOUT_SECONDS`, default 8s) and falling back to the regex verdict on any
  failure/timeout/parse miss. This can only ever ADD an escalation the regex missed; it never adds
  a blocking call to the ordinary conclusive case and never blocks the turn.
- **One context primitive, reachable at every loop step (unified card/topic context).** Card and
  topic context are no longer trapped in a single turn-start pass. The planner can now request two
  new read ops mid-loop: `{"cards": "<query>"}` runs card/topic assembly for a query through the
  SAME `ContextAssembler` the turn-start path uses, and `{"card": "<id>"}` fetches one known card's
  rendered content (via the assembler's new optional `render_card`; a store that omits it degrades
  to a NAMED "not supported" observation). Turn-start assembly becomes an eager, non-blocking
  pre-fetch into a shared, query-keyed in-run cache (`TurnCardCache`): when it times out, the future
  is kept referenced rather than discarded, so if it lands late a mid-loop `{"cards": <same query>}`
  read serves it from the cache with NO second assembly run. A 5s turn-start timeout is therefore no
  longer unrecoverable for the whole turn. Mid-loop card reads emit `EVENT_CONTEXT` marked
  `midloop: true` (with an `origin` of cache/prefetch/fresh) so consumers see context arriving
  mid-turn. Reference `render_card` implemented on `FileContextStore`, delegated by
  `HybridContextAssembler` and `CompositeContextAssembler`. Failing / absent / timed-out card reads
  return a NAMED observation, never empty.

### Changed
- **A verifier failure is never reported as success.** When goal verification cannot run (LLM
  outage, unresolvable verify tier, parse failure), the deep result is now UNVERIFIED: it reports
  failed with the real reason ("Unverified: goal verification did not run (...)"), instead of
  silently trusting the worker's own exit code. The answer path likewise logs the real reason and
  exits "unverified", never "verified".
- **Mid-loop reads and turn-start context assembly time out loudly.** Each parallel read gets a
  per-operation budget (`QAR_READ_OP_TIMEOUT_SECONDS`, default 60s); a stall reports which named
  operation timed out, never an empty "nothing found". The context-assembly wait is configurable
  (`QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS`, default 5s) and a timeout now logs at WARNING with a
  process-wide counter and an `assembly_timed_out` marker on the context event, instead of a
  debug-level note while all fresh context is silently dropped.
- **Deep runs are never untimed, never silent, and never watch the wrong session.** The
  `claude -p` deep subprocess now always has a wall-clock cap (`SubprocessConfig.timeout_seconds`,
  else `QAR_DEEP_TIMEOUT_SECONDS`, else 1 hour); on expiry its whole process group is killed and
  the run returns a hard failure naming the elapsed time and the limit. The session progress
  monitor no longer gives up after 15 quiet seconds: it heartbeats (default every 10s) for the
  entire subprocess lifetime, so a hung worker is distinguishable from a working one. Each run is
  launched with an explicit `--session-id`, and the monitor watches exactly that session file
  instead of "any new jsonl" (no more cross-attaching to a concurrent session).
- **Card render order is stable (prefix-cache precondition).** Card selection and truncation stay
  fully score/LLM-driven, but the surviving cards now render sorted by card id instead of by a
  score that drifts call to call. The usefulness judgment is preserved per card as
  `priority_rank` (consolidator) / `effective_score` (vector assembler) / `relevance_score`
  metadata; consumers that want "the most useful card" read the field, never list position.
  Rationale: provider prompt caches match on a literal prefix, and measured benchmarks show a
  reshuffled card order makes caching cost more than not caching at all.

### Fixed
- **rep_sync: an empty/failed profile fetch can no longer blank a rep's local skill file.**
  `QuestClient.get_ai_profile` returns `{}` when the GET fails or no rep exists for the
  (team, user) pair; `pull_rep_to_skill` used to render that empty profile into the skill file,
  wiping the managed persona locally, and a later "both"-direction push would then write the wipe
  up to Quest, destroying the profile. `pull_rep_to_skill` now raises `RepSyncError` on an empty
  profile and leaves the file byte-identical, so the poller's best-effort pull logs the failure
  and the task runs with the previously synced file instead.
- **The understanding channel no longer invents a reply, and the context event no longer quotes the
  conversation.** Round 2 of the leak above. Giving the four REPLY-producing calls a voice contract
  killed the third-person narration inside the answer, but two leaks survived it because the answer
  stage does not produce them.
  - **The meta-echo came from a call the first pass missed.** `_derive_goal_condition` (and
    `_understand_input`'s resolver) called `provider.answer()` with **no system prompt**, exactly as
    the answer stage used to. Handed a bare "Hello" with no role to play, a cheap model defaults to
    the only role it knows and ANSWERS: the derived "goal condition" came back as "Hello. How can I
    help you with your Quests today?", which is what the understanding event then carried. Both calls
    now pass `system=GOAL_CONDITION_SYSTEM`, which tells the model it is a request analyser, not in a
    conversation, and must never answer, greet, or ask a question.
  - **A greeting now costs zero LLM calls.** New `is_small_talk()` short-circuits
    `_derive_goal_condition` for a bare greeting/thanks/acknowledgement: the message is its own
    done-standard, so no restatement is generated, no understanding event fires, and the most common
    turn there is drops a round trip. A greeting that carries a real request ("hi, create a habit for
    me") is deliberately NOT small talk and still gets a proper goal condition.
  - **`EVENT_CONTEXT` no longer ships the person's own words.** A conversation-history card is titled
    with the raw user turn it came from (`core/turn_context_store.py`), and both the card titles and
    the assemblers' source `items` rode out untouched, so a consumer rendering them replayed the chat
    back at the person ("Hi", "User: Hi...", "Hello") inside a retrieval panel. New
    `_safe_event_title()` describes history cards instead of quoting them, new
    `_project_sources_for_event()` keeps only path-like items and collapses free text to a count, and
    the event's `text` field carries a card COUNT instead of a concatenation of card titles. The
    event is also tagged `data["internal"] = True`, like the understanding event.
- **The reply channel now carries only the reply.** A chat turn was arriving with the run's internal
  machinery read aloud inside it: an echo of the request ("Understood as: Hello."), a third-person
  narration of the model's own plan ("The user expressed interest in ... I will create a habit
  titled ..."), and a recital of which files and cards had been retrieved. Root cause: the answer
  stage called `provider.answer()` with **no system prompt at all**, so the only instructions the
  model saw were the grounding and sub-question-merge blocks, which are phrased ABOUT the person in
  the third person ("Answer the user's latest message...", "The user asked: ..."). With no voice
  contract, the model mirrored the voice of its own instructions.
  - New `REPLY_VOICE_SYSTEM` in `core/orchestrator.py` is the single voice contract for the reply
    channel, passed as `system=` on every call that produces text the person reads
    (`_grounded_answer`, both `_answer_subquestions` calls, `_synthesize_after_deep`). It states that
    the output IS the message, and forbids the meta-echo, third-person self-narration, source and
    retrieval listings, raw internal state, and progress commentary, one rule per observed symptom.
  - The grounding blocks and the injected `UNDERSTOOD REQUEST` block are now explicitly labelled
    `INTERNAL` with "use it, never quote it, never name its sources".
  - The sub-question merge prompt no longer opens with "The user asked:", which handed the model a
    third-person frame and invited it to narrate the split back.
  - Internal material keeps its own typed channels (`EVENT_UNDERSTANDING`, `EVENT_CONTEXT`,
    `EVENT_STATUS`, narration `EVENT_PARTIAL`) and is now tagged `data["internal"] = True` on the
    understanding event, so a consumer can route it to a debug/details surface instead of the bubble.
  - New `restates_meaningfully()` replaces the byte-for-byte `!=` guard on the understanding event: a
    cheap-tier restatement that only re-punctuated or re-cased the message ("Hello" -> "Hello.")
    counted as a fresh understanding and echoed the person's own greeting back at them.
  - `emit.status("Selected context for goal: ...")` is now `"Working on: ..."`. The old string named
    an orchestrator step and read as leaked machinery in the consumer's live status tick.
  - Covered by `tests/test_reply_voice_separation.py`.

### Added
- **Quest-folder sync now runs automatically every poll scan, not just around a matching task.**
  `Poller.run_once()` calls a new `_sync_all_quest_folders()` that syncs EVERY entry in
  `cfg.quest_folder_map` on every scan (best-effort per entry; one bad folder never blocks the
  others or the scan), so the standing `poll`/systemd/cron process keeps every mapped quest
  folder's `QUEST_SYNC.md` current on its own poll cadence with no task required — the previous
  task-scoped pre-run pull / post-run push hooks still fire additionally, right around a matching
  task, for freshest-at-execution-time behavior. `quest_folder_map` and
  `quest_folder_sync_direction` are now also settable without writing consumer code, via new env
  vars `QAR_QUEST_FOLDER_MAP` (a JSON object, `{"quest_id": "folder"}`) and
  `QAR_QUEST_FOLDER_SYNC_DIRECTION` (`pull`/`push`/`both`), read by `cli.py`'s `_config_from_env()`
  (documented in its module docstring).

### Changed
- **`goal_folder_sync` renamed to `quest_folder_sync`** (module, `RunnerConfig.goal_folder_map` ->
  `quest_folder_map`, `RunnerConfig.goal_folder_sync_direction` -> `quest_folder_sync_direction`,
  `GoalFolderSyncError`/`GoalFolderSyncResult` -> `QuestFolderSyncError`/`QuestFolderSyncResult`,
  `pull_goal_to_folder`/`push_folder_to_goal`/`sync_goal_folder` ->
  `pull_quest_to_folder`/`push_folder_to_quest`/`sync_quest_folder`). A Quest holds a goal plus
  its state and notes, so "quest folder" names the synced concept more precisely than "goal
  folder" did. Also newly exposed on the CLI: `quest-ai-runner sync-quest-folder <quest_id>
  <folder> [--direction pull|push|both]`, so a consumer no longer has to import the module
  directly for a one-off sync.

### Added
- **Cross-environment parity: an interactive context-request fast lane (presence-aware push).**
  Chat on one environment can now fetch fresh local context from ANOTHER environment's runner
  (e.g. a live server's corpus) with a latency that no longer depends on the runner's 900s
  background poll. `Poller` gains a `context_request` handling path
  (`_handle_context_request`): a claimed task carrying a structured `{query, max_chars, ...}`
  request is answered by assembling context LOCALLY via the runner's own `context_assembler`
  (no goal loop, no LLM plan/answer call), truncated to `max_chars`, and reported done via the
  new `QuestClient.report_done_with_data` (plain text plus optional `result_data` card
  metadata). Discovery is also `env_id`-aware now: `QuestClient.discover_due` takes an optional
  `env_id` (multi-environment teams route work to the right runner; the poller passes
  `cfg.env_id`). The runner's OWN thread (`Poller._fast_lane_loop`, started by `run_forever`)
  serves interactive work with sub-poll-interval latency: by default it holds a LONG-POLL GET
  (`QuestClient.wait_for_interactive`, against a new `GET /api/assistant-tasks/wait` backend
  endpoint) open at a time, reconnecting immediately after each return so a live chat
  context-request is answered in close to real time whenever the runner is up; `QAR_WAIT_CHANNEL=0`
  falls back to a short interval poll (`QAR_CONTEXT_POLL_SECONDS`, default 5s, via the new
  `QuestClient.list_interactive_due`). An in-process claim guard (`_claim_slot`/`_release_slot`)
  prevents the background scan and the fast lane from both handling the same task when they
  observe it in the same short window. New `RunnerConfig` fields: `wait_channel_enabled`,
  `context_poll_seconds`, `wait_timeout_seconds`; new env vars `QAR_WAIT_CHANNEL`,
  `QAR_CONTEXT_POLL_SECONDS`, `QAR_WAIT_TIMEOUT_SECONDS` (documented in `cli.py`'s module
  docstring). See `docs/quest-api-contract.md` ("Fast lane for interactive tasks").
- **Query-aware retrieval routing: structured constraints, filter-capable stores, a planner-visible
  filtered query.** Some requests name an explicit time period, topic, who, or kind of content
  ("what did we finish last Wednesday?") — relevance-only ranking under-serves these. The SAME
  goal-condition-derivation call `Orchestrator._derive_goal_condition` already makes for a
  self-contained message now ALSO parses OPTIONAL structured retrieval constraints
  (`time_range`/`topic_terms`/`actor`/`content_kind`) from its one reply (no new LLM call); new pure
  helpers `parse_goal_condition_reply` and `_format_now_block` do the splitting/date-block
  rendering. `run()`/`run_stream()` take a new `now` (ISO date/datetime) so relative expressions
  ("Wednesday", "last week") resolve against the caller's real clock; absent falls back to the
  process clock. `OrchestratorResult` gains `retrieval_constraints`. When constraints are present
  and a `conversation_store` is wired, `run()` makes one bounded, best-effort
  `related_slices(..., filters=constraints)` call and folds it into `context_view` under a labeled
  block, so "what did we do last Wednesday" is answered from conversations that actually happened
  then. `ConversationStore.current_slice`/`related_slices` gain an optional `filters` param (an
  opaque dict core never reads); `SessionFileConversationStore.related_slices` applies `time_range`
  as a HARD filter over cached digest timestamps BEFORE the relevance gate, folds `topic_terms`
  into the ranking query, and DEGRADES to relevance-only (never a silent empty) with a
  `ConversationContext.degraded_note` + a labeled `(Note: ...)` line when the filtered set is
  empty; `content_kind`/`actor` are accepted but not enforced for local session files (no
  structural "kind" to check). `ConversationContext` gains `degraded_note`. The reference
  `ClaudeConversationsAdapter.query` applies the same `time_range` hard-filter-then-degrade rule.
  Planner-visible: `time_range`/`topic_terms`/`actor`/`content_kind` are new generic, optional
  `reads[]` properties in `DECIDE_TOOL`'s schema, documented in the planner prompt — since
  `_exec_one_read` already forwards the whole read spec to `RetrievalAdapter.query(spec)`
  unchanged, no new dispatch path was needed, so the brain can make a targeted, filtered retriever
  call mid-run (the same widening loop that already exists for reads) with zero core plumbing
  changes. New shared date helpers in `conversation_format`: `parse_date_bound`,
  `timestamp_in_range`. See `docs/context-assembly.md` ("Query-aware retrieval routing").
- **Query-aware retrieval routing, card stores: item-level `time_range` filtering.** Card content
  items already carry a `ts`; the turn's parsed `time_range` now reaches them as a HARD filter.
  When `Orchestrator._derive_goal_condition` parses a `time_range`, the orchestrator threads it
  into the assembly meta (`meta["time_range"]`) for BOTH the main-turn `assemble()` and every
  per-goal assembly (`_assemble_for_goal_with_cards`, so deep subgoals inherit it), and into the
  warm recent-context path. `FileContextStore.assemble` then drops content items whose `ts` falls
  outside the range before rendering (items with NO timestamp are ALWAYS kept: absence of a
  timestamp never hides content); a card left with zero items is dropped from the result, and when
  the filter would empty every selected card it degrades to the unfiltered selection led by an
  explicit `(Note: ...)` line, never a silent empty. `core/recent_context.py`'s `filter_relevant`
  and `render_recent_cards` gain an optional `time_range` with the same rules over record `ts` /
  item `last_used_ts` (a fully emptied render degrades to unfiltered with the same note). New pure
  helpers reusing the shared date parsing (no reimplementation):
  `card_content_render.filter_content_by_time_range` (epoch `ts`) and
  `recent_context.ts_in_time_range` (ISO timestamps), both on `conversation_format`'s
  `parse_date_bound`/`timestamp_in_range`. Meta without a `time_range` key is byte-for-byte
  today's behavior, and assemblers that ignore meta keys are unaffected. See
  `docs/context-assembly.md` ("Query-aware retrieval routing").
- **Full-horizon, relevance-first cross-conversation recall in `SessionFileConversationStore`.**
  `related_slices` now considers EVERY session file on every call (no time window or recency
  cutoff: an old-but-relevant conversation is always reachable) while staying cheap as
  conversations accumulate, via a two-stage scan: stage 1 ranks ALL files by a compact per-file
  digest (first/last message snippets, cached per file and invalidated by `(mtime, size)`, so a
  warm call costs one `stat` per file; oversized files get a bounded head+tail raw read instead of
  a full parse); stage 2 fully loads only a hard-capped shortlist (`_MAX_FULL_LOADS`) for precise
  selection + rendering. Candidates whose digest shares no word with the query drop out entirely,
  and a small recency floor (the 2 most recent conversations) always renders regardless of match,
  so an unmatched query pulls ONLY the floor, never unrelated conversations. The store also
  re-lists the session dir at most every 30s, so conversations written after construction become
  reachable, and it no longer parses every file eagerly at construction. New shared helpers in
  `conversation_format`: `scan_conversation_files` (path-only index), `rank_candidates_by_digest`
  (query-overlap-boosted, length-normalized, recency-tie-break digest ranking), `nl_terms`
  (word-level tokens for prose relevance), `query_overlap_boost`, and a `must_include_ids` param
  on `select_related` for caller-supplied floors.
- **Scoped, item-level recent-context usage memory (task execution + item ranking).** Extends the
  warm recent-context fallback below in three ways:
  1. **Deep/background runs now benefit too, not just chat turns.** `Orchestrator._assemble_for_goal`
     (used by every deep subgoal in `_run_deep`) now ALSO loads the scoped warm set, gates it
     against the GOAL text, and merges surviving cards into the per-goal context (dedupe, fresh
     wins) -- the same completeness guarantee a chat turn gets. After a goal completes, the cards
     + items its context actually included are recorded back to the store under every applicable
     scope key, so a follow-up task on the same quest/conversation warm-starts. Verified identical
     under `Mode.BACKGROUND` (the executor's lane) with no `conv_id` in play.
  2. **Three memory SCOPES, consulted together:** `core/recent_context.py`'s
     `FileRecentContextStore` now keys by SCOPE (`"conv:<conv_id>"`, `"quest:<quest_id>"`,
     `"global"`) instead of one bare key -- `record`/`load` take a scope-key list (a single bare
     string is still accepted for convenience, classified as "conv"). `load` merges the scopes
     deduped by card id with narrower-wins precedence (conv > quest > global). conv/quest keep the
     existing 8-turn/24-card/14-day caps; `global` (aggregating everywhere) gets larger 24-turn/
     64-card/30-day caps. `filter_relevant` weights each scope's lexical relevance (conv 1.0, quest
     0.8, global 0.5) plus the existing 7-day recency tie-break; only conv-scope's immediately
     preceding turn gets the follow-up free pass -- quest and global ALWAYS need real lexical
     overlap, so cross-conversation/global memory never drags in something unrelated. New
     `OrchestratorConfig.recent_context_global_enabled` (default True; env
     `QAR_RECENT_CONTEXT_GLOBAL`) turns off ONLY the global scope.
  3. **Item-level usage memory + ranking.** Beyond remembering which CARDS were used, each card
     record now carries its content ITEMS (`{id, type, locator, preview (<=300 chars),
     last_used_ts, input_keywords}`, capped 8/card, unioned by item id across turns), tagged with
     the stopword-filtered keywords of the input that turn was answering. `render_recent_cards`
     ranks a carried-over card's own items by (overlap with the CURRENT input's keywords, then
     recency) before rendering, so previously-useful items lead. A new
     `build_item_usage_hint(records, query_text)` turns this memory into a compact
     `{card_id: [item_id, ...]}` hint threaded into `context_assembler.assemble(..., meta=...)` as
     `meta["recent_item_usage"]`; `adapters/hybrid_context_assembler.py`'s consolidating LLM pass
     (`core/card_filter.py`'s `consolidate_context`) folds it into the prompt as a HINT (prefer
     keeping/ordering these ids first, never a hard override), and the rebuilt `rendered_section`
     now reorders (not just prunes) a card's verbatim item region when the consolidator's returned
     order differs, so ranking reaches the actual rendered text. Assemblers that don't know the
     `recent_item_usage` meta key simply ignore it (purely additive).

  Zero LLM calls in the warm path itself (the consolidator hint rides an LLM call that already
  happens). Tests extended in `tests/test_recent_context.py` and
  `tests/test_orchestrator_recent_context.py`, plus new
  `tests/test_hybrid_context_assembler_recent_hint.py`. Documented in `docs/context-assembly.md`.
- **Warm recent-context fallback for follow-up turns (no LLM).** `core/recent_context.py`:
  `RecentContextStore` (a tiny Protocol, `record`/`load`, both best-effort and never raising) plus
  `FileRecentContextStore`, its reference implementation, one JSON file per conversation under
  `<root_dir>/recent/<sha1(key)[:16]>.json`, capped at 8 turns / 24 unique cards (newest wins) and
  pruned after 14 days, written atomically. `filter_relevant` is a pure, no-LLM lexical gate: a
  genuine follow-up ("what about that?") gets the immediately preceding turn's cards for free, an
  older or unrelated turn's cards need real keyword overlap (ratio >= 0.15 or >= 2 shared
  informative tokens), ranked with a 7 day recency tie-break. `Orchestrator` now takes
  `recent_context=...` and, on every `run()`, loads + gates the conversation's own recent cards and
  merges the survivors into `context_view` and `EVENT_CONTEXT`'s `card_metadata` (tagged
  `adapter: "recent"`) AFTER fresh assembly resolves, deduping by card id (fresh wins). Crucially,
  this merge fires even when the fresh `ContextAssembler` is unwired, times out, or raises, so a
  follow-up turn is warm instead of empty. The merged selection is recorded back to the store for
  the next turn. New `OrchestratorConfig.recent_context_enabled` (default True) and
  `recent_context_max_cards` (default 6); env `QAR_RECENT_CONTEXT` / `QAR_RECENT_CONTEXT_MAX_CARDS`
  (read in `cli.py`), wired via `config.py`'s `resolve_recent_context_store` alongside the card
  store, independent of whichever `ContextAssembler` a consumer chose. Tested in
  `tests/test_recent_context.py` and `tests/test_orchestrator_recent_context.py`; documented in
  `docs/context-assembly.md`.
- **Cooperative mid-run cancellation for background tasks.** `QuestClient.is_task_cancelled(id)`
  checks whether a task's status is `"cancelled"` or its `cancel_requested` field is truthy
  (fail-open: any API error returns False, so a transient hiccup can never kill a legitimate run).
  `Orchestrator.run()` gains an optional `cancel_check: Callable[[], bool]` checked at natural loop
  boundaries (before each plan/gather/replan step, before each deep-goal retry attempt, and once
  more after deep execution finishes); when it reports True the run stops cleanly and returns an
  `OrchestratorResult` with `kind="cancelled"` instead of the usual answer/deep/confirm outcome.
  `None` (the default) is byte-for-byte the old behavior. `TaskExecutor.execute()` builds a
  THROTTLED cancel check (at most one `is_task_cancelled` call per ~15s of real time) and threads
  it into the run. On a cancelled outcome, or when a final unthrottled check right before reporting
  done/failed (or after an orchestrator error) finds the task already cancelled, the executor does
  NOT PATCH a terminal status (the backend already set it and appends its own terminal chat
  message; a PATCH would just 409) and posts only a best-effort status note onto the task's own
  progress stream, returning `ExecutionOutcome(status="cancelled")`. Covered in
  `tests/test_orchestrator.py` (cancel_check boundaries) and `tests/test_runner.py` (executor and
  QuestClient behavior).
- **`QuestClient.post_conversation_message` accepts an optional `task_id`.** Stamped on the body
  only when given (omitted keeps the request byte-for-byte unchanged), so the backend can
  correlate a chat progress post back to the task it belongs to. `TaskExecutor` now passes the
  task's own id on every started/progress/done/decision message it posts into the chat.
- **The goal/claims verification judge now runs at a strong tier, with a risk-managed fallback.**
  New `OrchestratorConfig.verify_tier` (default `"best"`): `_verify_goal` — the ONE small,
  hard-capped call whose verdict gates the whole turn (met/not-met decides done vs
  needs_you/failed; `claims_unexecuted` decides honesty remediation) — resolves its model from
  this tier instead of the mid `planner_tier`, routed via `get_provider_for_model` like the
  overseer. Spend the strong model on judgment, keep cheap tiers for gathering: a wrong verdict
  either ships a false "done" or triggers a full regeneration / deep re-run, both costlier than
  the tier delta. If the strong-tier call fails or returns an unusable verdict, the judge retries
  ONCE at `planner_tier` (the previous judge), so a deployment whose best tier is not servable
  never silently loses the goal/claims gate. `verify_tier=""` restores the old single
  planner-tier call. Env: `QAR_VERIFY_TIER`. Tested in `tests/test_verify_tier.py`.
- **The overseer and verify tier are now configurable from the environment.** The
  minimal-intervention overseer previously could not be enabled in the CLI/poll deployments at
  all (no env wiring existed). `_config_from_env` now reads `QAR_OVERSEER` (1/true enables),
  `QAR_OVERSEER_TIER` (default "best"), `QAR_OVERSEER_MAX_SIGNALS`, and `QAR_VERIFY_TIER`;
  documented in `.env.example`.
- **A degraded overseer consult retries once at the planner tier.** `OverseerSignal` gains a
  `degraded` flag distinguishing a real "proceed" verdict from a failure that fell back to
  proceed (provider error / unusable response). `_submit_oversee`'s background worker retries a
  degraded consult ONCE at `planner_tier` (skipped when both tiers resolve to the same model), so
  a deployment whose overseer tier resolves to a model the wired provider cannot serve no longer
  has a permanently silent overseer that looks exactly like a healthy one. Still fully
  non-blocking; both calls happen in the same background worker.
- **Labeled overseer signal eval.** `evaluation/overseer_signals_eval.py`: 11 hand-labeled
  hook-A/hook-B digests judged by a real model, with contrast pairs (described-vs-done drafts,
  question-vs-instruction phrasing) and no reuse of the prompt's own exemplar phrases. 11/11 at
  `gemini-3.5-flash` on 2026-07-06; see docs/overseer.md.
- **The answer goal loop verifies against the turn's DERIVED GOAL CONDITION.** Step 1
  (`_understand_input`/`_derive_goal_condition`) establishes a checkable done-standard for every
  turn, but the final answer verification judged against the plan's own goal restatement, letting
  the two drift. When a distinct condition was derived it is now the bar the answer is verified
  against; when none was (derivation failed safe / message already concrete) the plan's goal and
  the generic bar apply exactly as before.
- **Native, key-free web search (default-on).** The runner can now ground answers on the live
  web using the model provider's OWN web-search tool, reusing the LLM key it already has, with no
  separate web-search key and no Claude Code subprocess. New `ProviderWebSearchAdapter`
  (`adapters/provider_web_search_adapter.py`) is a `RetrievalAdapter` backed by a `ModelProvider`'s
  native search: `AnthropicProvider.web_search` uses Claude's `web_search` server tool and
  `GeminiProvider.web_search` uses Gemini's Google Search grounding (both added, plus routing in
  `MultiProvider` and the optional `supports_web_search`/`web_search` hooks on `ModelProviderBase`).
  `build_orchestrator` wires it into the retrieval stack automatically whenever the provider
  supports it, so the planner discovers "web" as a source out of the box. Opt out with
  `WEB_SEARCH_ENABLED=false`; a Tavily `WEB_SEARCH_API_KEY` still takes precedence when set. This
  is what lets ordinary AI tasks (e.g. "find marathons near Portland", "suggest a product") return
  current, grounded results instead of "I couldn't find anything". `derive_capabilities` reports
  `web:true` when a native or Tavily web adapter is wired. See [docs/web-search.md](docs/web-search.md).

### Fixed
- **Conversation selection is now actually query-sensitive.** The relevance bias in
  `select_current_slice` / `select_related` was a no-op (`terms | (terms & query_terms)` equals
  `terms`), and prose was tokenized with the path-oriented `extract_terms` (whole-phrase tokens
  that never overlap a query). Turn and digest ranking now applies a real word-level
  query-overlap boost (`conversation_format.query_overlap_boost` over `nl_terms` word tokens), so
  "reachable by relevance to the current input" holds instead of ranking purely by
  distinctiveness + recency.
- **The executor's fallback prior-conversation read is bounded.** A task carrying `conv_id` with
  no `ConversationStore` wired used to dump the ENTIRE linked conversation into the task's
  context via an uncapped `read_section`; it now passes `max_bytes`
  (`executor.CONV_CONTEXT_MAX_BYTES`, 16k), so per-task prompt cost no longer grows with
  conversation length.
- **Over-budget conversation reads keep the recent tail.**
  `ClaudeConversationsAdapter.read_section` used to head-truncate at `max_bytes`, dropping the
  newest turns (the ones a linked task usually needs); it now elides the MIDDLE (new
  `conversation_format.truncate_transcript_middle`), keeping the opening plus mostly the recent
  tail.
- **The answer step is told it cannot make changes.** `_grounding_block` now states explicitly
  that the read-and-answer step cannot edit files, run commands, or change code/data/config, that
  changes happen only through a separate execution run, and that it must never claim a change was
  made, only describe what should be executed. This prevents the hallucinated "I have now directly
  updated and written the changes to <file>" replies at the source; the goal-verification claim
  check (see the broken-promise honesty entry under Added) catches whatever still slips through.
- **Overseer no longer escalates plain questions to a deep task.** `escalate_deep`'s guidance keyed
  off "the request uses an action verb," so a question that merely mentioned one ("how would I add
  X?", "what would it take to fix Y?") read as an action request answered by a read-and-answer plan
  and got escalated, i.e. a question in chat silently turned into a background task instead of being
  answered inline. `OVERSEER_PROMPT` now requires the request be PHRASED AS AN INSTRUCTION (an
  imperative or a polite command like "can you add X") before treating it as an action request, and
  explicitly exempts interrogative questions that merely mention an action verb. Mirrors the
  QUESTION-vs-COMMAND guard the planner-level fallbacks (`_message_requests_change`,
  `_INFO_QUESTION_RE`) already had. Tested in `tests/test_overseer.py`.
- **Web-search spec parsing.** The planner emits varied/nested query shapes (e.g.
  `{"query": {"operation": "web_search", "params": {"query": "..."}}}`); both web adapters
  previously read `spec["query"]` directly and passed a nested dict to the search API, which failed.
  A shared `coerce_web_query` (`adapters/web_query_spec.py`) now robustly flattens these shapes;
  `WebSearchAdapter` (Tavily) and `ProviderWebSearchAdapter` both use it.

### Added (earlier)
- **Minimal-intervention overseer.** A new, off-by-default `Orchestrator` capability
  (`core/overseer.py`): a high-quality model reads a cheap, hard-capped digest of the current run
  (never the full gathered text) and returns exactly one signal, almost always `proceed`. Consulted
  at two points (both non-blocking; see below), after each "worth a look" plan step and once at the
  answer checkpoint with the draft answer included, both capped by `overseer_max_signals` per run. A
  `redirect` feeds one short course-correction hint back to the next plan; `answer_now` stops
  reading and answers with what's gathered; `escalate_deep` hands ROUTINE work to deep execution;
  `escalate_human` hands a genuine HUMAN-ONLY fork to a confirm/decision-request. Fails safe on any
  error (degrades to `proceed`) and is byte-for-byte inert when off (the default, and never spawns a
  thread when off). See [docs/overseer.md](docs/overseer.md). Tested in `tests/test_overseer.py`
  plus wiring coverage; the existing orchestrator/runner/UI tests pass unchanged.
- **`run()` now ALWAYS derives a checkable goal condition, not just for anaphoric follow-ups.**
  This is a core `Orchestrator.run()` STAGE 1 behavior change, distinct from the overseer feature
  above (though it feeds the overseer's `RESOLVED AS` field). Previously, `goal_condition` was only
  computed (via an LLM rewrite) when a cheap keyword check said the message leaned on conversation
  context; a self-contained message got zero LLM calls and `goal_condition` defaulted to the raw
  text unchanged. Context-FETCHING (pulling conversation history to resolve anaphora) stays exactly
  as conditional as before -- that is unchanged. But goal-condition ESTABLISHMENT (deriving a
  concrete, checkable done-standard, mirroring the deep planner's own `goal` field) is now a
  SEPARATE, always-on concern: a self-contained message now also gets ONE cheap-tier
  (`resolve_tier("fast")`, never "best") LLM call via the new `Orchestrator._derive_goal_condition`,
  which restates the message as a done-standard or echoes it unchanged if it already is one. Fails
  safe to the raw message on any error. **This adds one LLM round trip to every turn that previously
  had zero** (a real latency/cost tradeoff, deliberately chosen so the overseer and any other
  goal-condition consumer always sees a checkable standard, not just a disambiguated sentence).
  Tested in `tests/test_goal_condition_derivation.py` and updated
  `tests/test_conversation_understanding.py` cases; several existing tests' expected `answer_calls`/
  `answer_models` counts were bumped by one to account for the new call.

### Changed
- **Both overseer hooks are now non-blocking.** The overseer's provider call runs on a per-run
  background `ThreadPoolExecutor` (mirroring context assembly) instead of blocking the run loop.
  Hook A submits on a "read" plan and keeps walking, then polls the result at the top of the next
  plan step and applies `redirect`/`answer_now`/`escalate_deep`/`escalate_human` one step late. Hook
  B used to wait synchronously up to a short bound on EVERY answer; it now does the same
  non-blocking check as hook A and, if unresolved, ships the draft immediately and hands the pending
  consult to a background finisher (`overseer_background_finish_timeout_seconds`, default 30s) that
  raises a real decision-request for a late `escalate_human` and records a late `EVENT_OVERSEER` for
  `redirect`/`escalate_deep` (best-effort; does not auto-launch unattended deep execution). New/
  changed config: `overseer_poll_timeout_seconds` (default `0.0` = never block, used by both hooks),
  `overseer_background_finish_timeout_seconds` (replaces the old
  `overseer_answer_checkpoint_timeout_seconds`). Off (`overseer=False`) still spawns zero threads.
- **Escalate split into `escalate_deep` / `escalate_human`.** The single `escalate` signal
  conflated "this needs real execution" (routine, AI-doable) with "this needs a human" (identity,
  irreversible/authorization, genuine ambiguity). They are now distinct signals; `escalate_human`
  routes through the confirm/decision-request mechanism instead of `_run_deep`, and the prompt is
  written to keep it rare, mirroring "AI acts first, only genuine forks go to a human."
- **`OVERSEER_PROMPT`'s explicit "no em dashes in your output" rule removed** (a deliberate,
  narrowly-scoped exception, requested for this specific model-facing instruction only; it does not
  change the repo-wide no-em-dash copy convention). The authored prompt TEXT itself is still checked
  to contain no literal em dash (`tests/test_overseer.py::test_overseer_prompt_authored_text_has_no_em_dash`).
- **Cheap, non-LLM pre-filter gate for hook A.** Hook A no longer submits on a blind fixed cadence:
  a free heuristic (`_oversee_worth_a_look`) only lets it submit when consecutive reads cross
  `overseer_gate_min_consecutive_reads` (default 2), the plan repeats the previous step's action+goal
  (`overseer_gate_repeat_plan`, default on), or time/read spend crosses `overseer_gate_spend_fraction`
  (default 0.6) of budget. Hook B is not gated (it is a one-time final check, not a cadence).
- **Overseer digest: grounded request, conversation history, and truncation-order fix.** The digest
  now shows `CURRENT USER REQUEST` (the raw, verbatim message, always) plus an additional
  `RESOLVED AS` line only when the resolved `goal_condition` differs (never a silent replacement); a
  `QUALITY BAR` line carries `quality_standards` when present; a new `RECENT CONVERSATION` section
  shows a few PRIOR turns in this same conversation (deduped against the current request); a new
  `PRIOR ESCALATIONS THIS CONVERSATION` section (always present, "none yet" by default) surfaces
  earlier-turn escalations via a new `run(prior_escalations=...)` param; and the old flat "GATHERED"
  bullets are replaced by a numbered, kind-tagged `OPERATIONS THIS TURN` section. The digest's
  decision-critical fields are now PROTECTED from truncation: the sheddable history sections
  (`RECENT CONVERSATION`/`PRIOR ESCALATIONS`/`OPERATIONS THIS TURN`) are capped and dropped first, so
  `PASS`/`CURRENT PLAN`/`RATIONALE`/`SPEND`/`TIME`/`AGENT'S READ BUDGET` always survive. The old
  `READING: … used` line is relabeled `AGENT'S READ BUDGET: …`, clarifying it is the agent's own
  cumulative reads, not the digest's own size. Default `overseer_digest_char_budget` raised
  1200 -> 1600 to accommodate the new essential sections.
- **Overseer tool schema trimmed for token efficiency.** `ClaudeCliProvider.plan()` appends the
  entire `OVERSEE_TOOL` JSON schema as inline text on EVERY consultation (it cannot force native
  `tool_choice`). Its field descriptions duplicated `OVERSEER_PROMPT`'s prose; they are now bare
  mechanical minimums (full semantics live only in the prompt), cutting the appended schema block
  from ~597 to ~318 characters (~47% smaller) per consultation.
- **`quest-ai-runner chat --check`** validates chat's prerequisites (a reachable model provider,
  and `QAR_CORPUS_ROOT` / `QAR_CONTEXT_CARDS_DIR` if configured) and exits, without opening the
  terminal UI, so a broken setup is caught before launch instead of hanging on a blank screen.
- **Terminal UI: first-run missing-provider detection.** With no API key env vars set, chat falls
  back to the `claude` CLI provider; if that binary isn't on PATH, session setup used to hang
  silently. `textual_ui.py` now checks up front and shows an actionable error naming exactly which
  env var or install step is missing.
- **Terminal UI: decision-awaiting prompt state.** While the AI is paused on a mid-turn question
  (`EVENT_DECISION`), the input placeholder changes to "Reply to the question above to continue…"
  and the question renders with a distinct yellow border prefix, so a pending question doesn't
  blend into the rest of the transcript.
- **Terminal UI: `/status` shows today's token budget** (via the existing `DailyUsageTracker`),
  read-only, when a usage file exists.
- **Terminal UI: shallow turns reuse the "context it used" panel.** Previously only deep runs
  populated the Alt+C future-context panel; shallow turns gathered context cards but discarded them
  at turn end. They now render through the same panel via `_build_shallow_context_bullets`.

### Fixed
- **`EVENT_UNDERSTANDING` was silently dropped by both terminal UIs.** The orchestrator emits
  `EVENT_UNDERSTANDING` right after Stage 1 resolves the goal condition, well before the planner
  loop or answer generation runs, but neither `textual_ui.py`'s `_handle_event` nor
  `interactive.py`'s `_TurnRenderer.render` had a branch for it, so the resolved "Understood as:
  ..." text was computed and discarded. Both UIs now render it as its own line the instant it
  arrives: a cyan diamond (`◆`) in the Textual UI (bold cyan) and the plain-terminal renderer
  (rich / ANSI-color / plain-text fallbacks), visually distinct from the yellow `?`/`┃` used for a
  blocking `EVENT_DECISION` question. Tested in `tests/test_understanding_event_ui.py`.
- **Named deep-runner registry (`deep_runners` + `deep_runner_classifier`) silently never
  executed.** Several gates that decide whether the orchestrator can carry out deep work
  (`_run_deep`'s "no runner configured" bail-out, the broken-promise guard's remediation, the
  overseer's answer-checkpoint escalation, and the "answer describes/claims unexecuted work"
  safety nets) all tested `self.deep_runner is not None`. That was correct for the single-runner
  wiring style, but a consumer using the newer named registry (`deep_runner=None` plus
  `deep_runners`/`deep_runner_classifier`, e.g. one runner for in-app data ops and one for
  open-ended tasks) has real execution capability that these checks didn't see — so a `deep` plan,
  or a safety net trying to defer to `deep` after an answer merely described the action, silently
  did nothing. Separately, the emit/run_id/context_preamble capability checks were computed against
  `self.deep_runner` (always `None` for a named-registry consumer) instead of the runner actually
  selected by the classifier, so even when execution DID happen its exec events (generated code,
  raw output) never reached the live stream. Added `_has_deep_execution_capability()` as the one
  place that answers "can we run deep work at all" and used it at every gate; capability checks
  now run against the resolved per-task runner. Also fixed a latent crash in the exec-event debug
  log line (`ev.text` can legitimately be `None`) that this fix newly exercises. Tested in
  `tests/test_orchestrator_named_runner_registry.py`.
- **Poller: `claim()` returned an ambiguous `{}` on failure, indistinguishable from a
  successful-but-empty response.** It now returns `None` on failure and the poller only marks a
  task's signature handled after a successful claim, so a failed claim (already claimed elsewhere,
  or a transient API error) leaves the task un-marked and it is correctly re-offered on a later
  scan instead of silently dropped.
- **Poller: task completion was reported in submission order, not completion order.** `Poller.run`
  now drains the worker pool with `as_completed` so a fast task is claimed/reported as soon as it
  finishes rather than waiting behind a slower task submitted earlier.
- **`StateStore` / `DailyUsageTracker`: non-atomic writes could leave a corrupt file on a
  crash mid-write.** Both now write to a temp file and `os.replace()` into place, a single
  filesystem operation.
- **`StateStore`'s handled-signature cap evicted an arbitrary subset**, since a plain `set` has no
  defined iteration order. It's now an insertion-ordered dict, so the cap evicts the oldest entries
  first.
- **Terminal UI: clipboard-copy failure gave one generic message** regardless of whether no
  clipboard tool was installed versus a tool being present but failing (e.g. no display). The two
  cases are now distinguished (`_copy_to_clipboard_tool`), each with an actionable message.
- **`interactive.py`'s deprecation warning fired on import**, including when `textual_ui.py`
  imports names from it (which is the recommended path), rather than only when the deprecated ANSI
  rendering path actually runs. Moved to `InteractiveSession.run()`.
- **Deep-run dashboard showed the same subgoal as multiple duplicate entries while it retried.**
  The session monitor derived each `EVENT_EXEC`'s `run_id` from the Claude Code session file it was
  watching, but a retry spawns a brand-new subprocess/session, so each retry got a different id and
  the consumer's dashboard rendered it as a new, separate deep run. `run_goal` now accepts an
  optional `run_id`; the orchestrator passes the subgoal's own stable `task_uuid` (generated once,
  before its first attempt) so every retry reports under the same id. Opt-in via signature
  inspection like `emit`/`context_preamble`, so runners that don't accept it are unaffected.
- **Terminal UI: Tab ("next agent") silently did nothing.** Textual's `Screen` has its own
  built-in `tab` binding (`app.focus_next`) which, sitting closer to the focused prompt in the DOM
  chain, intercepted every Tab press before it ever reached `cycle_deep_run` (the prompt TextArea
  itself doesn't consume Tab). The app's `tab` binding is now `priority=True`, which Textual checks
  in an earlier App-wide pass before that walk, so cycling between concurrent deep runs works
  again. Also: the collapsed dashboard now marks which run Alt+D/Tab/a click currently targets (a
  "▸" marker and distinct color on that run's header), and a run's block in the dashboard is now
  directly clickable (`DeepActivity.on_click` hit-tests the click's row against a `{row: run_id}`
  map returned by the new `_DeepRunTracker.get_dashboard_with_map()`) to expand that specific run,
  the same target Alt+D would open for it, instead of only being reachable via Alt+D/Tab.

- **Specificity ranking signal: prefer the exact subject over a same-category sibling, and tell the
  model which is which.** Dense retrieval ranks by topical similarity, so a query about one subject
  ("result-prediction evaluation") pulls back siblings that share the category head ("evaluation")
  but are a different specific thing ("atom evaluation"), and they score nearly as high. New
  model-free scorer `adapters/specificity.py` measures each query term's power to PARTITION the
  retrieved neighborhood (`p*(1-p)` over candidate document frequency): the term in every candidate
  (the shared category) and a generic ask-word absent from every candidate both fall to zero
  weight, leaving the distinguishing terms of the real subject. A candidate's specificity is the
  fraction of that distinguishing mass it covers. Wired into `VectorContextAssembler` as the
  PRIMARY re-rank key (ahead of the existing recency decay, which stays a secondary factor), so a
  sibling with higher raw similarity is demoted below the true subject. It never gates (the
  similarity floor still decides inclusion) and is neutral when the query has no discriminating
  structure, so it is never worse than ranking without it. The signal is surfaced to the answering
  model as a per-hit "subject match: on-subject / WEAK (missing: ...)" label and as structured
  `specificity` fields on `card_metadata`, so the model reads a grounded on-subject-vs-adjacent
  signal instead of inferring it (pairs with the `SPECIFICITY_GATE` prompt discipline). No model
  call, no global corpus, no word list, no new dependency. Tested in `tests/test_specificity.py`.
  Honest boundary: it distinguishes among retrieved candidates; when the true subject was never
  retrieved at all it goes neutral (left to the similarity floor, the prompt gate, and a future
  cross-encoder/NLI increment).
- **Specificity gate: answer about the EXACT subject asked, not its category.** A new
  `SPECIFICITY_GATE` in `core/context_doctrine.py` closes the failure mode where a question about
  one specific subject got answered from a DIFFERENT subject's material just because both shared a
  category word (e.g. a question about "result-prediction evaluation" answered from "atom
  evaluation" docs). Retrieval ranks by similarity, so sibling topics in the same category
  routinely surface; the gate makes every layer that can ground an answer pin the specific referent
  and refuse to answer from a sibling. It is woven into the planner prompt and the deep doctrine
  (`DEEP_CONTEXT_DOCTRINE`), and enforced again at answer time in `orchestrator._grounding_block`:
  if the context covers only a different specific subject, the model says so, names what it DID
  find, and asks which was meant rather than silently switching subjects. Relevance/specificity is
  primary; recency stays a backup tie-break only (the existing age-label / decay re-ranking in
  `vector_context_assembler.py`) and never overrides specificity. Tested in
  `tests/test_orchestrator.py::test_specificity_gate_is_woven_into_planner_deep_and_grounding`.

### Fixed
- **Guidance card conversion crashed every guidance selection.**
  `GuidanceProvider._to_core_card` passed `description=`/`tags=` to the core `GuidanceCard`
  dataclass, which defines only `id`/`title`/`relevance`/`body`, raising `TypeError` (caught by the
  orchestrator, so runs proceeded with no use-case guidance). It now maps the source card's
  `relevance` (falling back to `description`) onto `relevance`, and the two scorers read
  `card.relevance` instead of the nonexistent `card.description`. Covered by `tests/test_guidance.py`.

### Changed
- **Terminal UI: the AI's reasoning beats now flow into the main transcript, not a bottom strip.**
  The "thinking out loud" narration (the instant ack + the planner's conversational rationale) was
  rendered in a single-line `NarrationBar` pinned above the prompt, updating in place so each beat
  replaced the last. It now writes each beat into the scrolling transcript feed inline, above where
  the answer lands, so the reasoning of what's happening reads as part of the conversation in order
  rather than flashing by at the bottom. Exact consecutive repeats are dropped. The `NarrationBar`
  widget is removed. Tested in `tests/test_future_context_ui.py`.

### Fixed
- **Terminal UI: mouse-wheel scrolling works again, and text selection works without Shift.** The
  Textual app was launched with `.run(mouse=False)` (added so the terminal could do native drag-to-
  select), which disables ALL mouse reporting — so the scroll wheel did nothing and the
  `on_mouse_scroll_*` handlers never fired. A Textual app runs in the alternate-screen buffer where
  the terminal has no scrollback of its own, so the app must consume wheel events itself, which
  requires mouse reporting on. It now launches with `mouse=True` (Textual's default), so: the wheel
  scrolls the transcript; a plain click-drag produces Textual's own in-app selection where the
  terminal/multiplexer forwards drag motion (some terminals and tmux still require **Shift+drag**,
  which also works as the terminal-native selection); **Ctrl+C** copies the selection via OSC-52
  (works locally and over SSH/mobile) and only quits when nothing is selected; and Ctrl+Y still
  copies the last AI reply. Tested in `tests/test_copy_or_quit.py`.
- **Terminal UI: click anywhere to start typing.** The transcript (a `RichLog`) and the scrollable
  side panels are focusable by default, so clicking them moved keyboard focus off the message input
  and keystrokes went to the transcript instead of the prompt — you had to click precisely in the
  input to type. The transcript is now non-focusable, and an app-level `on_click` sends focus back to
  the prompt for any click (covering the still-focusable side panels), so a click anywhere leaves the
  cursor in the message box. Wheel scroll, PageUp/PageDown, and drag-to-select are all unaffected.
  Tested in `tests/test_click_focus.py`.
- **Terminal UI: drag-to-select and copy the transcript actually works now.** A stock `RichLog`
  stores pre-rendered `Strip`s and its `render_line` returns them verbatim — it never paints
  Textual's `screen--selection` highlight and its text extraction doesn't fire, so dragging over the
  transcript did *nothing* (no highlight, no copy), regardless of terminal/tmux native-selection
  behavior. `TranscriptLog` now implements selection itself: it captures the mouse on press, tracks
  the range in content coordinates (auto-scrolling when the drag runs past an edge), paints the
  selection background in `render_line` (background only, so highlighted text stays readable), and
  copies the selected text to the clipboard on release with a subtle "Copied: …" confirmation. A
  plain click (no drag) just refocuses the input. Works over SSH/mobile via OSC-52. Tested in
  `tests/test_transcript_selection.py`; verified end-to-end under a headless Textual mount.
- **Terminal UI: copy to clipboard now actually works, with robustness and ephemeral feedback.**
  Both drag-select and Ctrl+Y copy paths tried only Textual's OSC-52 (which many terminals /
  tmux setups don't honor), wrote confirmation to the scrollback (piling up on repeated
  copies), and gave no feedback when copy failed. Now: try a local clipboard CLI
  (wl-copy/xclip/xsel) first for a definite success; fall back to tmux-aware OSC-52
  when no tool is installed (works over SSH if the terminal supports OSC-52); and show
  ephemeral toasts (2.5 second notifications, not scrollback) with clear status ("Copied:
  clipboard" vs. "sent to terminal — install wl-clipboard/xclip if paste fails"). The
  robustness is unified in `QuestAITerminal._copy_text()` and `_emit_osc52()`, shared by
  both Ctrl+Y (last reply) and drag-select (transcript selection).
- **`ClaudeConversationsAdapter` now wired into the default retrieval stack.** The adapter was fully
  implemented and tested but never instantiated in `_config_from_env()`, so past Claude Code session
  transcripts were invisible to grep during both interactive chat and task runs. It is now added by
  default (searching `~/.claude/sessions` or `QAR_CLAUDE_SESSIONS_DIR`). Disable with
  `QAR_CONVERSATION_SEARCH=false`.
- **Terminal UI toggles use Alt-chords, not bare letters (which were swallowed by the prompt).** The
  "expand agent" toggle was bound to a bare `d` and the context panel to a bare `f`. Textual's `Input`
  consumes printable keys (it calls `event.stop()` + `event.prevent_default()`), so while the prompt
  is focused (almost always) a bare letter is typed into the message instead of firing the binding.
  Both now use Alt-chords that are not consumed: **Alt+D** (expand agent) and **Alt+C** (context it
  used), with all hints/placeholders updated to match. A regression test asserts no binding uses a
  bare printable letter (`tests/test_future_context_ui.py`).

### Changed
- **Deep-task display: show the subgoal (live + record) and summarize what it did instead of a
  wall.** A deep task used to surface as a flat grey wall headed by "Executing work…": the subgoal
  it was assigned was never shown, its final output (emitted on a milestone) was never rendered, and
  the permanent record replayed every single read/write. Now:
  (1) `Orchestrator` stamps each subgoal's `goal` onto its `EVENT_EXEC` events and tags the
  completion `EVENT_MILESTONE` with the run's `run_id`, so a consumer can show WHICH subgoal a run
  serves and attach that run's final output to it.
  (2) The live dashboard (`_DeepRunTracker.get_dashboard`) renders the subgoal as its own prominent
  (bold cyan) header line above the live action lines, shown fully instead of cut at 60 chars.
  (3) The scrollback record (`_flush_deep_run`) is now a SUMMARY, not a replay: the subgoal header, a
  one-line activity roll-up (`_summarize_exec_lines`, e.g. "12 reads · 3 edits · 2 commands"), and
  the worker's full final output under a `result` rule. The per-operation trace stays available live
  and in the detail panel (Alt+D). A run with no structured result falls back to the worker's own
  narration (capped); a run with only a result is still recorded. New `_DeepRunTracker`
  `set_final_output()`/`update_goal()`.
  (4) The Alt+D detail panel (full per-action trace) now works AFTER a run finishes: finished runs
  with actions are kept in a capped cross-turn archive (`_deep_archive`), so Alt+D/Tab can replay a
  task's every action even after later turns rebuild the live tracker. `_open_detail_for` and the
  toggle/cycle actions source from live runs first, else the archive (toggle opens the most recent
  finished run); the scrollback summary shows an "Alt+D: see every action" hint when there are
  actions. Tests in `tests/test_deep_output_ui.py`.

### Added
- **Semantic card dedup/merge: the post-deep updater merges into a clear twin instead of creating
  one.** When `Orchestrator._update_cards_after_deep` would CREATE a new context card, it now first
  asks the card store's OPTIONAL `find_similar_card(text, *, user_id, min_score)` capability whether a
  sufficiently-similar card already exists for THIS user (by embedding COSINE similarity) and, if so,
  REDIRECTS the edit to UPDATE that card (merging the proposed name/description + content) rather than
  minting a near-duplicate. The capability is detected by duck-typing, exactly like the existing
  card-update detection: `QdrantCardRepository.find_similar_card` implements it by reusing the SAME
  document embedder + cards collection it already embeds cards into on write (no second embedding
  path), scoped to the user's `u:<user_id>:` id-namespace so a merge can never cross users;
  `FileContextStore.find_similar_card` delegates to the repo when present. A store with no embeddings
  (the default `FilesystemCardRepository`) exposes nothing, so the updater cleanly degrades to
  create-as-before (no string/fuzzy fallback). Threshold is the new `OrchestratorConfig`
  `card_merge_similarity` (default `DEFAULT_CARD_MERGE_SIMILARITY` = 0.85, clear-twin only); a value
  of `1.0` disables the behavior. Edits that already target a shown existing card are untouched, and
  the whole path is best-effort (any miss/error -> create as before). Tests in
  `tests/test_async_card_update.py` (orchestrator wiring: redirect, no-match, capability-absent,
  user-scope isolation, existing-id-untouched, threshold-1.0 disable) and
  `tests/test_qdrant_card_repository.py` (offline embedded-Qdrant: same-user match, cross-user
  isolation, threshold gating).

### Changed
- **Recently-used / recently-updated cards win a near-tie in keyword retrieval (bounded boost).**
  In the keyword arm, `usage_count` and `last_verified_at` were only TIE-BREAKERS, so a card the user
  just relied on did not actually resurface more readily. `FileContextStore` now applies a small
  bounded multiplier (`_recency_boost_factor`, default cap +20%, configurable via the new
  `recency_boost_max` constructor arg, set 0.0 to disable) to the RANKING score, blending a usage
  signal (saturating at 5 uses) with a recency signal (newest content `ts`, 30-day half-life). The
  CONFIDENCE GATE still uses the un-boosted relevance score, so an irrelevant-but-recent card is never
  resurrected; a card with no history is unaffected (factor 1.0). Tests in
  `tests/test_card_content.py::TestRecencyBoost` (including gate-independence).
- **Card content de-duplicates references on write (no more accumulating the same pointer).** The
  post-deep card updater (and `record()`) appended content items with no dedup, so re-adding the
  same collection id / file path / note across runs stored duplicate items until the recency-trim
  dropped the oldest. New `content_identity_key()` + `dedupe_content()` (in `card_content_render.py`)
  collapse items that point at the SAME reference (collection by id, file by path, note by text) into
  one merged item, keeping the existing item's stable id and refreshing to the newest `ts` + freshest
  non-empty `why`. Applied in `FileContextStore._update_card_inner` (after additions, before the
  recency-trim) and in `_record_inner`. Within one card / user scope only; lossless (no information
  dropped on merge). Tests in `tests/test_card_content.py::TestContentDedup`.

### Fixed
- **Narration beats no longer assert ungrounded conclusions before the search finishes.** The
  relayed planner-rationale beats (the conversational "thinking out loud" lines) were told to be
  "specific and opinionated," which pushed the model to declare conclusions from partial GATHERED
  (e.g. "I'm not seeing a spec, so we must be relying on native translation") before it had read
  enough. The re-plan rationale instruction now requires the beat to speak only to what GATHERED
  actually shows, to state an absence as "I haven't found X yet" (never as "X doesn't exist" or a
  substitute fact), and to voice an unconfirmed hunch as a hunch or question, not a settled fact. The
  step-0 instruction now forbids naming what it expects to find before any read. Test
  `test_narration_rationale_instructions_demand_grounding_discipline`.
- **Hybrid consolidation no longer guts a card's non-item content.** The consolidating rebuild used
  to reconstruct `context_view` from each card's content `items` alone, pasting only `item.text`
  under a `### <title>` header. That silently dropped everything a rendered card section carries
  beyond its items: a keyword card's summary, its file PATH LISTINGS (the core value of the IDF arm,
  which are not content items), and its conventions. A keyword card with no items was skipped
  entirely. The rebuild now starts from each surviving card's VERBATIM `rendered_section` (the whole
  block both arms already emit). Both arms attach `rendered_section` to their `card_metadata`
  (`adapters/file_context_store.py`, `adapters/vector_context_assembler.py`); item pruning is applied
  by REMOVING the pruned items' exact rendered fragments from that verbatim section (never
  re-synthesizing it), with a guard that leaves the section intact when a fragment cannot be located.
  Item-less file/reference cards now participate in consolidation too (`consolidate_context` /
  `_validate_consolidation` in `core/card_filter.py` keep a named card with an empty items list as
  "keep the whole card"). The orchestrator's deep preamble (`_materialize_deep_context`) likewise
  pastes each card's verbatim `rendered_section`, swapping only a pointer-delivered file item's
  fragment for a pointer line. New tests in `tests/test_vector_context.py` (mixed item/file-only,
  prune-by-removal, drop), `tests/test_consolidate_context.py` (file-only card keep/drop), and
  `tests/test_per_goal_context_iteration.py` (deep preamble rendered_section + pointer swap).

### Added
- **Expandable "Context it used" panel in the Textual terminal UI.** After a deep run completes, the
  orchestrator's FUTURE-CONTEXT bullets (framed for the user as the context the AI used and judged
  important for what it did, not internal memory plumbing) are surfaced in a collapsible
  `FutureContextPanel`. A dim hint line appears in the transcript ("Alt+C: Context it used (N)");
  press **Alt+C** to toggle. The toggle uses Alt+C rather than a bare letter because the prompt Input
  consumes printable keys, so a plain 'c'/'f' would be typed into the message instead of toggling
  (Alt+C is not consumed and fires reliably while typing). The panel is never shown when there is
  nothing to surface. A pure helper `_build_future_context_text()` builds the Rich Text and is
  testable offline without a Textual event loop. Tests in `tests/test_future_context_ui.py`.
- **Consolidating holistic context filter (one LLM pass over the merged card set).** After both
  retrieval arms each filter their own cards, `HybridContextAssembler` now runs a single
  consolidating LLM call over the merged, deduped card set that (a) drops tangential or redundant
  cards across arms, (b) reranks the survivors, and (c) prunes which content ITEMS inside each kept
  card survive. Content stays VERBATIM (the model selects card ids and item ids only, it never
  rewrites text), via the new `consolidate_context()` in `core/card_filter.py` (parsed with the
  existing `_extract_json`). The hybrid then rebuilds `context_view` from only the surviving
  cards/items, in the returned order, under `### <title>` headers. It engages only when a
  `model_provider` is wired (the balanced tier, set in `config.py`) and at least one merged card
  carries structured items; on no provider or any failure it falls back to exactly the prior
  mechanical merge (the never-worse guarantee). Each arm now attaches structured `items` (and a
  `title`) to its `card_metadata`, built by the new `render_card_content_blocks()` in
  `adapters/card_content_render.py` (which `render_card_content` is refactored to reuse, byte for
  byte). New tests: `tests/test_consolidate_context.py`, plus consolidation cases in
  `tests/test_vector_context.py`.
- **Deep preamble materializes context paste-vs-pointer.** The per-goal DEEP context is now rendered
  from the assembled `card_metadata` items: a file item tagged `deliver="pointer"` (and
  `pointer_eligible`) becomes a short pointer line naming the path (the worker re-reads the file with
  its own tools), while everything else is pasted verbatim. The planner/answer path still uses the
  fully-pasted `context_view`. The `EVENT_CONTEXT` event now carries a lightweight projection of the
  card metadata (ids, titles, per-type counts, file paths) instead of dumping each item's full text.
  File-only deployments with no structured items keep using `context_view` unchanged.

### Fixed
- **The instant response now goes out the moment it is generated, never sequenced behind context
  search or guidance.** The narration first beat was generated in the background but only EMITTED at
  `Narrator.flush_first()`, which the orchestrator calls after the (potentially slow) guidance
  `select()` and the search-status emission. So even though the one-sentence ack was ready in ~1s, it
  could not appear until the main pipeline reached that point. The first beat now emits ITSELF from
  its background thread the instant the model returns (`Narrator.begin` → `_gen_and_say`), decoupled
  from the main pipeline; `flush_first()` is now only a join barrier that preserves ordering (the ack
  still precedes the planner's later relay beats). Emit is lock-guarded so the background ack and a
  main-thread relay beat never interleave. Safe because every consumer funnels events through a
  thread-safe queue (`run_stream`) or marshals to the UI thread. Regression test
  `test_narrator_first_beat_emits_from_background_without_flush`.
- **The instant "thinking out loud" line now actually shows while context is being searched (terminal
  UIs).** The orchestrator emits the narration/instant-ack beat as `EVENT_PARTIAL` tagged
  `data={"narration": True}`, and the frontend already consumed that key, but both terminal sessions
  (`interactive.py`, `textual_ui.py`) still gated on the legacy `data["ack"]` flag. So the immediate
  acknowledgment was never recognized: the Textual UI misrouted it into the discarded answer buffer
  (the real answer comes from `final.text`), and nothing appeared during the search. Both consumers
  now recognize `data["narration"]` (keeping `ack` for back-compat) and render it as a dim line.
  Regression test `test_instant_ack_emits_narration_flagged_partial`.
- **The context-search status reads "searching context…" instead of "searching corpus…".** That
  stage is the `ContextAssembler.assemble()` call (a hybrid keyword + vector search over the wired
  context cards plus turn history), not a single named source. "searching context" names the stage
  (STAGE 2: FIND CONTEXT) honestly and matches what the user expects to see.

- **The internal FUTURE-CONTEXT section no longer leaks into the deep output shown to the user.**
  When the async card updater is active, the deep worker is asked to END its output with a
  `=== FUTURE CONTEXT (for similar requests by this user) ===` section that the updater parses to
  learn reusable pointers. That section is plumbing, not part of the deliverable, but it was being
  shown verbatim at the end of the user-facing deep result. New `_strip_future_context()` removes it
  from every user-facing surface (the final deep `EVENT_RESULT`, the `deep_output` milestone, the
  "Worker output" not-met milestone, and the deferred-deep fold-in) while `DeepResult.output` stays
  raw so `_parse_future_context()` / the card updater still see it. Stripping and parsing split the
  output at the same delimiter, so they are complementary with no overlap. Tests
  `test_strip_removes_future_context_from_user_output`, `test_strip_is_noop_without_delimiter`,
  `test_strip_and_parse_are_complementary`.

- **Direct follow-ups are answered from the conversation instead of triggering a fresh corpus
  search.** When a user asked a follow-up whose answer the assistant had JUST given (e.g. it
  described a plan and its file path, then the user asked "what's the filepath?"), the planner still
  ran a full read/grep loop over the corpus — slow, and pointless because the answer was already in
  the transcript it was handed. The planner prompt now leads with a TRANSCRIPT-FIRST principle:
  before choosing `read`, check the RECENT TRANSCRIPT and GATHERED, and if the answer is already
  there (the common direct-follow-up case) answer straight from it; only `read` when the current
  message genuinely needs substance the conversation does not already contain. The transcript NOTE
  carries a matching carve-out so a direct follow-up is treated as the user explicitly asking about
  the prior turn. Regression test
  `test_planner_prompt_instructs_answering_followups_from_transcript`.

- **Capability/discovery menus no longer pollute the answer (the planner stops answering from the
  operations list).** The auto-injected `list_operations` menu (and any `list_sources`/`describe_*`
  discovery read) was added to `gathered` and rendered to the answer LLM as "ACTUAL CONTENT READ FOR
  THIS ANSWER" — so for a substantive request the model answered FROM the menu ("I have these
  operations; shall I run discover_goals / grep?") instead of gathering the real material (codebase,
  conversation). Discovery observations are now tagged (`discovery: true`) at the read site and: (1)
  excluded from the answer grounding entirely (`_grounding_block`) — when only a menu was gathered,
  there is no answer-content section, so the model grounds on real context or says it lacks it; (2)
  excluded from a single-goal deep run's `context_preamble` (the worker has its own tools and does
  not need the orchestrator's operations menu); (3) relabeled in the planner/worker view as
  "AVAILABLE CAPABILITIES — a MENU of what you can call, NOT content"; and (4) the planner prompt now
  states a capability listing is not "real content" and it must read actual sources before
  answering. Regression test `test_discovery_listing_is_not_answer_grounding_content`.

- **Each finished deep task now carries its FULL output on the completion milestone**
  (`data.deep_output`), so a consumer can show exactly what the task produced rather than only a
  "Completed: <goal>" line.

- **A deferred deep run's output is now folded into the final answer, instead of being discarded.**
  When the planner routed to `answer` but the turn then escalated to a deferred deep run (because the
  message requested a change, or the answer only described unexecuted work), the deep worker did the
  real work but the user-facing reply stayed the pre-deep proposal — typically a "shall I proceed?"
  stub with zero awareness of what the deep run produced. The deliverable was emitted only as a side
  milestone (which some consumers, e.g. the Textual UI, truncate to its first sentence), so the real
  result was effectively lost and the turn looked like it stopped to ask permission for work it had
  already finished. Now, after a deferred deep run produces substantive output, the orchestrator
  re-synthesizes the final reply grounded in that output (new `SYNTHESIZE_AFTER_DEEP_PROMPT` +
  `_synthesize_after_deep`), grounds `context_view` in it, and still holds the synthesized reply to
  the overall goal via the answer-goal-verification loop (so the turn keeps going if the deliverable
  is incomplete rather than stopping half-done). Regression test
  `test_deferred_deep_output_is_folded_into_final_answer` in `tests/test_orchestrator.py`.

- **Progress narration no longer repeats itself.** When narration is on, the per-step beats were
  drifting into the same line reworded several times ("looking up the details of your marathon
  quest" six times). Two changes: (1) the planner's narration instructions are tighter and the
  re-plan instruction now tells it to react to what it just found in GATHERED and say what that
  makes it check next (bridge insight to intent), not narrate a read in isolation; (2) `Narrator`
  now backstops with a normalized near-duplicate check (`_is_repeat`/`_norm`) so a paraphrase of an
  earlier beat is dropped, not just an exact echo. Narration tests in `tests/test_ux_features.py`.

- **The planner now distinguishes a QUESTION from a COMMAND, so asking ABOUT something is answered
  instead of being executed as a task.** Routing was keyword-driven: a message containing an action
  verb ("fix", "add", "change", "build", "update", "show"…) was treated as work to do, even when the
  user was only ASKING — "how would I add a field?", "what would it take to fix the back button?",
  "should we refactor this?" These were mis-handled as deep tasks instead of being answered. Two
  changes fix it: (1) the `PLANNER_PROMPT` now opens with an explicit QUESTION-vs-COMMAND gate that
  decides intent BEFORE the code-change rule — an interrogative ("how/what/why/should we/is it…") or
  a "?" message that asks about something is a QUESTION → answer; a plain or polite imperative ("fix
  X", "can you add…", "please update…") is a COMMAND → deep; when torn, answer. (2) the message-intent
  escalation fallback `_message_requests_change()` no longer auto-escalates a question that merely
  contains a change verb: an interrogative message returns `False` (answer), while a polite imperative
  aimed at the assistant ("can you fix…", "please add…") still returns `True` (execute). Genuine
  commands and bug reports are unaffected. Regression tests in
  `tests/test_orchestrator.py::test_message_requests_change_distinguishes_questions_from_commands` and
  `::test_question_with_change_verb_is_answered_not_executed`.

- **Async hand-off deep runners no longer trigger a runaway relaunch loop.** A `DeepRunner` that
  does not execute inline but queues the real run to finish out-of-band (e.g. a chat runner that
  creates a tracked task and returns a `DeepResult(met=True, output="task #N launched")` sentinel)
  was being re-verified by the deep-goal loop: the verifier judged the sentinel `output` against
  the goal, found it not-met, escalated the model and RELAUNCHED a fresh task — every iteration, up
  to `deep_goal_max_iterations`. The result was N phantom tasks per message and a stream that ended
  in error with no reply. `DeepResult` gains a `deferred: bool = False` flag; when a run is
  `deferred`, the goal loop TRUSTS its `met` and stops (no re-verify, no relaunch). The real outcome
  is verified when it reflects back. Inline runners are unaffected (default `False`). Regression test
  in `tests/test_per_goal_context_iteration.py::test_deferred_handoff_runs_once_and_is_not_reverified`.

- **Context-card RETRIEVAL quality: vector-selected cards now RESOLVE their references, and an
  unrelated query returns no card.** Two retrieval bugs made learned cards unusable end to end even
  though card LEARNING was correct:
  1. **References were resolved only in the keyword arm.** Resolution lived inside
     `FileContextStore._render_card_content`, so a card SELECTED by the semantic
     `VectorContextAssembler` rendered only its description and silently dropped the live
     collection / conversation data it pointed at. The render logic (recency+relevance ranking,
     budgeted resolution through the resolver registry) is now extracted into ONE shared routine,
     `adapters/card_content_render.py::render_card_content`, used by BOTH arms. `VectorContextAssembler`
     gained a `reference_resolvers=` param and resolves each selected card's `content` via the shared
     routine; `QdrantCardVectorStore.search` now forwards the card's `content` (and `card_id`) in the
     hit payload so the vector arm has the references to resolve. `FileContextStore` is unchanged in
     behavior (it delegates to the same routine).
  2. **No relevance cutoff: every query returned ALL of a user's cards.** The confidence gate now
     applies to the hit's RAW similarity score (recency decay only re-orders hits, it never pushes a
     still-similar hit below the floor), making a Voyage-cosine card floor meaningful. Calibrated
     against the card-quality eval (on-topic cards score ~0.50-0.58, incidental ones <=0.41), so a
     ~0.45 floor admits a topic query's own card and drops every card on a truly unrelated query.
     The global default stays the permissive `0.0`; the card wiring sets the floor explicitly.
  Verified by `evaluation/card_quality_eval.py` (real model + Voyage): 0/4 -> 4/4 select+resolve and
  1/1 clean-on-unrelated, stable across runs. New offline regression tests in
  `tests/test_vector_context.py::TestVectorArmCardResolution` lock the vector-arm resolution and the
  raw-score gate. Generic by construction: all resolution still goes through the injected resolver
  registry (a type with no resolver degrades to an unresolved-pointer line).

### Changed
- **Terminal UI: deep-run output is bigger live, scrollable when expanded, and kept on screen after
  each task finishes.** The calm inline deep dashboard now shows a few legible lines per run (3 for a
  single run, tightening to 2/1 as runs go concurrent) instead of a single cramped line. The expanded
  detail panel (`d`) is now a real scroll region holding a run's ENTIRE history (it no longer caps at
  a 22-line tail): it grows to fill the screen, auto-follows the tail, and pages back/forward with
  `PgUp`/`PgDn` (paging up pauses follow; scrolling back to the bottom resumes it). Most importantly,
  each deep task's FULL output is now written into the scrollback transcript the moment it finishes
  (detected via the `EVENT_EXEC` terminal phase, with a turn-end flush as a fallback for cancelled or
  phase-less runs), so what each deep task did stays readable after the live widgets are hidden.
  Tests in `tests/test_deep_output_ui.py`.

### Added
- **Conversational stage narration: one continuous "thinking out loud" train of thought
  (`OrchestratorConfig.narrate`, `narration_system_prompt`).** When `narrate=True`, the orchestrator
  narrates the turn as ONE evolving human train of thought rather than emitting hardcoded status
  labels. The flow is a HYBRID with two beat sources, both emitted as `EVENT_PARTIAL` (so a consumer
  shows them live and speaks them on voice) and both in the selected rep's voice (`rep_preamble`):
  - **The opening beat (instant ack)** is one cheap call on the planner tier, started concurrently
    with context assembly so it adds NO wall-clock latency on quick turns. It is grounded in the new
    message + recent conversation + persona, and (having read nothing yet) claims no findings, so it
    cannot fabricate. HOW it speaks is overridable by the consumer via `narration_system_prompt`
    (the persona is layered on top).
  - **Every later beat is the planner's OWN `rationale`** — when narrating, the planner writes its
    `rationale` field as a short, spoken, in-persona line about what it is doing/noticing now,
    grounded in the gathered observations, in the planning call it ALREADY makes (Approach B: zero
    extra LLM call). The orchestrator relays it via `Narrator.relay()` at read/deep stages, so the
    beat reflects genuine, data-grounded reasoning and quick (answer-only) turns add nothing. The
    `plan`/`replan` event carries no duplicate text while narrating (the rationale is spoken as the
    beat instead of shown as an expandable detail).
  This SUPERSEDES and folds in the old `instant_ack` path (the ack is just the first beat, not a
  separate mechanism; `instant_ack=True` still works and routes through the narrator). Every
  narration failure is swallowed: the turn never depends on it. `Narrator` in `core/orchestrator.py`;
  em dashes are stripped defensively and consecutive beats are space-separated (brand-voice safe).
- **Pluggable card PERSISTENCE boundary: `CardRepository` (`adapters/card_repository.py`).** A tiny
  `CardRepository` Protocol (`load_all` / `read` / `write` / `delete` / `exists` / `revision`, every
  method BEST-EFFORT and NEVER raising) plus the default `FilesystemCardRepository` (one
  `<cards_dir>/<id>.json` file per card, atomic temp-file + `os.replace` writes, and a cheap
  `(max_child_mtime, file_count)` change-stamp for `revision()`). `FileContextStore` now routes
  EVERY card read / write / delete / enumerate / cache-invalidate through an injected repository:
  the constructor still takes `cards_dir` and builds a `FilesystemCardRepository(cards_dir)` by
  default, and accepts an optional `card_repository=` override so a consumer can persist cards in a
  database / vector store while ALL card logic (selection / IDF / recency / the card-update API /
  `export_for_embedding` / bootstrap) stays in the store. The in-memory cache now invalidates on the
  repository's `revision()` (the old `_dir_stamp` filesystem scan was removed). Fully behavior-
  preserving: with the default filesystem repo the on-disk format and selection are byte-for-byte
  unchanged (all existing card/context/vector tests pass).
- **Optional native text-search hook on the repository boundary (`search_cards`).** A repository MAY
  additionally expose `search_cards(query, *, limit) -> Optional[Dict[str, Dict[str, Any]]]` to
  serve the keyword/IDF arm directly from a native full-text store (e.g. a Qdrant-backed repo)
  instead of the store scanning every card in memory. `FileContextStore.assemble()` detects the
  capability by duck-typing (`hasattr`, never an isinstance check): when present and returning a
  non-`None` set, those cards become the candidate pool for the keyword arm (the store then applies
  its existing IDF ranking / confidence gate / recency over them); when absent or returning `None`
  the store falls back to today's in-app IDF over `load_all()`. Purely additive: the default
  `FilesystemCardRepository` does NOT implement `search_cards`, so its behavior is unchanged.
- **Generic Qdrant-backed card persistence: `QdrantCardRepository` + `QdrantCardVectorStore`
  (`adapters/qdrant_card_repository.py`, behind the `[qdrant]` extra).** A reusable
  `CardRepository` that persists context cards as points in ONE Qdrant collection (no `cards_dir`),
  so any consumer can store cards in Qdrant by wiring config/connection only — no consumer
  reimplements a card repo. The connection mirrors `QdrantVectorStore`: pass an existing `client=`,
  OR `url=`/`api_key=` for a server, OR neither for an EMBEDDED local Qdrant under `path`. It takes
  an `embedder` callable (`texts -> vectors`, e.g. `make_voyage_embedder`), a `collection`, a
  `vector_size`, and an OPTIONAL `scope` dict (payload key/values for multi-tenant isolation, e.g.
  `{"user_id": ...}`) — every read/write/delete/search is filtered by `scope` so a scoped card
  never leaks. `write` derives the card's embed-text via the NEW shared `card_embed_text` helper
  (so it MATCHES `FileContextStore.export_for_embedding`), embeds it ONCE, and upserts one point
  `{id from (scope, card_id), vector, payload: card + scope + a flat _search_text field}`;
  `search_cards` uses a Qdrant full-text index + `MatchText`. `QdrantCardVectorStore` is a
  query-only `VectorStore` over the SAME collection (`upsert`/`sync` are no-ops), so each card is
  embedded exactly once. All methods are best-effort and never raise. Wired by env via
  `QAR_CARDS_BACKEND` (`file` default | `qdrant`): when `qdrant`, `resolve_context_assembler`
  builds a `QdrantCardRepository` from `QAR_CARDS_COLLECTION` / `QAR_QDRANT_URL` /
  `QAR_QDRANT_API_KEY` + the `QAR_EMBEDDER_BACKEND` embedder and passes it as the default
  `FileContextStore(card_repository=...)`, with the query-only `QdrantCardVectorStore` as the vector
  arm; `file` keeps today's behavior byte-for-byte.
- **Shared `card_embed_text` helper (`adapters/card_repository.py`).** The single source of truth for
  a card's embed/search text (name + description/summary + content note/why + keywords). Both
  `FileContextStore.export_for_embedding` and `QdrantCardRepository.write` call it, so the vector
  arm's seed/sync text and an embedding repo's write-time text can never drift.
- **Source-agnostic context-card CONTENT (cards are no longer file-only).** A context card
  (`adapters/file_context_store.py`) can now carry an optional top-level `content` list of TYPED
  items, each either a REFERENCE resolved FRESH to current content on every use, or an LLM NOTE
  (synthesized text). Files become just ONE reference type, so a card may have zero `files[]` and
  still be selectable, renderable, and embeddable. Item shape:
  `{"id", "type": "file|collection|conversation|query|note", "locator": {…}, "ts": <epoch float>,
  "why": "<short>"}` (for `note`, `locator = {"text": …}`). Fully additive: a card with no `content`
  and no resolvers wired behaves byte-for-byte as today's file-only card (the existing
  `file_context_store` tests pass unchanged).
- **`ReferenceResolver` framework (`adapters/reference_resolver.py`).** A tiny `ReferenceResolver`
  Protocol (`resolve(locator, *, max_chars) -> str`, NEVER raises, returns `""` on failure) plus a
  `{type: ReferenceResolver}` registry. Built-in resolvers ship for `note` (returns the locator
  text) and `file` (re-reads the live file fresh through the store's own path, reflecting
  staleness). The data-backed types (`collection`, `conversation`, `query`) are CONSUMER-INJECTED so
  the library stays generic; an un-wired type degrades to a graceful unresolved-pointer line
  (e.g. `[collection ref: <name>/<id> (unresolved)]`) instead of failing. `build_resolver_registry`
  merges built-ins with consumer resolvers (consumer wins on collision). Wired through a new
  `RunnerConfig.reference_resolvers` field and into the default `FileContextStore` in
  `resolve_context_assembler`.
- **Recency-bounded content resolution in `assemble()`.** Because a card's content can grow
  unbounded, each selected card ranks its content by recency (`ts`) plus relevance to the task
  (term overlap), resolves only the top-N within a char budget, and skips/trims the rest. New
  `FileContextStore` constructor knobs `max_card_refs` / `max_card_ref_chars` (module constants
  `_MAX_CARD_REFS=8`, `_MAX_CARD_REF_CHARS=4000`, `_MAX_CARD_CONTENT_ITEMS=200`). Never raises.
- **Card-update API (read-modify-write) on `FileContextStore`.** `add_content(card_id, item)`,
  `update_content(card_id, item_id, new_item)` (correction; appends if the id is unknown),
  `remove_content(card_id, item_id)`, and a batched `update_card(card_id, add=, replace=, remove=)`.
  Each is a safe atomic read-modify-write that normalizes the item, applies the recency trim, and
  persists. `record()`/`_record_inner` are generalized to also append non-file content via
  `outcome["content"]` (a list or single dict) without changing their file-pinning behavior. An
  async LLM updater can use this later; the API and tests land now.
- **No-file cards flow end to end.** `_card_term_weights` now tokenizes content `why`/note text (so
  a pure note/collection card is IDF-selectable), and `export_for_embedding` includes note text +
  content `why` (so such a card is vector-searchable). New built-ins exported from
  `adapters/__init__.py`: `ReferenceResolver`, `NoteResolver`, `build_resolver_registry`,
  `make_file_resolver`. New offline tests in `tests/test_card_content.py` (stub resolvers).
- **Async post-deep context-card updater (prepare for the FUTURE after a deep run).** After a deep
  task finishes (answer already delivered), an ASYNC, best-effort LLM process updates THIS user's
  context cards so the next similar request starts better-grounded. Two parts in
  `core/orchestrator.py`:
  (1) when the updater is active, each deep brief is appended with
  `DEEP_FUTURE_CONTEXT_INSTRUCTION`, asking the worker to END its output with a machine-parseable
  section after the line `=== FUTURE CONTEXT (for similar requests by this user) ===`;
  `_parse_future_context()` slices that section back out (last delimiter wins; absent -> "").
  (2) `_update_cards_after_deep_async()` spawns a background daemon thread (never blocks the answer,
  never affects the `OrchestratorResult`, never raises) that gathers the request + what executed +
  the parsed future-context + the user's CURRENT relevant cards (`assemble().card_metadata`), makes
  ONE cheap `balanced`-tier LLM call (forced `card_edits` tool, falling back to text parsed with the
  repo's `_extract_json`) returning a STRUCTURED edit plan, and applies it via the card-update API
  (`update_card(fields=, add=, replace=, remove=)`): name/description `fields`, content `add`
  (preferring resolvable `collection`/`file` references over copied snapshots, a `note` only when
  nothing external can be pointed at), `replace` corrections of stale items, and `remove`. Edits are
  user-scoped (card ids prefixed `u:<user_id>:` from `_ctx_meta['user_id']`, so cards never leak
  across users) and bounded (`async_card_update_max_cards`, `async_card_update_max_edits_per_card`).
  Card-update capability is detected GENERICALLY by `_card_update_store()` (any object exposing
  callable `update_card` + `add_content`, unwrapping composite/hybrid assemblers), not by a
  `FileContextStore` type check. New `OrchestratorConfig` toggle `async_card_update: bool = True`
  (env `QAR_ASYNC_CARD_UPDATE=0/1` in the CLI); when off, or when no card-update store or no provider
  is wired, the deep loop is byte-for-byte unchanged (no future-context block appended, no LLM call).
  Centralized prompts: `DEEP_FUTURE_CONTEXT_INSTRUCTION` and `CARD_UPDATE_PROMPT`/`CARD_UPDATE_TOOL`
  (no em dashes). New offline tests in `tests/test_async_card_update.py`.

### Changed
- **Selection/render algorithm extracted into pure module-level functions in `conversation_format`.**
  `select_current_slice(messages, query, *, recent_turns, max_chars)` and
  `select_related(conversations, query, *, max_convs, max_chars, get_conv_id)` are now standalone
  pure functions in `adapters/conversation_format.py`, operating on plain message/conversation dicts
  and returning `(rendered_text, metadata_list, truncated_flag)` tuples. The shared helpers
  (`_msg_role`, `_is_user`, `_render_turn`, `_relevance_doc`) and constants
  (`_USER_SCORE_BOOST=1.5`, `_USER_VERBATIM_CAP=2000`, `_AI_COMPACT_CHARS=400`) are also defined
  at module level there. `SessionFileConversationStore.current_slice` and `.related_slices` delegate
  to these functions after resolving keys and applying scope filtering; behavior is identical.
  Any other `ConversationStore` backend (Mongo, etc.) can now reuse the same algorithm without
  duplicating it.

- **Per-deep-goal context + a verifier that drives context-widening and tier escalation.** When the
  planner fans deep work into N goals, each goal now selects its OWN context instead of sharing one
  run-level view: `_run_deep` builds a per-goal block from the wired `context_assembler.assemble(goal)`
  plus a relevant `conversation_store.current_slice(conv_id, goal)`, and threads THAT into the goal's
  `context_preamble`. The goal verifier (`_verify_goal` / `VERIFY_GOAL_TOOL` / `VERIFY_GOAL_PROMPT`)
  now also returns `need_more_context` (bool) + `context_query` (string) + `next_tier` (one of the
  registry tiers, optional), parsed robustly (a string reply is recovered with the shared
  `_extract_json` helper; all three default to False/""/None on any parse miss). When a goal is NOT
  met and `need_more_context` is set, the loop pulls MORE context for the next iteration
  (`_widen_for_goal`: a fresh assembler read for the named missing context, WIDER conversation
  retrieval via `related_slices`, and a targeted `retrieval.grep`), the widening growing each round so
  a retry always sees more than the last, and runs the next attempt at the verifier's `next_tier`
  (resolved through `registry.resolve_tier`, kept worker-runnable) or one rung up the deep-model
  ladder. Bounded by the existing `deep_goal_max_iterations` cap + token budget. Visible STATUS ticks
  fire when per-goal context is selected, when more context is fetched, and when the tier changes. NO
  em dashes in the verifier prompt. Fully additive and guarded: with exactly one goal and no
  assembler/store wired, behavior is byte-for-byte unchanged. `run()` forwards a `ctx_meta` (carrying
  `conv_id`/`conv_scope`) into `_run_deep` so the per-goal helpers can reach the conversation store.
- **`ConversationStore.current_slice` selection: minimal "always in" + user/AI asymmetry.** The
  reference `SessionFileConversationStore.current_slice` no longer force-includes the last
  `recent_turns` messages raw. The ONLY guaranteed turn now is the **last USER turn** (the actual
  intent); everything else (recent AI turns, older turns) is a relevance CANDIDATE selected by
  TF-DF-IDF, not auto-included by recency. `recent_turns` is now the "considered window" (default
  lowered to 4): the last N turns merely join the candidate pool. USER turns are PREFERRED (a x1.5
  score boost) and render VERBATIM (capped only if absurdly long); AI turns earn inclusion purely by
  relevance, even the latest one, and are always COMPACTED. Per-turn relevance is LENGTH-NORMALIZED
  so length alone never wins, and an AI turn's TF-DF-IDF *document* is its compact form so a long AI
  answer cannot dominate df/idf. The last USER turn is guaranteed present even after `max_chars`
  truncation (the last message is the fallback anchor when there is no user turn). `related_slices`
  now applies the same AI compaction to its per-conversation tail. No method signatures changed (a
  separate Mongo implementation mirrors them); the `ConversationStore` Protocol docstring documents
  the new `recent_turns` meaning. No new LLM call is added: the no-LLM TF-DF-IDF + compaction is the
  pruning, and Step 1 stays one resolve round-trip.

### Added
- **`compact_message(text, *, max_chars=400)` helper** (`adapters/conversation_format.py`): compacts
  a long message to its beginning + end plus the few most salient MIDDLE sentences/lines, selected
  with TF-DF-IDF at sentence/newline granularity (each chunk is one document, reusing
  `extract_terms` / `select_representatives`), joined with an ellipsis marker within `max_chars`. A
  short message is returned unchanged. Never raises (falls back to head+tail truncation). Used to
  compact AI turns in the conversation store so a long AI answer cannot dominate downstream df/idf.
- **User Input Understanding (Step 1) + storage-agnostic `ConversationStore`.** The brain now
  treats resolving WHAT the user means as a first-class step that runs ONLY when needed. A new
  `ConversationStore` Protocol (`core/adapters.py`, with `ConversationContext` value object) offers
  `current_slice(conv_id, query, ...)` (always the last N turns PLUS TF-DF-IDF-selected relevant
  older turns of the SAME conversation) and `related_slices(query, scope, ...)` (TF-DF-IDF slices
  from OTHER conversations in scope); both NEVER raise. `Orchestrator.run()` gains keyword-only
  `conv_id` / `conv_scope` (full back-compat). When a store is wired AND a `conv_id` is present AND
  a cheap NO-LLM gate (`_needs_context_to_understand`) flags a short/anaphoric/acknowledgement input,
  the brain pulls the current slice and asks the model ONCE to rewrite the message into a
  self-contained GOAL CONDITION (`RESOLVE_REQUEST_PROMPT`). A `MORE_CONTEXT_NEEDED` reply pulls
  related conversations and retries ONCE; a `CLARIFY:` reply short-circuits the turn to a terminal
  confirm (executor → needs_you) without running the planner. On a successful resolve it emits a
  new streamed `EVENT_UNDERSTANDING` event (in `SURFACING_EVENTS`) and prepends the pulled
  conversation context + resolved request to `context_view`; context SELECTION (Step 2) then targets
  the resolved request while the planner still receives the user's LITERAL words. Self-contained
  inputs skip Step 1 entirely (no LLM hop, ZERO added latency); with NO store wired, behavior is
  byte-for-byte today's. New reference impl `adapters.SessionFileConversationStore` (local Claude
  session files) reuses the TF-DF-IDF sampler and a new shared `adapters/conversation_format.py`
  module factored out of `ClaudeConversationsAdapter` (its public behavior is unchanged). Wired via
  `RunnerConfig.conversation_store`; the executor builds a `conv_scope` from the task and passes
  `conv_id`/`conv_scope`, and only falls back to dumping the full transcript when no store is wired.
- **WebSearchAdapter**: new `RetrievalAdapter` that grounds the shallow orchestrator loop in live
  web data via the Tavily API. Uses stdlib `urllib.request` only (no extra deps). Enable with
  `WEB_SEARCH_ENABLED=true` and `WEB_SEARCH_API_KEY=tvly_...` env vars. `WEB_SEARCH_MAX_RESULTS`
  controls result count (default 5). When enabled, the CLI wires it alongside `FilesAdapter` via
  `CompositeRetrievalAdapter` so local corpus and web search are both queried. The adapter is
  graceful: every method catches all exceptions and returns `Observation(kind="error")` rather than
  raising. Exported from `quest_ai_runner.adapters` as `WebSearchAdapter`.
- **`derive_capabilities` web detection**: when a `WebSearchAdapter` with a configured API key is
  in the retrieval stack (directly or inside a `CompositeRetrievalAdapter`), `derive_capabilities`
  now correctly reports `web: true` even without a deep runner. This lets the Quest backend route
  web-research tasks to a runner that has shallow web search but no subprocess deep-runner.

### Changed
- **Deep execution now runs on our own goal-verification loop instead of Claude Code's `/goal`.**
  `/goal` re-checks its condition inside the worker every turn (token-heavy) and caps the condition
  at 4000 characters, rejecting a longer one and then running nothing. The worker is now invoked
  headless (`-p`) with the done-standard given as plain context; after it runs, the brain verifies
  the done-standard AT THE QUALITY BAR via one cheap LLM call (`_verify_goal`), judged through the
  AI rep persona and the GUIDANCE CARDS selected for the input (guidance = the quality standards).
  If unmet, the brief is augmented with why it fell short and what to do next, and it re-runs up to
  `deep_goal_max_iterations` (default 3). `DeepResult.met` now reflects the brain's verification,
  not just the worker exit code, so a silent no-op or a sub-standard result is a confirmed failure.
  Parallel subgoals carry the OVERALL goal in their prompt so each stays aligned with the larger
  goal (hierarchical goal: overall user goal -> subgoals).
- **The goal loop now applies to EVERY input, not just deep execution.** A plain ANSWER is also
  pursued as a goal: when a quality bar is wired (a GuidanceProvider), the written answer is verified
  against the user's goal at that bar and regenerated with steering until it meets the bar or
  `answer_goal_max_iterations` is reached. Every deep process prompt carries the top input-level goal
  (the user's actual request) plus, in a fan-out, the overall goal it serves, alongside its own
  done-standard; retries carry the prior output and the verifier's feedback. New: `run()` /
  `run_stream()` accept a `pending_inputs` callable so new user messages that arrive mid-run are
  folded into the next deep process / answer retry (inert when the consumer does not supply it).
- **The deep loop auto-selects and escalates the worker model, bounded by a token budget.** The
  deep worker is Claude Code (Claude models only); the loop manages a Claude model ladder
  (`QAR_DEEP_MODELS`, default `haiku,sonnet,opus`): start at the fast Anthropic tier, escalate to a
  stronger model each time the goal is not met. An explicit per-task model request (a `model_hint`
  that resolves to a Claude model) or a guidance card `model:` preference pins the model (no
  escalation). The fixed 3-attempt cap is replaced by an overall **token budget**
  (`QAR_GOAL_TOKEN_BUDGET`, default 300k): the worker runs with `--output-format json`, its reported
  tokens/cost are parsed into `DeepResult.tokens`/`cost_usd`, and the loop continues while under
  budget (`deep_goal_max_iterations`, raised to 8, is now a hard safety cap). A non-Claude model id
  is never passed to the Claude Code worker.
- **Mid-run messages are a generic abstraction.** `core/inbox.py` adds `InputInbox` +
  `InMemoryInbox`; the Orchestrator takes an `input_inbox` and `run()` auto-drains the current
  conversation (keyed by quest/conversation/session/user id) when no explicit `pending_inputs` is
  given. `build_orchestrator` wires a default inbox, so any interface (chat, Quest frontend, ...)
  only pushes new messages with `inbox.push(conversation_id, message)` and the brain folds them in.

### Fixed
- **Actionable requests now EXECUTE instead of ending as a proposal.** The cheap planner often
  routes a change request to `action="answer"`, proposes the change in prose, and forgets to set
  `answer_contains_work_to_execute`. The previous escalation nets only matched specific ANSWER
  phrasings (past-tense claims, "I need to update X"), which a model like gemini rarely produces
  verbatim, so a proposal such as "Aligning these to use the same created_at field will guarantee
  ..." matched nothing and the turn finished without doing the work. The orchestrator now also
  escalates by **user-message intent**: `_message_requests_change()` (imperative change verbs +
  bug/wrongness signals, excluding pure how/what/why questions) detects an actionable request from
  the stable message; when a deep runner is available and nothing executed, it runs a deep step
  whose brief carries the assistant's proposed approach so the run APPLIES it. Emits a visible
  status and logs the escalation. The earlier `_answer_describes_unexecuted_work` net (dropped in a
  refactor that left it defined but uncalled) is also re-wired as a fallback, now recognizing "the
  fix is to update X" / "this needs to be updated". Plain informational answers still do not escalate.
- **Interactive chat no longer crashes on a deep turn.** `InteractiveSession._run_turn` called
  `self._ensure_ai_label()` for a `kind="deep"` result, but that helper lives on `_TurnRenderer`,
  so every deep turn raised `AttributeError: 'InteractiveSession' object has no attribute
  '_ensure_ai_label'` (`OrchestratorResult` never sets `.text` for deep, so the broken branch
  always ran). It now calls `renderer._ensure_ai_label()` and shows the "planned, use /execute"
  hint only when no `DeepResult` actually executed, so it no longer misfires after a real run.
- **Quest decision summaries are capped at the 4000-char condition limit.** A verbose planner
  question/clarification could produce a decision summary that Quest stores as a goal CONDITION
  (max 4000 chars), so the POST was rejected with "Goal condition is limited to 4000 characters
  (got NNNNN)" even when the user's input was short. `QuestClient.create_decision` now truncates an
  over-long summary at the single boundary to Quest, so any caller's summary is bounded.

### Changed
- `FileContextStore.bootstrap()`: now uses an LLM (via the wired `ModelProvider`) to identify semantic topic cards across the codebase — a topic can span files from completely separate directories. The number of cards reflects the natural structure of the codebase, not a preset range. Without a provider, bootstrap is a no-op (cards accumulate via `record()` instead).
- **Bootstrap Stage 3 now dedups via keyword-clustering + an LLM merge decision** (replaces the old Jaccard file-overlap merge). Cards that share at least 2 keywords are clustered transitively (union-find); each multi-card cluster gets ONE LLM call asking which cards describe the same concept and should be merged. New cards are also deduped against the cards already on disk, folding a duplicate new card into the existing card's id rather than writing a divergent one. With no provider it falls back to a keyword-union merge (Jaccard >= 30%).
- **`FileContextStore.bootstrap()` is now INCREMENTAL.** It diffs the walked files against the existing cards: only files referenced by NO card (uncovered) drive a fresh 3-stage LLM fan-out, and only cards whose pinned files changed (stale) are regenerated. When everything is covered and unchanged the bootstrap is a no-op returning 0. (A second `bootstrap()` over an unchanged corpus therefore returns 0, not the card count.)
- `_walk.py`: add `site-packages` to `_BASE_SKIP_DIRS`
- Orchestrator: context assembly now runs in a background thread concurrent with the instant-ack; collected with a 3 s timeout so corpus search never blocks interactive responses
- Orchestrator: emits `"searching corpus…"` status when a context assembler is wired
- **`TurnMemory` replaced by `TurnContextStore` + `CompositeContextAssembler`.**
  Conversation turns are now stored as card files (the same format as `FileContextStore` file
  cards) under `.quest-context/turns/` and retrieved by the same IDF keyword-overlap stack.
  This means turn history benefits from semantic retrieval when a `VectorContextAssembler` is
  also wired. `TurnMemory` (keyword-only, in-memory) has been removed entirely.
  `InteractiveSession` now wires a `TurnContextStore` into the orchestrator's
  `context_assembler` automatically: if a `context_assembler` is already set the session wraps
  it with a `CompositeContextAssembler([existing, turn_store])`; otherwise it sets the
  `TurnContextStore` directly. A single-turn `_last_transcript()` buffer continues to provide
  immediate conversational continuity for the current pair. The orchestrator's `record()` call
  now includes `"response": res.text` in the outcome dict so `TurnContextStore.record()` can
  write complete turn cards. `/clear` resets the single-turn buffer (turn store cards are
  durable and are not cleared). `docs/context-assembly.md` updated to match.
- **`_run_deep` now receives `gathered` and includes the brain's specific reads in
  `context_preamble`.** When the orchestrator reads files before deciding to go deep, those
  reads (in `gathered`) are now forwarded into the deep runner's `context_preamble` alongside
  any `rep_preamble`, so the subprocess does not have to re-discover what the brain already
  found. The preamble is built as `rep_preamble` (if any) followed by a `--- RELEVANT CONTENT
  FOUND BY THE BRAIN ---` section rendered from `gathered`. The `wants_preamble` gate now fires
  whenever the runner accepts `context_preamble` (not only when `rep_preamble` is set), so
  gathered-only runs also benefit. The claim-remediation `_run_deep` call also threads `gathered`
  through.

### Added
- **`quest-ai-runner bootstrap` CLI subcommand.** Builds or refreshes the context card store for a corpus on demand: `--corpus PATH` (default `QAR_CORPUS_ROOT` or cwd) and `--cards-dir PATH` (default `QAR_CONTEXT_CARDS_DIR` or `<corpus>/.quest-context`). Uses the env-selected model provider, runs the incremental bootstrap, and prints the card count.
- **Bootstrap version metadata + auto re-index.** A successful `bootstrap()` writes a `bootstrap_meta.json` sidecar to the cards dir recording the bootstrap algorithm version, card count, and UTC completion time. On startup, `_bootstrap_if_needed` compares the stored version against the current `_BOOTSTRAP_VERSION`: when the stored cards were built by an older algorithm it re-indexes the corpus in the background (chat stays available immediately) instead of only refreshing stale cards.
- **`quest-ai-runner chat` — a polished interactive (attended) session.** A multi-turn REPL over
  the orchestrator brain that streams every `ProgressEvent` to the terminal in real time, keeps a
  rolling transcript so follow-ups share context, and attaches results to a Quest goal with
  `--goal-id` (or `/goal <id>` mid-session). The UX is built for daily use: an animated spinner runs
  on a background thread while the brain works; "chatter" (plan/read/replan/status/exec) collapses
  onto that ONE updating line so it stays subtle instead of scrolling; partial reply chunks type out
  in place under a bold `AI` label; the user's message is echoed under a `You` label; and a faint
  rule separates turns so history stays scannable. Key handling matches what people expect: ESC
  cancels the current turn while it streams (a raw-stdin watcher flips a `threading.Event` the stream
  loop checks), Ctrl+C clears the input line without exiting, and Ctrl+D exits. A short header shows
  the goal id (when set) and the shortcuts. The session lives in `quest_ai_runner.interactive`
  (`start_interactive` / `InteractiveSession`). The `[tui]` extra now also pulls in `rich` (alongside
  `prompt_toolkit`) for the rendering; both are OPTIONAL — the session degrades to plain
  `input()`/`print()` (no spinner animation, best-effort keys) when neither is installed, via the
  `_HAS_PROMPT_TOOLKIT` / `_HAS_RICH` pattern. Generic and consumer-free: it only talks to the brain
  through `run_stream`.
- **`quest-ai-runner send "<task text>"`** — enqueue a new AI task from the CLI and print its id.
  Routes to `QUEST_TEAM_ID` by default (override with `--team-id`), optionally attaches to a goal
  (`--goal-id`) and schedules a future run (`--at <ISO-8601 UTC>`); omit `--at` to run at the next
  poll.
- **Broken-promise honesty check, judged inside the goal verification.** The `Orchestrator` now
  durably captures per-turn EXECUTION FACTS (which mutating deep actions ran and whether each
  SUCCEEDED or FAILED, from `DeepResult.met` plus `EVENT_EXEC` phase ticks) onto
  `OrchestratorResult.execution_record` (new module `quest_ai_runner.core.guard`:
  `ExecutionRecord`, `ExecutionFact`, `classify_exec_phase`). On every ANSWER turn the goal
  loop's `_verify_goal` call also receives that record and its ONE verdict additionally judges
  honesty: the answering step can never change files/code/data itself, so a reply claiming a
  completed change the record does not show as SUCCEEDED comes back `met=false` with the
  `claims_unexecuted` flag. There is deliberately NO regex claim detector: the verification LLM
  reads the reply and the record together, so no phrasing can slip past a pattern list (an
  earlier iteration gated on regexes and missed real replies like "I have now directly updated
  and written the changes to <file>"). On an unbacked claim the loop AUTO-REMEDIATES: it executes
  the work for real via a deep run, but ONLY when nothing actually executed this turn (no success
  AND no failure recorded) — an action that already ran, succeeded or failed, is NEVER re-run,
  since host actions are not guaranteed idempotent (the double-mutation safeguard) — then folds
  the real output back into the reply and re-verifies. Otherwise the reply is regenerated to be
  honest (no false success) and the result is flagged `claim_corrected` / `partial`;
  `TaskExecutor._report` maps a `claim_corrected` background-task answer to `needs_you` instead
  of `done`. Tunable and ON by default (`OrchestratorConfig.verify_claims=True`,
  `max_remediations=1`; the check runs at least once per answer turn even with
  `answer_goal_max_iterations=1`) and NEVER raises (any failure leaves the turn unchanged).
  App-agnostic; shared by live chat and background tasks (one Orchestrator).

### Fixed
- **Image describe-fallback could be handed a foreign model id.** `Orchestrator` now accepts a
  `vision_model` and threads it into `prepare_attachments`, so a consumer wiring a SEPARATE
  vision describer (a vision-capable `vision_provider` distinct from a text-only answering
  provider) can name the model that describer should use. Previously the describe-fallback reused
  the ANSWERING model id, which is foreign to a distinct describer (e.g. a tier-alias or Gemini
  answer id passed to an Anthropic describer would fail), so images degraded to "could not be
  transcribed" notes instead of being read. Backward compatible: when `vision_model` is unset the
  prior behavior (reuse the answering model id) is unchanged, which is correct when the describer
  IS the answering provider.

### Added
- **Optional `GuidanceProvider` adapter — retrievable USE-CASE-SPECIFIC instructions.** A host app
  can now keep its ALWAYS-ON core prompt small by moving instructions that apply to only SOME inputs
  (product facts, feature-flow guides, behavior policies) out of the static prompt and into a
  retrievable corpus of opaque "guidance cards" (`GuidanceCard{id, title, relevance, body}`). The
  new `GuidanceProvider` role (Protocol + `GuidanceProviderBase` ABC, all methods "never raise")
  exposes `list()` (cheap catalog: id+title+relevance, no body), `read(id)` (one card with body, or
  None), and an optional `select(user_message, *, k, meta)` for semantic pre-selection. When wired
  via `RunnerConfig.guidance_provider` (or `Orchestrator(guidance=...)`), the orchestrator calls
  `select()` ONCE before planning and PREPENDS the chosen cards as an "APPLICABLE GUIDANCE" block to
  `context_view` (the same compose order the `ContextAssembler` uses), and the planner gains two
  discovery verbs, `list_guidance` / `read_guidance`, that flow through the SAME observation
  machinery as a read. A `read_guidance` for a card already pre-selected this turn returns a short
  de-dupe note instead of re-injecting the body. Cards are OPAQUE to the brain — it stays
  app-agnostic. Purely additive: a consumer that supplies no `GuidanceProvider` sees byte-for-byte
  today's behavior (no block, no new verbs in effect). `OrchestratorConfig.guidance_topk` (default
  3) tunes how many cards are pre-selected.
- **Three-arm context engine + compounding task memory.** Beyond the keyword `FileContextStore`,
  the runner now orients an agent semantically. The bootstrap extracts the code's OWN docstrings
  (no LLM) for rich summaries (measured: keyword routing 13/53 -> 53/93 on the eval set).
  `VectorStore` is a new pluggable adapter with a default local-filesystem Qdrant
  (`QdrantVectorStore`, `[qdrant]` extra, embedder pluggable: `bge-small` 53/80, `bge-base`
  60/86, SOTA API higher); `BM25ContentStore` (`[bm25]` extra) searches the actual file CONTENT
  (exact identifiers/phrases, parallel multi-query). `HybridContextAssembler` fuses keyword +
  vectors (union recall 93). A `VectorContextAssembler` records `task -> context` associations
  (the headline compounding memory): recency-weighted (yesterday outranks a year ago),
  merged/deduped by task slug, capped with oldest-first eviction. Cold-start seeds docstring
  cards into the vector store on first use, auto-updated (re-embed only changed). A confidence
  gate falls back to plain Claude Code when nothing is confident (never-worse). `RunnerConfig`
  gains `vector_store` (default becomes hybrid when set). UX: `OrchestratorConfig.instant_ack`
  (a concurrent one-second acknowledgment, no added latency) and `AssembledContext.sources`
  (transparency: which adapter surfaced what), emitted as a status. Multi-tenant `scope`
  threaded for the Quest org/team/quest path. Honest eval (routing + Claude Code A/B + LLM
  judge) on a repo copy, metrics in the README and `evaluation/`.
- **Opt-in reps run tasks AS their Quest persona by DEFAULT.** The rep-sync capability is still
  OFF unless a consumer supplies `RunnerConfig.rep_sync_resolver`, but the moment it is on, the
  default does the complete thing with NO extra glue: the poller resolves the rep, PULLS its Quest
  profile into the local skill file before the run, builds a per-run preamble from that file's
  MANAGED sections (persona + learned corrections, composed with the runner's context doctrine via
  `core.context_doctrine.compose_deep_preamble`), and injects it into the deep run so the task runs
  AS that rep. New `RunnerConfig.rep_sync_direction` (`"pull"` default | `"push"` | `"both"`) gates
  the sync: `pull` = Quest -> skill file before the run (Quest is the source of truth at execution
  time, no push-back); `push` = skill file -> Quest AFTER the run only (no pre-run pull, so no
  persona injection); `both` = pull then push. `validate()` reports an unknown value. Push-back and
  the pre-run pull are both best-effort: a sync failure is logged and NEVER fails the task.
  `TaskExecutor.execute(task, *, rep_preamble=None)` threads the preamble into
  `Orchestrator.run(rep_preamble=...)`, which forwards it to the deep run ONLY for a `DeepRunner`
  whose `run_goal` accepts a `context_preamble` kwarg (older runners are untouched, mirroring the
  `emit` opt-in). `SubprocessGoalRunner.run_goal` now accepts an optional per-call `context_preamble`
  that overrides its configured base preamble for that run. The brain stays generic: it only passes
  a string through; the persona content comes from the consumer's Quest profile. No
  `ContextAssembler` is required: a consumer that sets only `rep_sync_resolver` gets the rep's
  persona in the run. Fully additive: a consumer with no resolver, and any existing caller, sees
  IDENTICAL behaviour to before.
- **Context handling is ON BY DEFAULT.** `RunnerConfig.context_assembler` now defaults to an
  `_AUTO` sentinel: leaving it unset makes `build_orchestrator` wire a default `FileContextStore`
  (cards under `context_cards_dir`, or `<corpus_root|cwd>/.quest-context`) so the runner grounds on
  reusable context out of the box. Pass an instance to customize, or `None` to disable. A new
  `RunnerConfig.context_cards_dir` sets the default store's location. `Orchestrator.run()` gains a
  `context_meta` parameter, and threads it plus `quest_id` to `assemble()`/`record()` so a
  multi-tenant assembler (e.g. a Quest-backed one serving many users) can scope per user/team/quest.
- **Fifth adapter role: `ContextAssembler` (PRE-FLIGHT CONTEXT)** -- an optional adapter that is
  called ONCE, guaranteed, before the plan->gather loop starts. `assemble(task_text)` returns an
  `AssembledContext` (context_view string + optional model_tier_hint + card_ids + stale list); the
  orchestrator injects `context_view` into the planner prompt and, when no explicit `model_hint`
  was passed by the caller, applies `model_tier_hint` as the run's model override. `record(task_text,
  outcome)` is called after the run as a best-effort write-back (never raises). The adapter is
  wired via `RunnerConfig.context_assembler` and forwarded to `Orchestrator(context_assembler=...)`;
  omitting it gives exactly the prior reactive-gather behaviour. The Protocol (`ContextAssembler`,
  structural) and the ABC (`ContextAssemblerBase`, nominal) live in `core/adapters.py`; the value
  object (`AssembledContext`) is a plain dataclass alongside them. Reference implementation:
  `adapters.FileContextStore` -- a stdlib-only ContextAssembler backed by per-card JSON files
  (one file per task slug); selects cards by keyword overlap with the task, checks pinned file
  freshness via sha256 + mtime (and optionally git blob SHA), renders a context_view string, and
  writes back an upserted card on each run. Atomic writes (tmp + os.replace). No third-party deps.
- **Centralized prompt doctrine (`core/context_doctrine.py`)** -- `SUFFICIENCY_GATE` and
  `MODEL_TIER_GATE` are module-level constants woven into `PLANNER_PROMPT` at module load, so the
  sufficiency checklist (read-before-acting, cheap-pass discipline, context-dry signal) and
  model-tier escalation rule (haiku/sonnet/opus; escalate one tier on a failed verification) are
  always applied without duplicating text. Both constants contain NO literal `{`/`}` characters,
  so they concatenate safely into the `.format()`-able prompt string. `DEEP_CONTEXT_DOCTRINE`
  combines both gates into a block suitable for prepending to a deep-runner's `context_preamble`;
  `compose_deep_preamble(base, assembled)` builds the full preamble in one call (doctrine + base +
  optional assembled context_view), so spawned deep agents obey the same discipline as the
  orchestrator. The `PLANNER_PROMPT` constant is now assembled at module load from named parts
  (`_PLANNER_HEAD`, `_PLANNER_ACTIONS`, `_PLANNER_TAIL`) plus the two gate strings, keeping the
  `.format()` call in `_plan()` unchanged.

- **Multimodal (image) + file-attachment support (the runner owns multimodal)** — the text
  provider does not do multimodal, so the runner does. A new standard handler
  `core/attachments.py` (`prepare_attachments(attachments, *, model, provider, vision_provider,
  vision_model, max_attachment_bytes)`) takes in-memory attachment items
  (`{filename, mime_type, data: bytes, kind: "image"|"file"}` — the SAME shape for chat uploads
  and panel context-docs) and, per the answering model/provider, EITHER passes an image NATIVELY
  (base64 content block) when the model is vision-capable and the provider can send blocks, OR
  DESCRIBES it with a separate vision-capable provider (centralized `DESCRIBE_PROMPT`), OR
  extracts a non-image file's text best-effort by type (txt/md/csv/json/code direct; pdf/docx via
  an optional light extractor if installed; any other type accepted with a clear binary note —
  never raises). Returns `native_blocks` (for the answer) and `text_context` (for the planner +
  grounding). A new vision-capability seam `model_registry.is_vision_capable(model)` (keyed by
  model FAMILY via `VISION_FAMILY_PATTERNS`: Claude 3.x/4.x, Gemini 1.5/2.x/3.x, gpt-4o/4.1/
  o-series → vision; default False) is the ONE place capability is decided. `AnthropicProvider.
  answer()` now passes a content-block LIST through to the SDK unflattened (plain-string path
  unchanged); `ClaudeCliProvider` degrades a block list to text and never crashes (and declares
  `supports_native_images = False`). `Orchestrator.run()`/`run_stream()` take a new optional
  `attachments` list, fold the prepared text into the planner CONTEXT, and ride native image
  blocks on the final answer message; the Orchestrator takes an optional `vision_provider` for the
  describe-fallback (wired from `RunnerConfig.vision_provider`). Per-attachment 50 MB cap
  (`config.MAX_ATTACHMENT_BYTES` / `attachments.DEFAULT_MAX_ATTACHMENT_BYTES`); attachments are
  processed CONCURRENTLY. Offline tests cover the capability map, the SDK image passthrough, the
  CLI degrade, native vs describe vs extract vs oversize vs unknown-binary, and the orchestrator
  threading.
- **Deep-run escalation marker (`QAR-ESCALATED: <decision_id>`)** — a spawned deep worker that
  raises a human-only decision mid-run (via whatever escalation mechanism its consumer preamble
  provides) can now report it back to the runner by printing `QAR-ESCALATED: <decision_id>` on its
  own line. `SubprocessGoalRunner` parses the marker (new `ESCALATION_MARKER` /
  `extract_escalation_id` in `core/goal_runner.py`, exported from `core`) and returns
  `DeepResult(met=False, decision_id=...)` regardless of exit code, so the executor reports the
  task as `needs_you` with the decision linked — the ask surfaces in the consumer's UI attached to
  the paused task instead of the task closing as done/failed. `GoalRunner.run` also normalizes
  `met=True` + `decision_id` to not-met so a custom runner can never report a paused run as done.
  Workers that never print the marker behave exactly as before. Offline tests cover marker parsing
  (last-marker-wins, bare marker ignored), the subprocess runner, the normalization, and the
  executor's `needs_you` report + chat post.
- **Configurable planner tier + answer timeout (env)** — the CLI now reads two optional env vars so
  a consumer can tune the brain without code: `QAR_PLANNER_TIER` sets the model tier for the planner
  step that picks read/answer/deep (default stays the cheap `haiku`; raise to e.g. `sonnet` when
  tasks are mostly real work so answer-vs-deep routing is decided by a more capable model), and
  `QAR_ANSWER_TIMEOUT` raises the per-call wall-clock cap for the `claude_cli` planner/answer backend
  above its conservative 180s default (headless completions over a large corpus can exceed it).
  Both are plumbed through existing config (`RunnerConfig.orchestrator.planner_tier`,
  `ClaudeCliProvider.timeout_seconds`); absent = prior behavior.
- **Per-run model hint** — callers can now pass an opaque `model_hint` string to
  `Orchestrator.run()` and `Orchestrator.run_stream()` to override the tier used for answer and
  deep steps in that run. The hint is consumer-defined: a tier name, or any string the consumer's
  `ModelRegistry` understands. The registry resolves it exactly as it resolves a planner-chosen
  tier, so unknown values degrade gracefully to the "sonnet" default rather than raising (note: the
  default registry resolves only the tier names, so a raw model id needs a registry that
  understands it). The hint applies to answer and deep steps only; the planner's own cheap calls
  stay on the configured planner tier. Absent/`None` means exactly the prior behavior. `TaskExecutor.execute()`
  automatically surfaces a `"model"` field on the task document as the `model_hint`, so a stored
  per-task model override reaches the orchestrator with no extra code in the consumer's poller.
  Precedence inside one run: `model_hint` > `plan.model_tier` (planner's own choice) >
  `default_tier` (compile-time default per step kind). Seven new offline unit tests cover: hint
  reaches `provider.answer`, absent hint leaves planner tier unchanged, hint on a deep step,
  unknown hint degrades gracefully, executor task-model field is forwarded, explicit `None` is
  treated as absent, and the full poller path.
- **Discovery contract on retrieval adapters** — `RetrievalAdapter` gains four optional discovery
  methods (`list_sources`, `describe_source(name, path=None)`, `list_operations`,
  `describe_operation(name)`) that make a source self-describing, so the planner asks what exists
  instead of needing a static schema blob in its prompt. The pattern is cheap-then-drill-down: a
  LIST call returns names plus one-liners, a DESCRIBE call returns detail for one item. All four
  return `Observation(kind="query")` and never raise; `RetrievalAdapterBase` ships benign
  "nothing to discover" defaults so existing subclasses keep working unchanged. The orchestrator
  dispatches the four read specs via `getattr`, so a structural adapter that predates the methods
  degrades to a "discovery not supported" observation (back-compat), and discovery specs fan out
  in parallel with the rest of the `reads[]` step. Reference implementations: `FilesAdapter`
  enumerates readable files (capped at 500) and returns a markdown heading outline per file;
  `CachedDbAdapter` accepts optional `sources` / `operations` / `describe` metadata at
  construction and falls back to inferring a schema from a sample row. 15 new offline tests in
  `tests/test_discovery.py`.
- **Stop re-sending unchanged transcript + context to the planner on re-plan steps** — within a
  single run the recent `transcript` and the static `context_view` never change between steps, yet
  the loop re-sent BOTH in full to the cheap PLANNER on every re-plan step (the prior wave only
  leaned out `gathered`). New additive `OrchestratorConfig.planner_abbreviate_repeat_context`
  (default `False` → byte-for-byte current behavior): when enabled, step 1 still sees the FULL
  transcript + context_view, and later re-plan steps swap them for a short "(unchanged since step
  1 — already provided)" reference note, so the planner focuses on the NEW `gathered` observations
  it's there to react to. The final ANSWER path is untouched — it always grounds on the full
  transcript + context_view, so answer grounding is never weakened. `_plan` gained a keyword-only
  `step` argument; empty transcript/context are left as-is (no note injected).
- **Resource-aware throttling (graceful pause under system overload)** — a long-running lane can
  now notice host overload and stop taking on NEW work instead of thrashing, resuming automatically
  once resources recover. New stdlib-only `quest_ai_runner/resources.py`: `ResourceLimits` (all
  limits OFF by default; set via `RunnerConfig.resource_limits` or env — `QAR_MAX_MEMORY_PERCENT`,
  `QAR_MIN_FREE_MEMORY_MB` for the remaining-resource form, `QAR_MAX_LOAD_PER_CORE`, plus
  `QAR_RESOURCE_RESUME_MARGIN` hysteresis (default 10%) and `QAR_RESOURCE_CHECK_INTERVAL` re-check
  cadence while paused (default 30s)) and `ResourceGuard` (samples `/proc/meminfo` /
  `os.getloadavg`, optional `psutil` fallback; logs a WARNING naming the tripped limits on entering
  overload and INFO on recovery; an unreadable metric disables its limit, warned once). The pause is
  LOSSLESS by design: the poller gates pickup before discovery (heartbeat still fires so the env
  stays live), re-checks per task mid-batch (deferring before mark/claim so the task re-fires), and
  `run_forever` waits at the guard's shorter re-check cadence while paused so the lane resumes
  promptly. Unclaimed tasks stay queued on the backend; in-flight tasks are never killed. Nothing
  configured → the guard is a no-op (backward compatible).
- **Leaner per-step planner view (older `gathered` compressed)** — the bounded plan→gather→re-plan
  loop re-fed the cheap PLANNER the ENTIRE cumulative `gathered` blob verbatim on every step, which
  grows fast on multi-read runs. The planner now sees a LEANER view: the newest
  `OrchestratorConfig.planner_recent_full` observations in full, and everything older collapsed to a
  one-line summary (source/path + key finding) so the planner still knows the ground it covered and
  won't re-issue the same read. The full `gathered` is unchanged and is still what the final ANSWER
  is synthesized from — only the planner's per-step input is trimmed. Two new config knobs (both
  additive, defaulted): `planner_recent_full` (default 4) and `planner_compress_over` (default 6);
  compression only engages once `gathered` exceeds `planner_compress_over` observations, so short
  runs are byte-for-byte identical to before. New helpers `_render_gathered_for_planner` and
  `_summarize_observation` in `core/orchestrator.py`; `_render_gathered` (full render) is untouched.
- **Task handler stamp + live execution-progress stream** — the runner now records WHO ran a task
  and streams its execution lifecycle to the task, so a Quest task-detail view can show "handled by
  X" and a live progress feed. `QuestClient.claim(task_id, handler=None)` stamps the handler on the
  in-progress PATCH (omitted when None → backward compatible), and the poller resolves the handler
  as the rep slug (basename of the `rep_sync_resolver`'s `skill_dir`, e.g. `"alex"`/`"sam"`),
  falling back to `RunnerConfig.runner_label` or None. New `QuestClient.report_progress(task_id,
  kind, text=, output=, data=)` POSTs `{kind, ...non-None}` to `/api/assistant-tasks/{id}/progress`
  (kind in started|status|exec|output|done|error) and is best-effort: it never raises, logging a
  warning on failure. `TaskExecutor` emits `started` on pickup, fans each real deep milestone to
  BOTH the originating chat AND the progress stream (kind `exec`), and emits a terminal event from
  the result (done→`done`, needs_you→`done` with a paused note, failed→`error`). All progress posts
  are best-effort and degrade cleanly against an older client without `report_progress`.
- **AI-rep ↔ skill-file sync (`runner.rep_sync`)** — keep a team AI rep's Claude skill file in sync
  with its Quest profile in ONE call: `sync_rep(client, team_id, user_id, skill_dir, direction=...)`
  (and the underlying `pull_rep_to_skill` / `push_skill_to_rep`). Pull renders the rep's `persona`
  and `learned_notes` into the skill file's runner-MANAGED sections (delimited by
  `<!-- QAR:MANAGED:... -->` markers, so any human-authored content is preserved and re-render is
  idempotent); push reads those sections back and PUTs them to the profile. `QuestClient` gained
  `get_ai_profile` / `update_ai_profile` / `add_rep_correction` over
  `GET|PUT /api/teams/{team_id}/members/{user_id}/ai-profile` and
  `POST .../corrections` (reuses the existing urllib client + bearer auth; no new HTTP). The poller
  has an OPT-IN `rep_sync_resolver` on `RunnerConfig`: when set, it pulls the rep's latest profile
  into its skill file right before running that rep's task (best-effort — a sync failure never breaks
  execution). Off by default, so existing lanes are unchanged.
- **Discovery tools on `RetrievalAdapter`** — `list_sources()`, `describe_source(name, path=...)`,
  `list_operations()`, and `describe_operation(name)` let the brain learn what a source of truth
  CONTAINS (collections/doc-sets, their fields) and what OPERATIONS it can call, instead of needing
  a static schema/operation blob pushed into the planner prompt. Each returns
  `Observation(kind="query")`, so results flow into `gathered` through the same path as a read and
  batch in parallel. Two levels each: a cheap LIST (names + one-liners) and a DESCRIBE drill-down.
  Exposed to the planner as four new `reads[]` spec-shapes (`{"list_sources": true}`,
  `{"describe_source": "<name>"}`, `{"list_operations": true}`, `{"describe_operation": "<name>"}`);
  the planner prompt teaches the brain to DISCOVER before guessing, neutrally (no source/operation
  is favored). `RetrievalAdapterBase` ships non-abstract defaults and the orchestrator dispatches
  via `getattr`, so older structural adapters keep working (a discovery spec degrades to a benign
  "not supported" Observation). `FilesAdapter` enumerates readable files + heading outlines;
  `CachedDbAdapter` takes optional `sources`/`operations`/`describe` and otherwise infers fields
  from a sample row.
- **`EVENT_EXEC` + `DeepRunner.run_goal(emit=...)`** — a deep runner can now stream its
  EXECUTION LIFECYCLE (generated code, each execution attempt, its raw output, retries, done/error)
  by emitting `ProgressEvent(type=EVENT_EXEC, data={"phase": ...})`. The orchestrator passes a live
  `emit` callable to runners whose `run_goal` accepts it (decided by signature inspection, so older
  `run_goal(*, goal, brief, model, max_turns)` signatures keep working and are never double-called).
  `EVENT_EXEC` is intermediate texture — NOT in `SURFACING_EVENTS` — so a LIVE run shows it while a
  BACKGROUND (`MilestoneSink`) run drops it, like the other chatter types. This lets an attended
  chat surface live code-execution + retries while a queued task stays quiet.

- **`ClaudeCliProvider`** — a keyless `ModelProvider` that drives the local `claude` CLI headless,
  so the orchestrator's planner/answer calls run on the box's Claude Code **subscription login**
  with no `ANTHROPIC_API_KEY` (the deep-runner already ran keyless this way; now the whole runner
  can). The CLI can't force `tool_choice`, so `plan` instructs the model to emit the `decide`
  tool's JSON and parses it leniently (degrading to a safe `answer` on any failure). `list_models`
  returns `[]` so the `ModelRegistry` uses its fallback tier map; tier ids map to CLI family
  aliases. The spawned process has API-key / session env vars stripped (subscription only).

- **Multi-environment heartbeat** — `RunnerConfig.env_id` (env var `QAR_ENV_ID`) is sent on the
  environment heartbeat so a team can attach SEVERAL runners, each registering as its own
  environment. Omitted = the team's default environment (single-runner deployments are unchanged).

### Changed
- **Deep-step status copy** — the live status emitted when the orchestrator enters the `deep`
  action is now a neutral `"working on this now…"` instead of `"this needs real work — running a
  goal-driven task"`. The old wording read as jargon (and conflated "task" with a consumer's own
  goal objects) when surfaced verbatim in a chat; the neutral phrasing travels better across
  consumers and drops the em dash.
- **CLI** — `QAR_MODEL_BACKEND` (`anthropic` | `claude_cli`) selects the model backend; when unset
  it auto-selects `claude_cli` (keyless) unless `ANTHROPIC_API_KEY` is present. The runner now
  works out of the box on a subscription login with no API key. The CLI also reads
  `QAR_RUNNER_LABEL` and `QAR_ENV_ID` for the heartbeat.

## [0.1.0] - 2026-06-05

Initial public release.

### Added
- **`core`** — the domain-free orchestrator brain: a bounded plan → gather → re-plan →
  answer/deep/confirm loop, with the frozen streaming interface (`Mode`, `ProgressEvent`,
  `StreamSink`, `MilestoneSink`) and the live↔background handoff.
- **`runner`** — the queued-task executor: an event-driven, signature-deduped poller; a claim
  step; an executor that maps results onto the Quest task callback; and a `QuestClient` covering
  discover / claim / report / escalate / loop-close.
- **The four adapter interfaces** — `RetrievalAdapter`, `ModelProvider`, `DeepRunner`,
  `EscalationSink` — plus reference implementations (`FilesAdapter`, `CachedDbAdapter`,
  `AnthropicProvider`, `SubprocessGoalRunner`, `QuestDecisionSink`).
- **`RunnerConfig`** + factory helpers and capability derivation (`web` / `corpus` / `code`).
- **`quest-ai-runner` CLI** — an env-driven console entry point for the poller.
- **Examples** (`examples/`) — a custom consumer, a runnable lane, and a live end-to-end demo.
- **Tests** — offline core-loop, model-registry, mode/streaming, runner (mock client), and
  example wiring tests.

[Unreleased]: https://github.com/SpiritualData/quest-ai-runner/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SpiritualData/quest-ai-runner/releases/tag/v0.1.0
