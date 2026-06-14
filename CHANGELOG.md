# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
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
