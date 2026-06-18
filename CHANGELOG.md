# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  gathered-only runs also benefit. `_guard_turn` also threads `gathered` through to its
  remediation `_run_deep` call.

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
- **Broken-promise guard — post-turn honesty check (auto-remediate then verify).** The
  `Orchestrator` now durably captures per-turn EXECUTION FACTS (which mutating deep actions ran and
  whether each SUCCEEDED or FAILED, from `DeepResult.met` plus `EVENT_EXEC` phase ticks) onto
  `OrchestratorResult.execution_record`. At turn finalization it guards ANSWER replies: a cheap
  STRUCTURAL gate (`text_claims_action`) engages only when the reply asserts a completed or imminent
  action (so plain informational turns pay ZERO model cost), then a focused verification call
  (`verify_supported`) decides whether the execution record backs the claim. On a mismatch it
  AUTO-REMEDIATES with ONE safe re-run, but ONLY when nothing actually executed this turn (no
  success AND no failure recorded) — an action that already ran, succeeded or failed, is NEVER
  re-run, since host actions are not guaranteed idempotent (the double-mutation safeguard). If still
  unmet, the reply is rewritten to be honest (`honest_rewrite`, no false success, no em dashes) and
  the result is flagged `claim_corrected` / `partial`. `TaskExecutor._report` maps a
  `claim_corrected` background-task answer to `needs_you` instead of `done`. Tunable and ON by
  default (`OrchestratorConfig.verify_claims=True`, `max_remediations=1`); the guard NEVER raises
  (any failure leaves the turn unchanged). New module `quest_ai_runner.core.guard`
  (`ExecutionRecord`, `ExecutionFact`, `text_claims_action`, `classify_exec_phase`,
  `verify_supported`, `honest_rewrite`, and the centralized `VERIFY_CLAIM_PROMPT` /
  `HONEST_REWRITE_PROMPT`). App-agnostic; shared by live chat and background tasks (one Orchestrator).

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
  as the rep slug (basename of the `rep_sync_resolver`'s `skill_dir`, e.g. `"joshua"`/`"subham"`),
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
