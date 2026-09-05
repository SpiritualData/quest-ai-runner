# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **A character worked a quest on a day they were not rostered for** (`runner/autopilot.py`). A
  quest had one character rostered `["Mon".."Fri"]` and another `["Sat"]`; on a Saturday the
  weekday character produced work and emailed the owner, because `resolve_persona` honored a
  goal's own `assignee_rep_id` unconditionally and the roster's days were therefore advisory. They
  are authoritative now: a character works a quest only on the days their own roster entry names,
  and nothing overrides that. A goal assigned to a ROSTERED character who is not on duty today is
  HELD (excluded from today's targets, worked on a day that character is rostered for, logged by
  count and id) rather than re-routed, since handing one character's assigned goal to whoever
  happens to be around is its own wrong answer; `resolve_persona` says so with a new `PERSONA_HELD`
  answer, because `None` already means "the plain assistant". A goal assigned to a character with
  NO roster entry is unchanged (no entry, no day setting to follow), and an unassigned goal keeps
  the day-matched-then-unrestricted roster order but can no longer fall past it to the
  consumer-injected fallback resolver or the plain assistant while the roster names any
  goal-working character. A day NOBODY is rostered for is now a cheap per-quest gate beside the
  cadence check (config plus clock, before any goal fetch or model call): the quest produces
  nothing and records a skip naming the day. That case previously ran the quest as the PLAIN
  ASSISTANT whenever it carried standing instructions, since the instructions branch fell from
  `personas_on_duty[0]` to the fallback resolver to `None`. New public helpers `PERSONA_HELD` and
  `split_held_for_another_day`; `personas_on_duty` / `persona_entries_on_duty` are unchanged (they
  were already the day-precedence source of truth, and a consumer keeps a parallel copy for
  attended chat), and `instructions_only` still keeps an entry out of goal routing while it works
  its own instructions on its own days. A quest with an EMPTY roster sees zero behavior change,
  which `tests/test_autopilot_day_authority.py` pins along with the rest of the rule. This
  supersedes the earlier design note that a persona with a goal assigned to it is activated
  whenever that goal comes due, independent of the quest's day schedule.
- **Context-assembly timeout default was too tight for a real deployment under load**
  (`core/orchestrator.py`, `context_assembly_timeout_seconds`). Measured on a live deployment: a
  warm turn-start assembly (card store + vector search + one downstream profile fetch) typically
  completes in well under 1s, but a recurring weekly burst of concurrent deep-run subprocess
  spawns (a reliability-probe batch, one worker started roughly every minute for several minutes)
  reliably pushed assembly past the old 5.0s budget -- the soft deadline was hit a dozen times
  across two such bursts, and at least once the hard budget was blown entirely, dropping ALL
  turn-start context (0 cards / 0 sources) for that turn. Default for
  `QAR_CONTEXT_ASSEMBLY_TIMEOUT_SECONDS` raised 5.0 -> 15.0: real headroom over the observed tail,
  still tiny next to the per-turn answer budget (`QAR_ANSWER_TIMEOUT`, minutes) and the deep-run
  wall-clock timeout (`QAR_DEEP_TIMEOUT_SECONDS`, an hour), and the common case is unaffected since
  it already returns in under a second. Still fully overridable via the env var.

### Added
- **"Run now" for a quest's autopilot** (`runner/autopilot.py`, `runner/poller.py`). A quest's
  `autopilot.run_requested_at` (stamped by the consumer's own endpoint) asks for the next pass
  immediately. The new `run_requested()` predicate treats a request as PENDING while it is newer
  than `last_pass_at`, and a pending request both satisfies the cadence gate and pulls the quest's
  existing pass occurrence to today (and back to the current local time when `run_time` is still
  ahead). Deliberately not a second execution path: it moves the SAME recurring occurrence, so the
  pass, its budget and its mail are unchanged. Self-consuming, so nothing has to clear it -- the
  pass stamps `last_pass_at`, which makes the request older and therefore spent. `mode` remains
  the outer gate: a request never runs a quest whose autopilot is off.

- **Standing instructions per PERSONA, and a persona who takes no goals** (`runner/autopilot.py`).
  A quest's roster entry (`autopilot.personas[]`) can now carry its own `instructions` and an
  `instructions_only` flag, because one roster can hold characters doing genuinely different jobs
  on the same quest: the character advancing the goals most days, and a specialist rostered for one
  day to review the quest from outside. Handing both the identical quest-wide brief describes
  neither. A persona's instructions are emitted as their own block, immediately after the
  quest-wide one and before the first `Goal:` block, verbatim, and their precedence over it is
  stated in the text rather than implied by position. `instructions_only` takes a persona out of
  GOAL routing (`resolve_persona` skips those entries in both roster loops) while leaving them on
  duty for their own work: without it, day-matched-beats-unrestricted means the specialist absorbs
  every goal on the day they are rostered and the weekday worker goes quiet. A goal's own
  `assignee_rep_id` still wins outright, and an assigned persona brings their instructions with
  them on any day. The always-work rule is now per persona, so one pass can create the day's goal
  batch AND the specialist's standing-review batch; each batch is one budget unit as before. New
  public helpers `persona_entries_on_duty` and `persona_instructions_for`; `personas_on_duty` is
  now a thin wrapper over the first and still lists `instructions_only` reps (they ARE on duty,
  which is what an attended chat on the quest asks). Fully backward compatible: a roster carrying
  neither field composes byte-identically to before.

### Fixed
- **A quest on another team synced its notes but never its goals** (`runner/quest_client.py`).
  `list_quest_goals` hits a team-scoped endpoint and always used the client's OWN `team_id`, but
  the quest need not be on it: an owner-scoped lane legitimately syncs quests across every team its
  owner belongs to, and a quest moved to another team 404s on the configured one from then on --
  every scan, forever, logged only as a skipped goal sync. When the caller did not name a team and
  the configured one does not own the quest, the client now resolves the quest's own team once
  (cached per quest, consulted only after a team-scoped call has already failed, so the ordinary
  path costs nothing) and retries there. An explicit `team_id` is honored as given and never
  second-guessed.
- **A quest moved to a SECOND team stopped syncing its goals until the runner restarted**
  (`runner/quest_client.py`). The fix above resolved a quest's own team once and cached it for the
  life of the process with nothing to invalidate it, so it survived exactly one move: after the
  next one both the configured team and the cached team 404'd, and `list_quest_goals` returned `{}`
  on every scan until a restart. The cached team failing is now read as the staleness signal it
  is: the entry is dropped and the team is resolved once more from the live quest list, bounded to
  that one re-resolution per call so a lagging quest list costs one skipped scan, not a retry
  storm. A miss (quest absent from the owner's list, or the listing itself failing) is no longer
  cached either; a cached "" was the same restart-only bug in a different coat. Pinned by three
  tests in `tests/test_quest_goal_sync.py`.

- **The fast lane stranded every owner-scoped real-time task** (`runner/poller.py`). The
  background scan resolved its discovery scope through `discovery_team_id` (falling back to
  `team_id`), but `_fast_lane_loop` scoped its long-poll and its short-poll fallback by
  `cfg.team_id` alone. A lane configured for owner-scoped discovery (`discovery_team_id=""`) is
  exactly the lane whose tasks carry `team_id=None` -- Quest's UI creates a personal chat task
  that way -- so the team-filtered wait matched nothing and every `real_time` task fell through
  to the full `poll_interval_seconds` background scan. From inside a live chat, where someone is
  waiting on the reply, that is indistinguishable from the lane being down (measured against a
  live backend: a team-scoped wait returned nothing after its full timeout while an owner-scoped
  one returned the queued task in 0.3s). Both fast-lane paths now share the background scan's
  scope via the new `Poller.discovery_team_id()`, and the attach gate accepts a teamless lane
  that has opted into owner-scoped discovery. A team-bound lane's per-team isolation is unchanged
  and pinned by a test.

- **A daily brief was skipped after any evening pass** (`runner/autopilot.py`). `cadence_due`
  compared UTC calendar days whenever a quest set no `run_timezone`, so a pass that ran late in
  the evening on a runner west of UTC stamped `last_pass_at` with the NEXT UTC date and the
  following day's pass was gated out as "already ran today". Real case: a 20:26 US/Pacific
  catch-up pass stamped 03:26Z and the next morning's brief never arrived. The zone-less path now
  degrades to the runner's own local clock via `local_time.now_in_zone`, the same single
  degradation rule the rest of the repo follows, and the UTC branch is gone rather than kept as an
  option. The suite pins `TZ` (new `conftest` fixture) so calendar-boundary assertions no longer
  silently inherit the host's zone.

- **Per-quest autopilot run time and standing instructions** (`runner/local_time.py` new,
  `runner/poller.py`, `runner/autopilot.py`, `config.py`). A quest can now set its own
  `run_time`/`run_timezone` (instead of only the team-wide `autopilot_pass_time`) and standing
  `instructions` that are folded into every batch the pass builds for it. Hybrid pass schedule:
  the existing team-wide pass keeps serving quests with no `run_time`; a quest that sets one gets
  its own recurring pass series, created/retuned/retired by the new
  `Poller._ensure_quest_pass_tasks` (kill switch: `RunnerConfig.autopilot_quest_pass_tasks`). The
  poller corrects the backend's UTC-dated spawn for a run time that crosses UTC midnight, and
  `cadence_due`/`_due_now_locally` gained an optional `tz` so the schedule and its gate read the
  same predicate. A quest whose instructions describe a deliverable now produces exactly one work
  batch per due pass even with no eligible goal (the "always-work" rule), so a quest's standing
  instructions can fully replace a hand-authored recurring task. New
  `RunnerConfig.autopilot_settings_refresh_seconds` controls how often the poller re-reads quest
  autopilot settings for this (TTL-cached snapshot, `Poller._quest_schedule_snapshot`). Fully
  backward compatible: a quest with no `run_time` and no `instructions` behaves byte-identically
  to before. New dependency: `tzdata` (a pure-data wheel backing `zoneinfo` where the host has no
  system tz database).

- **A task can name a specific goal inside a quest via `related_goal_id`** (`runner/executor.py`,
  `_build_context_view`). The task document already carries `goal_id`, but that field actually
  holds the QUEST's id on a quest-scoped task (a historical misnomer this repo does not rename),
  so there was no way for a task to say "this run is about goal X within this quest" and have the
  goal-context fetch honor it. When `related_goal_id` is present it is fetched as the real goal,
  resolved against `goal_id` as its quest id; when absent, behavior is byte-for-byte identical to
  before. Purely additive: reads the field defensively (`task.get("related_goal_id")`) so it
  activates automatically once a backend starts sending it. See `docs/quest-api-contract.md`.


- **`GOALS.md`: a quest's goal ladder in the folder, syncing both ways** (`runner/quest_goal_sync.py`,
  on by default via `RunnerConfig.quest_goal_sync`; direction follows `quest_folder_sync_direction`).
  `QUEST_SYNC.md`'s state block carries one OUTCOME and `next_steps` carries the two or three
  things to do now. Neither is the plan, so a folder that wanted its goals locally grew a
  hand-maintained substitute that drifted from Quest and that no other consumer had. Now one
  standard file, grouped by period, each goal a checkbox bullet carrying its id. Three edits push,
  chosen because each is unambiguous on the page: tick a box (completes the goal), change the text
  after an id (renames it), add a bullet with no id under a period heading (creates it, then the
  bullet is stamped with its new id so a repeated push is a no-op). Un-ticking does NOT reopen a
  goal, since inferring that from a missing `x` would make any rendering hiccup a silent state
  change. `direction="both"` pushes BEFORE it pulls, opposite to `quest_folder_sync`: the edits
  live inside the managed block that a pull regenerates, so pulling first would erase a tick
  before it was ever sent. New client methods `update_goal` and `set_goal_completed`.
  See `docs/quest-folder-goals.md`.
- **The quest state block in `QUEST_SYNC.md` now pushes back too** (`push_quest_state`, run
  automatically on `direction="both"`). Editing `**Goal:**` or `**Status:**` sends the new outcome
  or completion up via the role-scoped write. Two fields render but deliberately do NOT push:
  `current_state`, which no role may write through that route, and `strategies`, which are objects
  (id, title, accepted) that a list of bare titles cannot faithfully reconstruct. Both are
  REPORTED in the result's `unwritable` list rather than dropped, and a server-side partial
  refusal (`{"ok": false, "blocked": [...]}`, which arrives in the body rather than as a status)
  is surfaced instead of being read as success. New client method `write_quest_fields`.

- **Three-zone provenance convention for synced quest folders** (`runner/quest_folder_zones.py`,
  on by default via `RunnerConfig.quest_folder_zones`). A folder synced to a quest accumulates the
  person's material and the AI's side by side, and nothing on the page says which is which. The
  consequence is a slow one: an AI run produces an analysis, the next run reads it as the brief,
  the run after builds on that, and eventually the plan rests on a premise the person never agreed
  to. Every pull now scaffolds `human_context/` (their words, written to only to record input
  VERBATIM or when asked) and `ai_driven/` (the AI's workspace, where everything is a proposal),
  leaves everything else as collaborative work product, and writes the rule into the folder's own
  `CLAUDE.md` as a managed section so it reaches runs that never touched this library. The
  person's own Quest notes and email replies are captured verbatim into
  `human_context/from_quest/`, keyed by note id so a repeated pull rewrites nothing, and an
  unknown `author_kind` is deliberately never treated as human -- a missed capture is recoverable,
  an AI note filed as the person's words is not. `ai_driven/provenance_ledger.md` records what
  they have actually reviewed (`ai_proposed` -> `surfaced` -> `approved`/`rejected`/`superseded`,
  only `approved` settled, each move quoting them), and any run on a foldered quest is told to
  check it before building on AI-authored analysis. See `docs/quest-folder-zones.md`.

### Fixed
- **A bare "hold off"/"not yet" anywhere in the message vetoed the WHOLE turn, even a mixed one
  that also gave a directive.** `message_forbids_new_task` treated "hold on", "hold off", "stand
  by", and "not yet" the same as the unambiguous veto phrases ("don't create a task", "just answer
  here"), so "hold off on the emails, but go ahead and update the leads sheet" and "the deploy is
  not yet done -- fix it" degraded the planner's `action="deep"` to an answer and executed nothing,
  even though the second half of each message was a live instruction. Unlike the other veto
  phrases, these four name no topic of their own (no "task", no "answer"): they just mean "pause",
  so they are ambiguous the moment something else in the message is itself a directive. Split into
  its own pattern (`_BARE_HOLD_PHRASE_RE`) and only honored as a turn-wide veto when the rest of the
  message carries no change verb used as a verb; a bare hold phrase with nothing else in the
  message still vetoes exactly as before. Covered by
  `tests/test_question_not_task.py::test_mixed_hold_and_directive_does_not_forbid_a_task`.
- **Autopilot re-proposed the same goal on every pass, forever.** On a `plan_and_work` quest with
  no eligible AI goal, each pass created a fresh "Next step toward: `<outcome>`" proposal without
  ever looking to see whether the LAST one had been answered. A proposal is one question, so the
  person got a duplicate suggestion in their task list every pass and the same "Waiting for your
  approval before it can run: Proposed goal: Next step toward: X (on X)" line in every report:
  the quest's outcome three times in one line, under a heading about work that can "run" when no
  work exists yet. A pass now checks for its own still-open proposal on the quest (autopilot
  authored, text starting `Proposed goal:`, status queued/in_progress/needs_you/suggested), and
  when it finds one it creates nothing, spends no budget, and reports one honest skip line saying
  the earlier proposal is still waiting on them. This is deliberately narrower than the (opt-in,
  off-by-default) backpressure gate: an unanswered question must not stop a quest from doing work
  that is independent of it, but a duplicate of that question is never such work. Proposals also
  get their own heading in the report ("A goal proposed for you to accept or reject"), and a
  created item no longer names its quest when its own title already does.
- **A runner on the Quest-API card backend was about to lose every per-quest card, silently.** The
  card API hides auto-maintained cards (anything carrying `managed_by`, today one card per quest)
  from `GET /api/cards` and `GET /api/cards/search` unless the caller passes
  `include_managed=true`. That default is right for the user-facing Topics list, where cards the
  user never wrote and cannot usefully edit are noise. It is exactly wrong for a runner, which
  reads cards to GROUND the AI and for which the per-quest cards are the point.
  `QuestApiCardRepository` sent neither request with the flag, so the moment a backend carrying
  that default deploys, any lane with `QAR_QUEST_API_URL` / `QAR_QUEST_API_KEY` / `QAR_USER_ID`
  set would get zero quest cards on the listing (`load_all`, `revision`, and the store's in-app
  IDF fallback) and zero on search, where the endpoint filters the merged keyword AND vector
  result. Nothing would have reported it: the failure mode is a feature that appears never to have
  worked. Both requests now ask for the full store, and
  `tests/test_quest_api_card_repository.py` asserts it against a fake backend that reproduces the
  API's own filtering.
- **The Quest-API card backend could never read a single card, so cards written through it stayed
  invisible.** `QuestApiCardRepository` broke the `CardRepository` protocol in three ways at once,
  each failing quietly: `revision()` was declared as a per-CARD stamp (`revision(card_id)`) while
  `FileContextStore._load_all` calls it with NO arguments, so every read raised TypeError;
  `load_all()` returned a LIST where the store requires `{card_id: card_dict}` and calls
  `.values()` on it; and `search_cards()` took `max_results` positionally and returned a list,
  while the store calls `search(text, limit=N)` and type-checks the result as a dict, so the
  native search arm never ran. It also sent the search limit as `max` rather than the `max_results`
  the endpoint reads. The result was that a runner configured to persist context cards centrally
  behaved as if it had none, which is the opposite of the point: the backend is meant to be the
  source of truth for cards, not each machine's local `.quest-context` directory. `revision()` is
  now a cheap store-wide stamp (local write counter, card count, newest `updated_at`), and the
  listing it needs is briefly cached and reused by the `load_all()` that immediately follows, so a
  store read costs one request rather than two. Both reply shapes for `cards` (the list the API
  serves now, and the mapping older deployments served) are accepted. Covered by
  `tests/test_quest_api_card_repository.py`, including a real `FileContextStore` reading through
  the repository.

### Changed
- **An autopilot pass now reports the WORK it set in motion, by name, instead of its own
  bookkeeping.** A pass row read `Autopilot pass complete. Created 1 task(s): atask_d2014273cff6`:
  an internal id where the work's name belongs, and the scanner's own accounting presented as the
  outcome. `AutopilotResult` now carries one record per created item (title, quest, persona,
  whether it is running or waiting for approval, the goals it covers, any recurring tasks it
  adopted), and `summary_text()` writes plain sentences from that: what started, what is waiting on
  the person, what was skipped and why, each named the way the person named it. Ids stay in
  `created_task_ids`, which is code's business. A dry run keeps its id-bearing detail on purpose:
  it is read while setting autopilot up, by someone checking the picks.
- **A pass stamps `parent_task_id` (its own id) on every task it creates**, and
  `QuestClient.create_task` accepts the field. That link is what lets a consumer answer "what did
  autopilot actually do" with the finished work itself: quest-backend now rolls a completed
  autopilot task's output back onto the pass row that created it, so the pass reports the work
  whether or not the quest mails anything. A client whose `create_task` predates the argument
  still gets its task, without the link.
- **Every run is told how to write its result, whether or not its quest mails** (new
  `RESULT_IS_THE_WORK_CONTRACT` in `runner/executor.py`). The rule used to arrive only inside the
  email contract, so the same work was written as a readable brief when email happened to be on
  and as a status note when it was off. Delivery is code's decision; how the work is written is
  not. The email contract keeps only what is specific to delivery.

### Fixed
- **`BM25ContentStore` found nothing, always, even for a query matching a term that exists
  verbatim in exactly one file.** `_search_one`'s hit filter was `isinstance(score, float) and
  score > 0`, but `bm25s`'s score matrix is `numpy.float32` -- which, unlike `numpy.float64`, is
  NOT a subclass of Python's builtin `float` -- so the `isinstance` check was silently false for
  every real hit and the arm returned zero results unconditionally. Caught by re-running this
  repo's own offline suite instead of trusting a stale "pre-existing failure" label: 10 of
  `tests/test_bm25_content.py`'s tests were failing before this fix, all downstream of the same
  line. Dropped the `isinstance` guard (`quest_ai_runner/adapters/bm25_content_store.py`); a
  malformed/non-numeric score still degrades safely via the method's existing catch-all. Also
  fixed the adjacent import-guard test, which asserted the constructor raises `ImportError` when
  `bm25s` is absent by popping it from `sys.modules` -- a no-op simulation whenever `bm25s` is
  genuinely installed, since Python just re-imports it fresh; now sets it to `None` in
  `sys.modules`, which is what actually forces the next `import bm25s` to raise.
- **`FastEditRunner`'s whole-file rewrite path could pad a file with an extra blank line on
  every redundant retry, and its success report was too terse for the goal-verification judge to
  actually confirm, which is what made a retry redundant in the first place.** Found by a 10-case
  real end-to-end battery (real model calls, real files, no mocks): a request already fully
  satisfied by the file's existing content (a false-premise typo "fix," or a retry against a file
  the first attempt already corrected) still came back as a "changed" file, because the model does
  not reliably reproduce the exact original trailing newline even when told to preserve it, and the
  no-op check compared for byte-exact equality. Each spurious "edit" was then reported to the goal
  loop as just `"Edited notes.md in 0.8s..."` -- no evidence of what actually changed -- so the
  verifier judge, with nothing to confirm against, unreliably called genuine successes not-met and
  triggered another attempt, which padded the file with another blank line. For a non-idempotent
  request ("append a line") the same evidence gap let a real duplicate land, not just whitespace.
  Fixed three ways in `quest_ai_runner/adapters/fast_edit_runner.py`: (1) a whole-file rewrite's
  trailing newline is now re-normalized to match the ORIGINAL file's own convention before the
  no-op comparison, so drift the model introduces can't defeat that check or accumulate across
  retries; (2) the success report now includes a short unified diff per edited file, giving the
  verifier real evidence instead of a bare mechanical summary (this is what actually closed the
  gap -- a real 10-case end-to-end battery against a deliberately weak/cheap verify-tier model,
  including a genuinely non-idempotent "append a line" request, went from repeatedly duplicating
  content across goal-loop retries to landing correctly on the first attempt once the verifier had
  something to confirm against); (3) both wire-format system prompts gained an explicit "check
  whether this is already done before you act" rule as defense in depth.
  (`tests/test_fast_edit_runner.py`, `tests/test_fast_edit_ladder.py`.)
- **The fast answer loop's read budget was too tight for an ordinary few-file request, so a turn
  could give up mid-cascade and answer with a false "I wasn't able to pull X" instead of ever
  reaching the planner's next read step.** `DEFAULT_MAX_ELAPSED_SECONDS`/`DEFAULT_MAX_GATHERED_CHARS`
  (60s / 60,000 chars) bound the WHOLE read cascade for a turn, shared across every grep and read
  issued this turn, not per-read. A single broad grep earlier in the same turn could exhaust the
  budget before the planner ever got a re-plan cycle to request specific named files, at which
  point the loop wraps up with a `partial=True` best-effort answer whose grounding explicitly tells
  the model to "say plainly it needs to dig further" -- honest about the gap, but the gap itself was
  an artifact of an undersized budget, not a real retrieval failure. Widened to 90s / 150,000 chars
  (`quest_ai_runner/core/orchestrator.py`).
- **`qar <name>` (documented as the preferred way to open a persona chat, e.g. `qar wadona`) did
  not actually work: the `chat` subcommand's argparser had no positional argument at all, only
  `--rep`, so it failed with an "unrecognized arguments" error.** Added an optional positional `rep`
  argument as shorthand for `--rep NAME`; an explicit `--rep` still wins if both are given. When no
  `--persona-file` is given either, it also looks for `<corpus_root>/<name>/CLAUDE.md` (the
  character-folder convention) and loads it as the persona automatically, with no LLM call, same
  effect as passing `--persona-file` explicitly. Falls back to just setting the display name if no
  matching folder exists. (`tests/test_cli_chat_rep_positional.py`.)
- **A family-bucket label can no longer reach `claude --model`, which made deep runs fail
  identically on every retry.** Live: six consecutive attempts, each ending in the binary's own
  "There's an issue with the selected model (claude-sonnet). It may not exist or you may not have
  access to it", and then a "deep task complete" for work that never ran. `claude-sonnet` is not a
  model id: it is a family-BUCKET LABEL from `ClaudeCliProvider.CLI_RUNNABLE_MODELS`, which exists
  so `ModelRegistry.bucket_top` can bucket tiers on a CLI-only deployment that has no live model
  list. The spawn gated on `_is_claude_model`, which asks a purely SYNTACTIC question ("is this
  string Claude-shaped?") — the label passes it, so it was forwarded raw. The translator for
  exactly this already lived beside the labels it invents (`claude_cli_provider.cli_model`: family
  → the bare alias the CLI accepts, non-Claude → `None`, other Claude ids unchanged) and the
  shallow plan/answer path has used it since it was introduced; the deep path was simply missed.
  `SubprocessGoalRunner` now routes `--model` through it via `core.goal_runner.cli_safe_model`
  (lazily imported, so `core` still doesn't depend on `adapters` at import time, and degrading to
  the old syntactic gate if the adapter is absent). Two consequences worth knowing: a non-Claude id
  still omits `--model` entirely, exactly as before; and a dated id now invokes as its family alias
  (`claude-opus-4-8` → `opus`, i.e. latest of family), which is the same translation every shallow
  call has always used. (`tests/test_deep_failure_session_diagnostics.py`, `tests/test_runner.py`.)
- **The deep-model ladder now has a second rung on a CLI-only deployment, so "escalate on a
  not-met goal" escalates.** `fallback_deep_ladder` extended the ladder only when the fallback
  model was NOT Claude-runnable, on the reasoning that a Claude deployment had already chosen its
  deep model. But "Claude-shaped" and "a distinct rung worth escalating to" are different
  questions, and a CLI-only lane resolves its tiers to bucket labels — so the guard saw Claude,
  returned a length-1 ladder, and the goal loop's attempt-N indexing re-ran the identical model on
  every attempt. That is the other half of the six identical failures above: the escalation
  mechanism ran exactly as designed, with nothing to escalate to. The extension is now attempted
  for every fallback, and rungs are deduped by what the worker would ACTUALLY invoke
  (`cli_safe_model`), so `claude-sonnet`, `sonnet` and `claude-sonnet-4-6` count as one rung rather
  than three lookalike steps that would re-run the same model — a stronger tier is added only when
  it is genuinely a different model. Pins (an explicit per-task model request, a guidance model
  preference) still return a single-model ladder before this is reached, and the existing
  "escalation unavailable" WARNING still fires when nothing distinct can be found.
  (`tests/test_deep_escalation_ladder.py`.)
- **The terminal record shows a repeated failure once, with a count, instead of the same sentence
  six times.** A goal loop's retries report under the same `run_id`, so a failure identical on
  every attempt filled the run's scrollback record with the same line repeatedly and pushed
  everything else the run said out of the capped narration tail. `_summarize_exec_lines` now
  collapses CONSECUTIVE identical narration lines into one, tagged "(repeated N times)". Only
  consecutive ones: the same line said again after something else happened is a real second
  occurrence, not noise. Tool actions were already rolled into the counts line and are unaffected,
  and an interleaved tool or thinking line does not split a run of identical messages (neither is
  narration). (`tests/test_deep_output_ui.py`.)
- **A deep run that dies before printing its final envelope now reports what it ACTUALLY did,
  read from its own session record.** Observed live: a run read half a dozen files, wrote and ran
  a sanity-check script, wrote a design doc, edited three files and drafted an email, then failed
  — and the whole human-readable result was "The worker exited 1 with no error output. … Read the
  run output below for what it actually did", with nothing below it. The cause is that everything
  a human reads on a failure was derived from ONE source: the worker's final `--output-format
  json` envelope on stdout. A worker killed, crashed or reaped mid-flight never gets to print that
  envelope, so `out` is empty, so the `if tail:` branch that attaches "Last output:" never fires,
  and the message carries nothing — the exact failure mode where knowing what the run did matters
  most. A complete record existed the whole time: the Claude Code session JSONL under
  `.claude/projects/`, bound deterministically by the `--session-id` this runner itself generates,
  which the live monitor thread was already tailing line by line. Both blind-spot paths (`exit
  != 0` with no stderr, and the exit-0-with-empty-output no-op net) now fall back to the tail of
  that same file when the worker's own output is empty or too thin to diagnose anything from.
  Two deliberate reuse decisions: the file is located through the monitor's own
  `_find_claude_project_dir` + session-id match, so there is exactly one definition of "where this
  run's session file is"; and each record is rendered by the monitor's own `_format_message_text`,
  so the end-of-run summary reads like the live progress stream (`- Read: <file>`, `- $ <command>`,
  `- <assistant text>`) instead of being a second, divergent parser for the same format. The block
  is framed as an observation, never a diagnosis ("its own session record shows it last did (most
  recent last): …"), keeping the rule that this message states recorded facts and does not assert a
  cause the code cannot know; the one place that DID assert one — "the goal did not actually run"
  on an exit-0 empty run — now softens to "cannot be confirmed to have run" only when the record
  contradicts it, and is otherwise unchanged. Because this runs synchronously on the failure path
  it is bounded and silent: at most the last 256KB of the file is read, at most 12 actions and 1500
  characters are rendered, malformed or truncated records are skipped individually (a file cut off
  mid-write still yields every complete record before it), and a missing, unreadable or
  unparseable record simply falls back to today's bare message. It never raises, and never engages
  on a run that produced real output of its own.
  (`core/goal_runner.py`; `tests/test_deep_failure_session_diagnostics.py`.)

### Added
- **Documented the guidance-card system ([`docs/guidance-cards.md`](docs/guidance-cards.md)).** The
  whole mechanism for standing rules (`core/guidance_provider.py`,
  `adapters/guidance_card_manager.py`, `adapters/quest_guidance_loader.py`,
  `adapters/feedback_processor.py`) shipped with no public documentation, so nothing about it was
  discoverable from the repo: not the card format, not the tag vocabulary, not that guidance is
  auto-enabled by `build_orchestrator`, not that cards can be served from a host database to many
  machines at once. The new doc covers the card format and where the cards directory resolves from,
  the scope/operation/function/task tag vocabulary and the exact selection weights, the per-turn
  `APPLICABLE GUIDANCE` block and its timeout, how selected cards double as the goal-verification
  quality bar, `list_guidance`/`read_guidance`, the model-preference directive, the file + dynamic
  loader split, `FeedbackProcessor`, authoring rules, wiring, and a "what this does not do yet"
  section (no model-scoped selection, no per-card history in the hosted lane). Also records a
  signature mismatch found while writing it: `GuidanceProviderBase.select()` declares
  `(user_message, *, k=3, meta=None)` but the orchestrator calls
  `select(user_message, team_id=..., org_id=..., limit=...)`, so an ABC subclass matching the
  declared signature raises `TypeError`, which is caught and silently costs the turn its guidance.
  Documented as "accept `**kwargs`" pending a fix. Linked from `README.md` and `docs/README.md`.
- **Org-scoped environment heartbeat registration.** The environment heartbeat (how a runner tells
  the Quest backend "I'm alive, here's what I can do" so the backend can route deferred AI work to
  it) could previously only register at TEAM scope, even though the Quest backend already supports
  an org-scoped heartbeat endpoint -- so a shared/org-wide runner deployment had no way to make
  itself visible to every team in an org, only one team at a time. New optional
  `RunnerConfig.org_id` (loaded from `QUEST_ORG_ID`): when set, `QuestClient.post_environment_
  heartbeat(..., org_id=...)` POSTs to `/api/orgs/{org_id}/environment/heartbeat` instead of
  `/api/teams/{team_id}/environment/heartbeat` (same body shape, no backend changes needed), and
  `Poller._emit_heartbeat` passes it through automatically when configured. `team_id` remains
  required for task claiming/escalation regardless -- `org_id` only changes where the heartbeat
  lands. (`config.py`, `cli.py`, `runner/quest_client.py`, `runner/poller.py`;
  `tests/test_quest_client_heartbeat.py`, `tests/test_runner.py`.)
- **Live, two-way messaging channels -- a hub-to-hub bridge to OpenClaw over MCP.** QAR could only
  run queued Quest tasks (`poller.py`) or an interactive terminal session (`interactive_session.py`)
  -- nothing let it hold a real-time conversation over a phone chat app. New generic
  `core.adapters.ChannelTransport` interface (Protocol + `ChannelTransportBase` ABC, value objects
  `InboundMessage`/`InboundBatch`/`OutboundReply`/`SendResult` -- same "never raises, every failure
  is a returned value" contract as every other adapter role) plus `runner/channel_runner.py`'s
  `ChannelRunner`, the loop that drives one: receive a batch, AUTHORIZE each message against an
  explicit `RunnerConfig.channel_allowed_senders` allowlist (EMPTY = DENY ALL, fail closed -- a
  plain membership test against operator config, never a decision based on model-generated text,
  per this repo's hard rule #3), DEDUP via `runner/state_store.py`'s `StateStore` (extracted out of
  `poller.py`, mechanically, so both lanes share one dedup mechanism -- see
  `tests/test_state_store_extraction.py`), run at most one orchestrator turn per `chat_ref` at a
  time (a message arriving mid-turn folds into the running turn via `core.inbox.InputInbox` instead
  of starting a second one), and guarantee EXACTLY ONE terminal reply per turn -- answer,
  decision-relay, or a plain error message, even when the orchestrator itself raises
  (`runner/channel_session.py`'s `ChannelSink`, which also throttles milestone/progress sends, fires
  one "still working" ack on a long turn, and reuses `interactive_session.ChatSessionStore` per chat
  so anaphora resolution ("ok do it") works the same way it does in the terminal). Reference
  transport: `adapters/openclaw_channel.py`'s `OpenClawChannel` wraps the Phase-1 `MCPClient`
  (spawns `openclaw mcp serve --token-file <path>`, auth injected as a token FILE PATH, never a
  hardcoded token) to talk to [OpenClaw](https://github.com/openclaw/openclaw) (MIT) -- one bridge
  against OpenClaw's confirmed MCP tool surface (`events_wait`/`messages_send`/`attachments_fetch`)
  means every channel OpenClaw has configured (WhatsApp/Telegram/Discord/Slack/Google Chat/Signal)
  becomes a live QAR channel with no new QAR code; a new channel is OpenClaw-side config. This
  bridge structurally never calls OpenClaw's `permissions_respond` (approvals are QAR/Quest's job,
  never the gateway's -- pinned by `tests/test_openclaw_channel.py::
  test_bridge_never_calls_permissions_respond`), and a raised `EVENT_DECISION` is relayed to the
  chat as a message, never auto-resolved from a channel reply (a separate trust decision, out of
  scope here). New `channel` CLI subcommand (`quest-ai-runner channel [--once|--check]`), its own
  process/entry point -- NOT folded into `poller.py`'s loop or `TaskExecutor`, since a chat message
  has no Quest task id to claim/PATCH. `docs/live-channels.md` documents the non-negotiable OpenClaw
  lockdown checklist (version pin >= v2026.1.29 fixing CVE-2026-25253, no skills/plugins/cron/
  browser-automation, no agent/model configured in OpenClaw at all, Gateway bound to localhost only,
  OpenClaw never holding `QUEST_API_KEY`/model keys/corpus access) an operator must verify before
  connecting this bridge to a real Gateway. **Proven against fakes only** (no live OpenClaw
  instance was available at authoring time; the exact `events_wait`/`messages_send`/
  `attachments_fetch` payload shapes are documented assumptions, tolerant-parsed, see
  "Unverified assumptions" in `docs/live-channels.md`) -- a live end-to-end run needs a real
  channel bot token only a human operator can create.
  (`core/adapters.py`, `adapters/openclaw_channel.py`, `runner/channel_runner.py`,
  `runner/channel_session.py`, `runner/state_store.py`, `config.py`, `cli.py`;
  `tests/test_openclaw_channel.py`, `tests/test_channel_runner.py`,
  `tests/test_state_store_extraction.py`; `docs/live-channels.md`.)
- **Generic MCP (Model Context Protocol) client support -- Phase 1, read-only foundation.** New
  optional `[mcp]` extra (pinned `mcp==2.0.0`, the current PyPI release at the time this was built,
  reflecting the protocol's July 2026 revision) adds two pieces, both offline-safe to import (the
  `mcp` package is imported lazily inside a single connection seam, exactly like
  `AcpDeepRunner`/`agent-client-protocol`, so `quest_ai_runner.adapters` never requires it):
  `adapters/mcp_client.py`'s `MCPClient` is a raw protocol client (stdio subprocess or streamable
  HTTP transport, injected bearer auth via the same `TokenProvider` pattern as
  `GoogleChatAdapter`) that connects/discovers/lists tools+resources/calls a tool/reads a resource
  and NEVER raises -- every failure (missing extra, dead process, protocol/timeout error) comes
  back as a value. `adapters/mcp_retrieval_adapter.py`'s `MCPRetrievalAdapter` maps that onto the
  standard `RetrievalAdapter` discovery quartet (`list_sources`/`describe_source` from
  `resources/list`, `list_operations`/`describe_operation` from `tools/list` with the tool's own
  schema rendered as text, `read_section` from `resources/read`, `query({"tool", "args"})` from
  `tools/call` gated by an explicit `allowed_tools` allowlist -- a tool call outside it is refused
  before `MCPClient.call_tool` is ever reached; `grep` is honestly unsupported, no MCP analogue).
  Every surfaced name is namespaced by a configured `alias` (e.g. `issues:search`) so multiple MCP
  servers coexist in a `CompositeRetrievalAdapter` without collision. New
  `RunnerConfig.mcp_servers: List[MCPServerSpec]` folds each spec into the retrieval stack the same
  way the native web-search adapter is folded in inside `build_orchestrator()`. Also threaded MCP
  server config through to the deep-run layer: `SubprocessConfig.mcp_config_path` passes `claude -p
  --mcp-config <path>` through when set (confirmed against the installed `claude` CLI's own
  `--help`); `AcpConfig.mcp_servers` replaces a previously hardcoded empty list passed to the ACP
  agent's `session/new`. Scope is deliberately read-only foundation only -- a write-capable MCP
  adapter and a live-messaging-channel lane are separate, later work.
  (`adapters/mcp_client.py`, `adapters/mcp_retrieval_adapter.py`, `config.py`, `core/goal_runner.py`,
  `adapters/acp_deep_runner.py`; `tests/test_mcp_client.py`, `tests/test_mcp_retrieval_adapter.py`,
  `tests/test_deep_runner_mcp_passthrough.py`.)
- **Generic MCP write support -- Phase 2, gated exactly like every other write in this library.**
  Investigated first, not assumed: `FileWriter.write_file` (`core/adapters.py`) has exactly ONE
  reachable caller in the whole library, `FastEditRunner.apply_response`, and `FastEditRunner` is
  reachable only through `config.resolve_deep_runner_ladder` -> `Orchestrator._run_deep`, itself
  reachable only once the planner's own structured decision is `action: "deep"` -- nothing in the
  plan/gather/re-plan loop can call it. New `OperationWriter` Protocol + `OperationWriterBase` ABC
  (`core/adapters.py`) generalize that same gated-write shape from "replace a file's content" to
  "execute a named, schema-described mutating operation" (an MCP write tool is the motivating case,
  but the interface is not MCP-specific), reusing `WriteResult`'s existing value-not-exception
  contract rather than inventing a parallel one -- `WriteResult` gained an optional `detail` field
  for adapter-specific auditability (an MCP write's executed `{"tool", "args"}`) since a mutation
  here does not have a "path" the way a file write does; every other field keeps its FileWriter
  meaning unchanged. `adapters/mcp_write_adapter.py`'s `MCPWriteAdapter` implements it over
  `MCPClient.call_tool`, gated by its own `writable_tools` allowlist -- a SEPARATE list from
  `MCPRetrievalAdapter.allowed_tools`; being read-allowlisted grants no write access, and a refused
  call never reaches `MCPClient.call_tool` (spy-verified). `MCPServerSpec` gained a
  `writable_tools: List[str]` field alongside the existing `allowed_tools`, so one spec's
  connection can back both a read and a write adapter with independent policies.
  `adapters/mcp_write_runner.py`'s `MCPOperationRunner` is the `OperationWriter` analogue of
  `FastEditRunner`: a `DeepRunner` that picks at most one writable operation (from the writer's own
  discovered catalog) and its arguments via ONE forced-structured-output model call (never keyword
  scanning of free text, same discipline hard rule #3 requires of the main planner), executes it,
  and returns -- declining (empty catalog, or the model's own explicit decline) does nothing and
  reports `met=False`, escalating to the next rung exactly like FastEditRunner's "no candidate
  file" case. New `config.resolve_mcp_write_runners()` builds one `MCPOperationRunner` per
  `MCPServerSpec` with non-empty `writable_tools` (mirroring `resolve_fast_edit_runner`'s opt-in
  shape: a single condition, no env-var escape hatch) and `resolve_deep_runner_ladder` folds them in
  as an additional rung between `FastEditRunner` and the full deep runner -- the SAME ladder, SAME
  gate, no parallel escalation path. A default consumer (no `writable_tools` anywhere) sees byte-
  for-byte the same ladder as before this change.
  (`core/adapters.py`, `adapters/mcp_client.py`, `adapters/mcp_write_adapter.py`,
  `adapters/mcp_write_runner.py`, `config.py`; `tests/test_mcp_write_adapter.py`,
  `tests/test_mcp_write_runner.py`, `tests/test_mcp_write_ladder.py` -- the last of which drives the
  real orchestrator loop to prove a plain answer/read turn can never reach `write_operation`, with a
  positive control proving the same wiring genuinely does execute a write once the planner actually
  decides `action: "deep"`.)
- **An attended chat session standing in a quest's folder now OPENS already holding that folder's
  standing next-steps answer, instead of re-deriving one when asked.** The
  `QAR:MANAGED:next_steps` block in `QUEST_SYNC.md` was built as the one canonical "what to do next
  here", and the autopilot pass has read it and refreshed it since it landed; the attended half was
  deliberately left unwired at the time, because doing it would have meant editing files another
  process had open. So a person opened a fresh `qar chat` in exactly that folder, asked what to do
  next, and watched the AI work it out from scratch from goals, notes and files. The artifact was
  never excluded from indexing, so retrieval COULD surface it, but only by competing with every
  other file in the corpus on relevance, which is not what a standing answer is for. New
  `runner/session_next_steps.py` closes it: `InteractiveSession.__init__` (the session brain every
  chat entry point constructs) reads the block once, and `_effective_preamble()` threads it into
  every turn's `rep_preamble` alongside the persona, so it reaches the planner, the answer and the
  deep preamble structurally rather than when retrieval happens to score it. It is labelled where it
  lands: named as the current authoritative answer, told apart from a search result, with an
  instruction to START from it and say where it came from, and to say plainly if the work has moved
  past it. The freshness stamp travels inside the block itself (`render_next_steps` writes
  "Refreshed <date>, by <source>" as its first line), so the label cannot contradict it. Pure local
  file read, no Quest call, nothing that can block startup, and a very long hand-edited block is
  capped before it taxes every turn. (`tests/test_session_next_steps.py`.)
- **A turn that ran real work and left some of it unfinished now writes that back as the quest's
  standing next steps.** Deliberately narrow, on autopilot's own precedent that only a productive
  pass refreshes the artifact: the trigger is a completed (not cancelled, not errored) `kind="deep"`
  turn that produced at least one `DeepResult` and did not finish every goal. Everything else leaves
  the artifact alone, including small talk, an ordinary answer, a clarifying question, and a deep
  plan that never executed. A turn that finished ALL its goals also leaves it alone: it knows what
  it completed but not what comes after, deciding that is a planning judgment this refuses to spend
  a model call on, and replacing a considered answer with an empty block would leave the folder
  worse off than the stale one it overwrote. A DEFERRED result counts as unfinished, since queued
  out of band is not done. Per-goal attribution happens only when goals and results line up; a
  sequential-group deep run records results without a matching goal entry, so a partially finished
  unalignable turn keeps every goal listed rather than guessing which result belonged to which goal.
  The conclusion is deterministic and LLM-free, like `next_steps_from_pass`, and goes out through
  the existing `publish_next_steps` (local file first, then the Quest-side upsert). Nothing here can
  fail a turn: no folder, no artifact, no quest id, no Quest client, or a failed write all leave the
  session behaving exactly as before. (`runner/session_next_steps.py`: `next_steps_from_turn`,
  `refresh_from_turn`.)
- **A quest folder can now say which quest it belongs to without any configuration.**
  `quest_folder_sync.quest_id_in_folder` reads the `quest_id` its sync file already stamps in
  frontmatter. `RunnerConfig.quest_folder_map` remains the FIRST answer wherever it is set (it is
  the deployment's own statement, and `quest_for_path` also returns the mapped root, which is where
  the artifact lives when a session starts in a subfolder) — but the map is opt-in env config a chat
  user need not have set up, and the fallback is what lets the read and the write-back resolve in a
  folder that was ever pulled. Only the frontmatter counts: a `quest_id:` line elsewhere in the file
  is prose, not the mapping.
- **An autopilot pass now sees what the person has CAPTURED since it last ran, not only the rows
  the system recorded.** Quest gives every person an "Insights" collection: quick capture for the
  realization that arrives away from any goal ("mornings are the only time the writing happens"),
  carrying the free-text **category tags** they chose, an `acted_on` checkbox, and what was done
  about it. It is the one place in Quest holding something a person wrote down that has not become
  a goal or a task yet, and `grep -rn insight quest_ai_runner/` found nothing: a capture made on
  Tuesday sat untouched while every pass since composed its brief as if it had never been written.
  Adds `QuestClient.get_insights_collection` / `list_collection_entries` / `mark_insight_acted_on`
  and `runner/insights.py`, which composes recent unacted captures into one dated, tagged
  `InsightsContext`. The entries route is generic and has **no** server-side filter — not by date,
  not by field — so both halves of the selection are applied client-side, mirroring quest-backend's
  own `_get_recent_unacted_insights`: skip anything ticked `acted_on`, bound by the later of the
  caller's cutoff and a 14-day window, cap and clip the rest. Newest-first ordering lets paging stop
  at the first entry past the cutoff instead of walking a person's whole history. Both timestamp
  shapes the same field arrives in are handled (an ISO string over HTTP, a real `datetime` in
  process), and an offset-bearing timestamp is *converted* rather than stamped, since reading
  `23:30-07:00` as UTC moves a late-evening capture across a midnight cutoff. A client without the
  methods, a 404, or a transport failure all degrade to an empty context.
  (`docs/quest-api-contract.md`; `tests/test_insights.py`.)
- **The category tags reach the model as context, and are never matched against anything in code.**
  Each insight is rendered with the tags exactly as the person typed them, and the block closes by
  saying plainly that they are the person's labels for their own thinking rather than a routing
  rule: one tagged for something else can still matter here, one whose tag looks like a match can
  be irrelevant, decide and pass over the rest without comment. That judgment belongs to the model
  already weighing goals, next steps, and reflections for this quest. The alternative — comparing a
  tag against a quest or goal name — is a fixed string rule that silently drops every wording it did
  not anticipate ("dissertation" vs. "thesis" vs. no tag at all), which is exactly what hard rule #3
  forbids. `compose_batch_text` carries the block beside the reflection, for the same reason and
  with the same framing; `next_steps_from_pass` puts one condensed line in the artifact's `note`
  slot but never promotes an insight to a *step*, because the person captured it rather than
  committing to it. The freshness cutoff is the quest's own `autopilot.last_pass_at` — the same
  stamp the cadence gate reads, so "since the last time it ran" cannot drift out of step with when
  it actually ran, and no second tracker exists to go stale. Insights are user-scoped, so a pass
  reads them ONCE over the widest window it could need and re-cuts that result per quest in memory
  (`InsightsContext.narrow_to`); a quest with no `last_pass_at` sees the whole window, since on a
  first pass everything recent is new. A client without the insight methods composes exactly the
  batch it composed before. (`tests/test_autopilot_insights.py`.)
- **`mark_insight_acted_on` exists, and autopilot deliberately does not call it.** A pass creates a
  task; it does not do the work. Ticking the box at pass time would claim an action that has not
  happened, and a ticked insight drops out of every unacted list — including the one the person's
  weekly review is built from — so an insight marked acted-on for a task that is then never approved
  or that fails has been silently removed from their own list with nothing to show for it. The
  method is there for a surface that knows the work actually landed, reports failure rather than
  swallowing it, and is documented with that boundary in `docs/quest-api-contract.md`.
- **A bounded file edit can now be landed in ONE model call instead of spawning a full agent, and
  with it quest-ai-runner gains its first write capability, off by default.** Until now the only
  way this library could change a file was to escalate to a deep run: `SubprocessGoalRunner`
  spawning `claude -p`, up to `--max-turns 30`, with an hour-long timeout. That is the right tool
  for open-ended work and an absurd one for "fix the stale status line in that doc". The real
  defect was not cost, it was waste by construction: by the time the brain decides to execute it
  has ALREADY assembled the relevant context (cards, targeted reads, the conversation slice), and
  spawning a fresh agent throws all of it away and pays a second time for it to be rediscovered.
  Against a one-line edit that is roughly 6-10x the wall clock (a handful of sequential turns plus
  agent startup, versus one round trip) and about 10x the tokens.

  `adapters.FastEditRunner` is a `DeepRunner` that takes the other path: hand the model the context
  QAR already has plus the current content of the candidate files, ask for the edit in one
  `provider.answer()` call through `MultiProvider`, apply it in process, return. The wire format is
  chosen by file size rather than by cleverness — at or below 400 lines the model returns the
  file's COMPLETE new content, whose apply step is a `write_text` and therefore cannot fail to
  apply, and above that it emits SEARCH/REPLACE blocks. The blocks are matched by CONTENT, never by
  line number: exact match, then a uniform-indent-drift match (the mistake models actually make),
  then explicit `...` elision. A block that matches nothing leaves the file untouched and produces
  a precise diagnostic (which lines are really there, and whether the replacement is already
  present), which feeds ONE in-process retry; past that, escalating is cheaper than arguing. If
  several blocks target one file and any fails, the whole file is abandoned unwritten, because a
  partially applied chain is the one outcome worse than no edit at all.

  **The write boundary is the part that got the care, not the LLM plumbing.** `adapters.FilesWriter`
  is the only component in this library that writes into a consumer's corpus (everything else that
  opens a file for writing writes QAR's own state). Its containment runs through
  `files_adapter.resolve_in_tree` — deliberately the SAME function the read adapter resolves
  through, extracted rather than copied, because two independent implementations of one security
  boundary is itself the risk: they drift, and only one of them gets the fix. It resolves before it
  tests, so `..` is normalized AND symlinks are followed first, and containment is
  `resolved.relative_to(root)` catching `ValueError`, never a string comparison. A traversal, a
  symlinked directory or file escaping the root, and an absolute path outside the root are all
  refused with the outside file provably untouched; an absolute path INSIDE the root and a target
  that does not exist yet (the ordinary create case) are allowed. Writes refuse credential-ish
  files (`.env*`, `*.key`, `*.pem`, anything named like a secret) exactly as reads already did.
  Before an existing file is replaced its content is copied to a backup, and a backup that was
  asked for and could not be written REFUSES the overwrite rather than proceeding, since proceeding
  silently converts a recoverable edit into a destructive one. Backups live outside the corpus
  (`~/.quest-ai-runner/file-backups`, or `QAR_FILE_BACKUP_DIR`) so they are neither indexed as
  content nor left as untracked files in the consumer's own version control. They are NOT left to
  git: this library cannot know that a given corpus root is under version control at all, and a
  synced quest folder or a Drive mirror frequently is not. Every refusal is a
  `WriteResult(ok=False)`, never an exception, and `ok=False` always means the file is unchanged.

  The runner may only touch files that were ALREADY in the turn's context: candidate paths are read
  out of the goal/brief/preamble, resolved through the writer's boundary, and must exist — and that
  candidate set is enforced again as an allow-list at apply time, so the model cannot widen its own
  blast radius. The text scan only NOMINATES; the filesystem decides. With no candidate it spends
  no model call at all and returns not-met, so the failure direction is toward the more capable
  path rather than toward acting.

  **Off by default, and one switch turns it on.** `RunnerConfig.file_writer` (default `None`) is
  the only way write access is granted: no env var, no auto tri-state. Left unset, no object in the
  wired brain can modify a file, `resolve_deep_runner_ladder` returns a one-rung ladder holding
  exactly the runner the consumer already had, and behaviour is byte-for-byte what shipped before.
  `cfg.deep_runner` deliberately keeps meaning what it meant (a single runner — consumers and both
  chat UIs read it to decide whether execution is available); the ladder is the orchestrator's
  business. New: `core.adapters.FileWriter`/`FileWriterBase`/`WriteResult`,
  `config.resolve_fast_edit_runner`, `config.resolve_deep_runner_ladder`,
  `Orchestrator(deep_runner_ladder=...)`. (`docs/fast-edit-runner.md`, linked from `docs/README.md`
  and `docs/adapters.md`; `tests/test_file_write_containment.py`,
  `tests/test_fast_edit_runner.py`, `tests/test_fast_edit_ladder.py`.)

  The SEARCH/REPLACE parser and matcher are a MODIFIED copy of Aider's
  `editblock_coder.py`/`wholefile_coder.py` (Apache-2.0, commit `5dc9490b`), vendored into
  `quest_ai_runner/vendor/` with the modifications listed in that file's header per Apache-2.0
  §4(b) and attribution recorded in `NOTICE`. Vendored rather than depended on because Aider never
  published that layer as a package, and vendored rather than reimplemented because nearly every
  line of it absorbs a specific way real models get the format wrong (markers matched with
  `{5,9}`-repeat regexes because models miscount `<` and `=`; the filename recovered by walking
  back three lines through fences because models put it in the wrong place). Aider's own ablation
  measured a 9x increase in editing errors when content-anchored matching was removed, which is
  also why an LLM-emits-a-unified-diff design was rejected: every standalone diff library for
  Python anchors on line numbers models are unreliable about.

### Removed
- **BREAKING: the ANSI / `prompt_toolkit` chat renderer is gone. Textual is now the one and only
  chat UI, with no fallback.** `quest-ai-runner chat` used to try Textual and, on any import or
  startup failure, quietly drop into a second, independently-maintained terminal renderer built on
  raw ANSI cursor math (`_ContextPanel`'s spinner/source panel), a raw-stdin `termios` ESC watcher,
  a `prompt_toolkit` REPL, and its own turn renderer and footer. That fallback stopped earning its
  keep: `textual` has been a CORE dependency (not an extra) since it landed, so the fallback could
  only ever fire on a broken or unsynced install; and every UI feature since — prompt docking, clean
  log routing, in-app selection and OSC-52 copy, the multi-deep dashboard, the mid-turn decision
  prompt — had to be either written twice or silently left missing on the ANSI path. This project
  has decided to maintain one chat UI well rather than two unevenly, so the whole ANSI rendering
  implementation was deleted rather than deprecated.

  What this means in practice:
  - `quest_ai_runner.interactive` no longer exists. The shared session brain it contained —
    `InteractiveSession` (orchestrator construction, persona auto-resolution, bootstrap-log
    suppression, session save/load and history, `/status`/`/tasks` bookkeeping, the model-tier menu
    data, the Quest client) plus `_Console`, `_DeepRunTracker`, `_HELP`, `_BANNER` and
    `_SLASH_COMMANDS` — moved unchanged to **`quest_ai_runner.interactive_session`**. It sits at the
    top level, beside `textual_ui.py` and `textual_session.py`, rather than in `core/`, because it
    wires concrete config and adapters and so belongs on the same layer as the other entry points;
    `core/` stays adapter-agnostic. Update any import of `quest_ai_runner.interactive` to
    `quest_ai_runner.interactive_session`.
  - `start_interactive()` is gone. `textual_session.start_textual_interactive()` is the only entry
    point for an attended session.
  - Deleted outright: `_ContextPanel`, `_PanelAwareLogHandler`, `_EscWatcher`, `_TurnRenderer`, the
    `PromptSession` REPL and its slash completer/history, and `InteractiveSession`'s ANSI-only
    members (`_run_turn`, `_print_turn_footer`, `_print_header`, `run`, and the five
    `prompt_toolkit`-driven pickers `_cmd_models_menu`, `_pick_from_list`, `_cmd_goal`,
    `_cmd_quests`, `_cmd_reps` — the Textual UI has had its own native versions of all of these).
    One latent bug goes with them: `InteractiveSession.__init__` cleared the root logger's handlers
    and installed `_PanelAwareLogHandler`, and since Textual builds the session in a background
    worker AFTER `on_mount`, that clobbered the `RichLog` handler Textual had just installed.
  - `prompt_toolkit` is dropped from `dependencies` in `pyproject.toml`; nothing else in the
    package used it. `rich` and `textual` stay core.
  - When `textual` genuinely cannot be imported, `chat` no longer degrades or dies on a raw
    traceback: it logs one actionable error naming the missing package and the `pip install
    --upgrade` that fixes it, and exits non-zero. That message is the whole safety net now, so it is
    tested (`tests/test_cli_chat_requires_textual.py`).
  - Tests that covered the deleted renderer went with it (`tests/test_spinner_panel_overwrite.py`,
    and the `_TurnRenderer` halves of `test_ux_features.py`, `test_understanding_event_ui.py` and
    `test_no_deep_executor_honesty.py`); their historical bug-fix value stays in git history and in
    the entries above. Every test of the SHARED session logic was kept and repointed at the new
    module.

### Changed
- **The deep runner is resolved as an ORDERED LADDER, so escalating by runner needs no new logic.**
  `_run_deep`'s goal loop already ran an attempt, verified it against the written done-standard,
  and retried on failure — but it escalated only by MODEL. The runner was resolved once per task
  into a single `active_runner`. It now resolves a list, and the attempt loop indexes it with the
  same `min(index, len - 1)` shape the model ladder already used, so attempt 1 runs the cheapest
  rung and a rung that fails verification hands the next attempt to the next one. A one-rung ladder
  (every consumer that changed nothing) behaves exactly as a single runner did. Two consequences
  fell out of it: the optional-kwarg probes (`emit` / `run_id` / `context_preamble` /
  `working_dir`) are now computed PER RUNNER and cached by identity, since the rungs are different
  objects with different signatures and a probe of one says nothing about another; and the rule
  "a hard failure with no output is terminal" now applies only when there is no further rung — it
  was written when a goal had one runner, so "this runner produced nothing" and "nothing more can
  be tried" were the same statement, and on a ladder a first rung that declines is precisely the
  case the next rung exists for. A pinned `runner_override` and a classifier-selected named runner
  each collapse the ladder to one rung, because both are deliberate routing decisions that
  prefixing would override. `_has_deep_execution_capability` and `derive_capabilities`'s `code`
  flag now probe the ladder, so a consumer with a writer but no Claude Code correctly reports that
  it can execute. (`tests/test_fast_edit_ladder.py`.)
- **Deep execution is now ON BY DEFAULT: a consumer that does nothing gets Claude Code.**
  `RunnerConfig.deep_runner` defaulted to `None`, and nothing anywhere resolved it, so every
  consumer had to know to construct a `SubprocessGoalRunner` itself or lose deep/execution work
  entirely. Worse, `None` meant two incompatible things at once — "I never wired one" and "I
  deliberately want no execution" — which are indistinguishable to the library, so the forgetful
  case got the silent-disable behaviour. That is exactly how the false "Executing: …" report above
  reached a live user: the request routed to deep, and deep had nothing to route to. The field now
  carries the same tri-state `context_assembler` has had all along: leave it unset (the
  `_AUTO_DEEP_RUNNER` sentinel) and `config.resolve_deep_runner` builds a `SubprocessGoalRunner`
  pointed at `claude` on PATH; pass an instance to use your own (`AcpDeepRunner`, a queue worker,
  a test double); pass `None` to disable execution deliberately, which stays disabled and warns
  about nothing. The auto-built runner reads the environment the rest of the repo already documents
  for the deep worker (`QAR_DEEP_WORKING_DIR` falling back to `corpus_root` then the cwd,
  `QAR_CLAUDE_PATH`, with `QAR_DEEP_TIMEOUT_SECONDS` applied inside the runner), so this adds no
  new knobs. If Claude Code is not on PATH the resolution degrades the way everything else in this
  repo degrades: one loud warning naming the binary it looked for, what is now unavailable, and the
  three ways to fix it, then no runner — never a runner that would fail on every spawn, and never
  an exception. `build_orchestrator` resolves and **writes the result back onto the config**, so
  after it runs `cfg.deep_runner` is always a real runner or a real `None`; the chat UIs and
  `derive_capabilities` read that field directly, and the sentinel must never reach them.
  The CLI no longer builds the runner itself — it was duplicating this logic, and its
  `deep_runner = None` for a corpus-less run meant `qar chat` outside a corpus could never execute
  anything. (`tests/test_deep_runner_default.py`; `docs/writing-a-consumer.md`, `docs/adapters.md`.)

### Added
- **QAR can now read the person's own reflections, so "pick something based on my daily reflection"
  is a lookup instead of an apology.** Observed live: asked exactly that, an attended session
  replied that it did not have the reflection in front of it and asked the person to paste it. The
  model was right on both counts — it needed the text and it refused to invent it — but no action
  existed that could go and get it, and `grep -rn reflection quest_ai_runner/` found nothing. Quest
  had been storing the answers all along, in two user-scoped endpoints nothing here called:
  `GET /api/daily-plan/today` (`yesterday_review`, the person's own account of how the previous day
  went, plus what they planned for the day) and `GET /api/period-review/{period}/current`
  (`reflection_past` / `reflection_future` for a week, month, quarter, or year). Both are now
  `QuestClient.get_daily_reflection` / `get_period_reflection`, and `runner/reflections.py`
  composes them into one dated, labeled `ReflectionContext`. It takes today's daily entry or walks
  back a couple of days when today's is not written yet (the morning case, which is precisely when
  a background pass would otherwise look blind at a person who reflects every day), then the first
  requested period with a submitted, non-empty review, falling back to the *previous* period since
  early in a week the newest thing the person wrote is last week's review. Which period matters is
  the caller's call, never assumed. An unwritten reflection is a normal state everywhere in this
  path: no submitted review, a client that predates these methods, or a dead endpoint all degrade
  to an empty context rather than an error.
  (`docs/quest-api-contract.md`; `tests/test_reflections.py`.)
- **Both surfaces that decide what to work on now see it.** The attended chat gets a
  `reflection_context` query kind on `QuestRetrievalAdapter`, advertised to the planner as
  `get_reflection_context` in `list_operations`/`describe_operation` the same way `goal_context` is,
  needing no goal or quest id. When nothing is on record it answers `kind="query"` with a plain
  statement to that effect rather than `kind="error"` — an error reads as "this lookup is broken"
  and pushes the planner straight back into asking the human for text it has just verified does not
  exist. Autopilot reads the reflection once per pass (user-scoped, so caching it per pass rather
  than per quest is one pair of reads instead of N), derives the period order from the quest's own
  scope, and `compose_batch_text` carries it into every batch, framed as the lens to read the goals
  through and with an instruction to say so plainly if it contradicts the plan. `next_steps_from_pass`
  puts one condensed line in the artifact's `note` slot, as context for the list rather than a step
  on it. This is what the batch text was missing: every other input to it is derived from rows the
  system recorded, so a person could write "the writing goal keeps slipping, protect two mornings"
  in Quest and the next pass would compose its brief as if they had said nothing.
  (`tests/test_autopilot_reflection.py`, including the compatibility case: a client without the
  reflection methods composes exactly the batch it composed before.)
- **A deep run can now be steered WHILE it is running, through a second, opt-in `DeepRunner`.**
  `SubprocessGoalRunner` shells out to `claude -p`: one prompt in, one blob out, nothing can reach
  the worker once it has started. So a user message that arrived mid-run could only be folded in at
  the NEXT goal-loop attempt, which is the first moment the orchestrator gets control back — the
  person watching a twenty-minute run go the wrong way had no way to say so. New
  `adapters/acp_deep_runner.py` (`AcpDeepRunner` + `AcpConfig`) runs the SAME contract over the
  Agent Client Protocol instead: a live JSON-RPC session with the Node `claude-agent-acp` agent
  (which wraps Anthropic's Claude Agent SDK), whose steering extension injects a message into the
  turn currently in flight, pre-empting the generation and slotting in between a multi-step turn's
  tool calls. Both routes in are wired to machinery QAR already has: an `InputInbox` (`core/inbox.py`
  — the same inbox the orchestrator drains between attempts) polled while the turn is live, and a
  public `steer()` any thread can call. A message that arrives after the turn settled is pushed BACK
  to the queue for the next attempt rather than dropped, and the channel latches closed so a
  returned message is never re-offered on the next poll tick. This is **purely additive**:
  `SubprocessGoalRunner` is untouched and remains the default, both satisfy `DeepRunner` with the
  same signature, and selecting the other one is just `RunnerConfig.deep_runner`.
  (`docs/acp-deep-runner.md`; `tests/test_acp_deep_runner.py`.)
- **That runner reuses QAR's existing vocabularies rather than inventing parallel ones.** The ACP
  `session/update` stream is translated into the `EVENT_EXEC` ticks a `claude -p` deep run already
  emits — same event type, same `run_id`, same one-line texture (`$ pytest -q`, `Read: docs/x.md`,
  `[thinking] …`) — so every existing consumer renders it unchanged. One trap is pinned by a test:
  a tool finishing must NOT use the phase strings `core/guard.py` treats as terminal, or one
  completed `Read` would mark the whole subgoal succeeded; tool lifecycle uses
  `tool_result`/`tool_error` and only the run's own final tick is terminal. Permission requests are
  answered from the SAME config surface the subprocess runner uses (`disallowed_tools` beats
  auto-approval, a pinned `allowed_tools` fails CLOSED on a tool the payload cannot identify,
  `skip_permissions` auto-approves), and when a human is genuinely needed the ask becomes a real
  `EscalationSink` decision-request and the run returns `DeepResult(met=False, decision_id=…)` —
  the same `needs_you` contract the `QAR-ESCALATED:` marker gives today, which still works too. The
  tool being asked about is read from the structured `_meta.claudeCode.toolName` field, never from
  the agent-composed title (hard rule #3).
- **The Node the ACP agent runs under is config, and a too-old one fails loudly before anything
  spawns.** `claude-agent-acp` requires Node >= 22 and a box's ambient `node` frequently is not
  (and often cannot be upgraded, because other tooling depends on it). So the binary is resolved
  from `AcpConfig.node_path`, then `QAR_ACP_NODE_PATH`, then `PATH`, probed with `node --version`
  up front, and a version below the floor returns a `DeepResult` naming the version found, where it
  was found, and the knob to set — instead of dying inside the child with an engine warning. An npm
  bin shim is resolved through its symlink to the `.js` entry and launched as `<node> <entry>`,
  because the shim's shebang would otherwise pick up whichever `node` is first on `PATH`. The agent
  program resolves the same way (`AcpConfig.agent_command`, then `QAR_ACP_AGENT_COMMAND`, then
  `PATH`). Session lifecycle is deliberately one process + one session per `run_goal` call, exactly
  the lifetime a `claude -p` spawn gets: the goal loop never signals "this subgoal is finished", so
  a longer-lived session would have no defined moment to close and would leak a Node process per
  retry.
- **`agent-client-protocol` as a new optional `[acp]` extra.** Imported lazily inside the single
  connection seam, so importing `quest_ai_runner.adapters` never requires it and a deployment that
  does not use ACP pays nothing. It is not part of `[all]`, since the other half of this integration
  is an npm package pip cannot install. `tests/test_acp_deep_runner.py` runs fully offline against a
  scripted fake connection — no package import, no process, no auth, no network — and covers the
  interface contract, the event translation, the permission mapping, a mid-run steering injection
  actually reaching the session, and graceful degradation on a missing package, a missing binary, a
  too-old Node, a failed handshake, and a blown timeout.
- **The sufficiency gate now has a STRUCTURAL half, not just prompt text.** `SUFFICIENCY_GATE` tells
  the planner to issue another "read" when it has not READ the material it is about to answer from,
  and nothing enforced it. A real turn proved the cost: the assembled context surfaced a card whose
  content was a short synthesized SUMMARY of a note whose full text was live fetchable, and the
  planner answered at step 0 anyway, spending the reply telling the user it had "only the note
  header" and asking whether it should go and get the rest. Two things were wrong and only one was
  the model's: a summary and full content rendered IDENTICALLY, so it had no signal, and no check
  ever looked at the plan it returned. New `core/sufficiency.py` closes both, keyed entirely on
  structured data (an item's own declared fetch spec versus the read specs that actually ran), never
  on words in the model's output. A content item whose stored text is abridged declares it on its
  locator and names the read spec that fetches the real source (`{"text": "...", "full_ref":
  {"query": {...}}}`); the rendered item then carries an explicit `[abridged: N chars of SUMMARY ...]`
  marker naming that fetch, the context view gains an `ABRIDGED CONTEXT ITEMS` notice, and a plan
  that would terminate in "answer" while one of those fetches has not run this turn becomes ONE
  "read" step that runs it, after which the loop re-plans with the full text in GATHERED. Fires at
  most once per turn, and is INERT for any item that declares nothing (no notice, no gate,
  byte-identical prompts), so no existing deployment changes until its cards carry `full_ref`. The
  card updater is now instructed to attach one whenever it writes a note that only summarizes
  something still fetchable. Governed by `OrchestratorConfig.full_read_before_answer` (default on).
  (`core/sufficiency.py`, `core/orchestrator.py`, `adapters/reference_resolver.py::NoteResolver`,
  `docs/context-assembly.md`; `tests/test_sufficiency_gate.py`.)
- **The provider round trips behind a reported "1 step" are now pinned by a test.** A turn the UI
  called "1 step" took 83 seconds against fully prebuilt context, and the step count explained none
  of it: "steps" counts PLANNER LOOP iterations, while the simplest possible turn makes FOUR
  sequential model calls, all on the critical path. In order: the goal-condition derivation (cheap
  tier), the planner (planner tier), the answer (answer tier), and goal verification (`verify_tier`,
  the STRONGEST model, over the same context the answer saw and only after the answer is already
  written). No call is duplicated, neither the planner nor the answer sends its context twice
  (`_plan` and `_grounded_answer` each build both a flattened and a layered form, and only one
  reaches the provider), and provider retries fire only on transient 429/503/timeout errors. The
  time is four real generations. `tests/test_turn_call_budget.py` locks the count, the
  no-double-payload property, and the verifier's single tier fallback, so the budget cannot grow
  silently again.
- **A quest-linked folder now has ONE canonical "what to do next", instead of every reader
  reconstructing its own.** A person working in a quest's folder had no artifact that answered
  "what is next here": an attended session rebuilt the answer turn by turn from whatever context
  cards happened to surface, and the autopilot pass reasoned out its own answer independently, so
  the background view and the human's view could drift apart with nothing to notice it. There is
  now a third managed section in `QUEST_SYNC.md`, `<!-- QAR:MANAGED:next_steps -->`, holding the
  current recommended next action(s). It is a REPLACE, never a log: each refresh regenerates the
  block in place, so the folder always carries exactly one current answer, and re-publishing the
  same conclusion leaves the file byte-identical. Human-owned content elsewhere in the file is
  untouched, as with the existing managed blocks, and the block's own carry-over bullets sit inside
  the markers so the "Notes to push to Quest" parser never mistakes them for notes to post.
  (`runner/quest_folder_sync.py`: `NextSteps`, `render_next_steps`, `write_next_steps`,
  `read_next_steps`, `publish_next_steps`.)
- **That artifact syncs to Quest as an UPSERT, not a new note per refresh.** Quest's notes API is
  add + list only (there is no PATCH or DELETE on a note), so a refreshing artifact cannot live in
  a note without leaving a year of near-identical entries behind it. Quest **context entries** can
  be replaced in place, so the artifact is published as a single context entry matched by its fixed
  name (`NEXT_STEPS_ENTRY_NAME`), created once and PUT over on every later refresh. If the entry
  LISTING fails, nothing is written to Quest that round rather than blind-creating a duplicate; the
  local file is still correct and the next refresh retries. A client with no context-entry support
  at all falls back to appending a `[next-steps]`-marked note, and says in its result that this
  path accumulates. (`runner/quest_client.py`: `list_context_entries`, `create_context_entry`,
  `update_context_entry`.)
- **The autopilot pass both reads and writes it, so the two views cannot diverge.** For a quest with
  a mapped folder (`RunnerConfig.quest_folder_map`, now passed through to `AutopilotPass`), each
  pass reads the standing artifact into the batch text as the plan of record (an attended session
  may have refreshed it more recently than any pass), and then writes its own conclusion back over
  it, locally and on Quest. The written conclusion is deterministic, derived from the goals the pass
  already selected plus the recurring tasks it adopted and the previous period's unfinished goals:
  the pass has already decided what to work on, and asking a model to re-derive it here would spend
  a call to produce a different answer from the one it just acted on. Only a pass that actually
  produced work refreshes the artifact, and a dry run never touches it: overwriting a considered
  answer with "nothing eligible today" on a day the quest is gated or quiet would make the artifact
  less trustworthy than the guesswork it replaces. A Quest-side failure is reported in the pass
  summary as a bookkeeping warning and never fails the pass. (`runner/autopilot.py`:
  `next_steps_from_pass`, `AutopilotPass._read_next_steps` / `_refresh_next_steps`,
  `AutopilotResult.next_steps_refreshed`; `tests/test_autopilot.py`,
  `tests/test_quest_folder_sync.py`.)
- **`FileContextStore.bootstrap()` now also reuses an already-bootstrapped ANCESTOR corpus, the
  mirror image of nested (descendant) reuse.** Nested reuse only ever looked DOWN into a corpus
  root's descendants, so a narrower root (e.g. bootstrapping a subfolder several levels below an
  already-indexed `~/hq`) could never see the wider corpus's index, since an ancestor is by
  definition not inside `walk_root` — it re-ran full LLM topic discovery on content the wider
  corpus had almost certainly already indexed. Bootstrap now also walks UP through parent
  directories (bounded to 12 levels, stopping at the filesystem root either way) looking for the
  nearest ancestor with its own completed `.quest-context/bootstrap_meta.json`. When found, its
  cards are imported the same way as the descendant case, but FILTERED to the files that actually
  fall under this narrower root: a card with no in-scope file is skipped, and a card with some
  in-scope and some out-of-scope files is imported with its `files` list trimmed to just the
  in-scope subset, so an ancestor card's out-of-scope files are never mistaken for "covered" here.
  Same id-namespacing and `imported_from` provenance convention as the descendant case (here the
  ancestor's path relative to `walk_root`, e.g. `".."` or `"../.."`), so orphaned ancestor imports
  are pruned by the same existing logic when the ancestor stops offering them. Both directions can
  contribute in the same bootstrap (an indexed ancestor above and an indexed descendant below).
  Governed by the same `reuse_nested_cards`/`QAR_REUSE_NESTED_CARDS` flag as the descendant case;
  no new flag. (`adapters/file_context_store.py`: `_discover_ancestor_card_dir`,
  `_import_ancestor_cards`, wired into `_bootstrap_inner` alongside the existing descendant reuse;
  `tests/test_context_assembler.py::TestAncestorCardReuse`.)

### Fixed
- **A turn that executed nothing reported itself as work in progress.** Confirmed live: a user
  asked for a documentation file to be updated, and the final answer bubble of the turn read
  `Executing: CLAUDE.md's Current situation reflects that committee follow-up is paused until the
  in-person intensive with Dr. Mitchell.` Nothing had run. No deep executor was configured, no file
  was touched, and the only signal was a dim side note above it reading "(No deep executor
  configured; cannot auto-execute)" — easy to miss under a sentence that reads like a completion
  report. Three defects lined up to produce it, and all three are fixed at their own level rather
  than papered over in the renderer. **(1)** The orchestrator announced `Executing: <goal>` the
  instant the planner chose to execute, BEFORE checking whether anything could execute; it now
  fires only when `_has_deep_execution_capability()` holds, which is the same gate `_run_deep`
  itself applies one line later. **(2)** That announcement was typed `EVENT_RESULT`, the type that
  carries a turn's actual outcome. Both chat UIs fall back to the last result text when a deep turn
  flushes no output of its own (a deliberate mechanism, added for the 2026-07-26 duplicate-output
  fix), so an interim "I am starting" sentence came straight back out as the turn's answer. It is
  now its own type, `EVENT_INTENT`: an announcement of intent, in `SURFACING_EVENTS` so every sink
  still sees it, and documented as never usable as a turn's outcome. Both UIs render it as a
  progress line and keep it out of the answer-fallback pot. **(3)** `_run_deep`'s no-executor
  return carried `text=None`, which is why the UIs had nothing honest to show in the first place;
  it now carries `NO_DEEP_EXECUTOR_TEXT`, which states plainly that nothing ran, that no files were
  changed, and what to configure. The same wording therefore reaches every consumer, not just the
  one UI that had a side note for it. Both UIs also stopped recording `Attempted: <goal>` in the
  session history for a turn with no deep results at all — nothing was attempted, and that line is
  read back as context by the next turn.
  (`tests/test_no_deep_executor_honesty.py`.)
- **Autopilot tasks were titled after their persona and named it by raw id.** With no explicit
  `title`, the server derives one from the first line of the text, which is the "Act as ..." line,
  so every autopilot task in the list was named after its persona instead of its work. And with no
  name lookup, that line read "Act as rep_09d389aeb9ff" and then restated the same id twice more.
  Batches now carry a title taken from their goals, and the persona is named ONCE, by display name
  (resolved via `get_ai_profile`, cached per pass, degrading to the id if the lookup fails). The
  id remains authoritative in `assignee_rep_id`.
- **A period's target read as one run's workload.** A weekly goal handed to a daily run said "do
  this", which is both discouraging and wrong. The batch now states that what follows is the
  PERIOD's target, asks the run to advance it as far as one session honestly can, and to say what
  remains. The Definition-of-Done line also uses the goal's own `criteria` when it has any,
  instead of restating the brief generically.
- **A human-only day could shadow the week that held all the AI work.** `select_target_goals`
  stopped at the finest CURRENT period group even when the `ai_help` filter left it empty, so a
  quest with a human-only goal dated today and a weekly goal holding that week's actual AI work
  reported nothing to do, on exactly the days the user had also planned something for themselves.
  The search now continues to coarser CURRENT scopes. It still does NOT fall through past them to
  the unscoped next-goal fallback: planning a period and leaving no AI-enabled goal in it is a
  decision, and grabbing unrelated future work would override it.
- **A "daily" cadence silently became every-other-day.** `cadence_due` compared ELAPSED time
  (24h/7d/30d) while the pass task fires at a fixed wall-clock time, so one late pass put the next
  morning's pass inside the window and skipped it entirely. Found live: a first pass ran at 16:11
  and the following 06:00 pass was gated out. Cadence is now compared as CALENDAR periods, which
  is also what the words mean: daily = not yet today, weekly = not yet this ISO week, monthly =
  not yet this calendar month. Week and month comparisons use the (year, week) and (year, month)
  pairs, never the bare number, so December does not read as later than the following January.
- **Autopilot did nothing at all, because nobody ever created the task that does the work.**
  Autopilot is implemented as a recurring assistant task (`task_kind: "autopilot"`) that the
  executor routes to `AutopilotPass` — the design's deliberate choice, so the autonomy is visible,
  pausable and auditable like any other task. But creating that task was left to "a consumer", and
  no consumer ever did. Switching a quest to Suggest/Act saved the setting correctly and then
  produced silence forever, with no error anywhere to explain why. `Poller._ensure_autopilot_pass`
  now closes the loop from the runner side: any scan that finds an opted-in quest with no OPEN
  pass task creates one (daily, at `RunnerConfig.autopilot_pass_time`). The steady state costs one
  list call per scan — the per-quest opt-in read only happens when no pass task was found. Disable
  with `autopilot_ensure_pass_task=False` where something else owns that lifecycle.
- **Every autopilot work batch was created against the wrong id, so none of them could be
  created.** A task's `goal_id` field holds a QUEST id (the API resolves it with `get_quest` and
  404s anything else), but `_create_batch_task` passed the first target goal's own id from
  `list_quest_goals` — a different document with a different id. Work batches now link to the
  quest; which goals a batch covers is carried in its text, where `compose_batch_text` already put
  it. This also repairs the backpressure gate, which looks tasks up by quest id and so could never
  have seen autopilot's own output.
- **A suggestion could execute before the human ever saw it.** Autopilot created every task
  `queued` and PATCHed it down to `suggested` afterwards, because the create route had no `status`
  field when that code was written. Between those two calls the runner's poll could claim and run
  the task — precisely the approval that suggest mode exists to require. The status is now
  asserted at creation, so the window does not exist.
- **Internal per-stage bootstrap/scan logs (`context index: stage N — ...`, `BM25 context index:
  building for the first time`, etc.) cluttered the chat transcript.** A June fix
  (`InteractiveSession.__init__` raising `quest-ai-runner.context`'s logger level to WARNING before
  `build_orchestrator()`) was removed in August when the ANSI terminal moved to a panel-aware log
  handler for a DIFFERENT problem (cursor corruption, not noise), and Textual never had an
  equivalent — `on_mount`'s `_RichLogHandler` routes every propagated record straight into the
  visible scrollback. Restored as `_suppress_background_bootstrap_logs()`, shared by both UIs via
  `InteractiveSession.__init__` (now covers `bm25_content_store`'s logger too, not just the context
  one), gated off when the caller passes `-v`/`-vv` so an explicit verbose request still sees them.
  The user-facing bootstrap summary (`notify()`/`_tell()` in `config.py`) is unaffected either way.
  (`interactive.py`, `cli.py`, `textual_ui.py`, `textual_session.py`;
  `tests/test_bootstrap_log_suppression.py`.)
- **The prompt input box could be pushed off-screen by tall panels above it.** No widget in
  `QuestAITerminal`'s Textual layout was docked, so when the context/deep/deep-detail/future-context
  panels grew (cards shown, an expanded deep-run detail), the whole stack could exceed the viewport
  and shove the input box down, forcing the user to scroll the whole screen to find it. `#activity`
  and `#prompt` are now wrapped in one `dock: bottom` container (`#bottom-bar`) so they stay pinned
  to the bottom regardless of how tall the panels above grow, with `#transcript`'s `height: 1fr`
  filling the remaining space. A single docked wrapper, not two independently-docked siblings: two
  separate `dock: bottom` widgets alongside Textual's own bottom-docked `Footer` landed on the SAME
  row as the footer instead of stacking above it (`Footer`'s own space reservation only correctly
  accounted for one additional bottom-docked widget). (`textual_ui.py`;
  `tests/test_prompt_docked_bottom.py`.)
- **A session always started as generic "AI: Assistant" even when the corpus's own CLAUDE.md
  clearly designated a specific named persona as the intended owner of the work**, so the model
  would only reveal the right persona mid-answer, in prose, with the transcript header still saying
  otherwise. `InteractiveSession` now auto-resolves a persona when the caller supplied NEITHER
  `--rep`/`QAR_REP_NAME` NOR `--persona-file`/`QAR_REP_PERSONA_FILE` (a `rep_specified` /
  `persona_specified` pair threaded from `cli.py`, not a string match against the literal
  `"Assistant"` default, so `--rep Assistant` still counts as explicit): if `cfg.corpus_root` has
  its own top-level `CLAUDE.md`, one "fast"-tier LLM call (via `MultiProvider`/`resolve_tier`, after
  `build_orchestrator()`) asks whether it designates a named persona and, if so, a relative path to
  a fuller persona file (read with the same containment check as `FilesAdapter`). No corpus root, no
  CLAUDE.md, no provider, a timeout (12s), or unparseable output all fall back to today's exact
  behavior, never raising and never blocking startup more than a few seconds. Domain-free: the
  mechanism only reads whatever CLAUDE.md the consumer's own corpus happens to contain and asks a
  generic question about it. (`interactive.py`: `_resolve_persona_from_corpus`,
  `_read_persona_file_in_corpus`; `cli.py`, `textual_session.py`, `textual_ui.py` thread the new
  flags through; `tests/test_persona_resolution.py`, `tests/test_cli_persona_explicit_flag.py`.)
- **`GuidanceProvider.select()` could hang an entire turn indefinitely, UI-agnostic.** Unlike the
  concurrent context-assembly fetch right above it (bounded by
  `context_assembly_timeout_seconds()`), the turn-start guidance pre-selection called a
  caller-supplied `select()` directly and synchronously with no timeout of its own — a
  `dynamic_guidance_loader` hitting a stuck DB/network call, or a provider's own LLM filtering pass
  inside `select()`, blocked the whole orchestrator turn loop in the SAME "Searching context…"
  status the (actually-protected) context fetch already showed, indistinguishable from that fetch
  stalling. Matches a live report of a session stuck on "Searching context…" with no further
  progress. Now runs in a bounded `ThreadPoolExecutor` collected with the new
  `guidance_selection_timeout_seconds()` (env `QAR_GUIDANCE_SELECTION_TIMEOUT_SECONDS`, default
  5.0s); a timeout degrades to "no guidance this turn," matching the existing behavior for a
  `select()` that raises. (`core/orchestrator.py`; `tests/test_guidance_selection_timeout.py`.)
- **A "Sources:" header could print with nothing under it.** The Textual terminal's `EVENT_CONTEXT`
  rendering gated the header on the outer `sources` list being non-empty, but gated each line
  separately on that source's own `items` — a source with no file-level items (e.g. a recent/card
  match) left a dangling header with an unrelated narration beat landing right after it, reading as
  broken/missing content. The per-source lines are now built first and the header is only written
  when at least one line has content. (`textual_ui.py`; `tests/test_context_sources_header.py`.)

### Added
- **Autopilot can ADOPT a quest's own recurring tasks** (opt-in per quest via
  `autopilot.adopt_recurring`, off by default). When on, a pass folds that quest's due recurring
  occurrences into the persona batch it is already creating and closes the originals, pointing each
  at the batch that took it over. Without this, a quest with both autopilot and a daily recurring
  task gets two unrelated deep runs a day that cannot see each other's work. Autopilot's own tasks
  (the pass and its work batches) are never adopted: folding the pass into its own batch and
  closing it would kill the series that drives autopilot at all. Adoption happens only AFTER the
  batch was created successfully, so a failure duplicates work rather than losing it, and a failed
  close is reported rather than passing silently.
- **Batch tasks now carry their period's context and the previous period's outcome.**
  `compose_batch_text` states which scope the run owns, lists the goals and adopted AI tasks in it,
  and summarizes what the previous period actually produced: goals completed, goals left
  incomplete, and finished task results. A daily pass that cannot see yesterday has no way to
  notice the plan slipped, so it reissues the same instruction while the human falls further
  behind. When the previous period produced nothing, the text says so explicitly rather than
  omitting the section, since silence and "no information" read identically otherwise. New pure
  helpers `previous_period_key`, `previous_period_bounds` and `select_period_goals`.
- `QuestClient.create_task` gained the fields the Quest API already accepted but the client could
  not send: `status` (queued/suggested, asserted atomically at creation), `recurrence` (free text
  or a structured `{frequency, days?, time?, interval_days?}`), `scheduled_date`/`scheduled_time`,
  and `assignee_rep_id` — which lets autopilot carry the resolved persona STRUCTURALLY instead of
  only naming it in prose for a consumer to parse back out.
- **`QuestClient.create_goal` + `quest-ai-runner create-goal` CLI subcommand: create a real, typed
  Goal, not just an assistant task.** Until now the client had no goal-creation endpoint at all
  (`runner/autopilot.py`'s `_maybe_create_goal` degraded to a no-op, waiting for one to exist).
  `create_goal` POSTs to `/api/planning/goals` with a required `period` (validated client-side
  against the five accepted formats — day/week/month/quarter/year — so a typo fails fast instead
  of round-tripping to a 400) and an optional `quest_id` (goals attach to a quest's shared plan
  when given, or the caller's own standalone account when omitted). Raises on failure rather than
  swallowing it, same contract as `create_task`. See `docs/quest-api-contract.md`.
- **Personal lexicon: rank the terms ONE person actually uses (their distinctive vocabulary), by
  TF-DF-IDF over their own documents, with no model call at all.**
  `adapters/personal_lexicon.py` scores every candidate term (word n-grams that neither start nor
  end on a function word, so "law of attraction" survives whole and "of attraction" is never a
  term) as `(1 + ln TF) * (1 + ln DF) * IDF`. Both counts are log-damped deliberately: undamped,
  one long entry or one much-repeated ordinary word outranks the rare term that is the entire
  point, so damping leaves IDF as the factor that decides the ranking and volume only breaks ties.
  IDF comes from a `BackgroundFrequency`, and the two implementations answer the same question from
  different directions: `LanguageBaseline` reads the bundled three-band English frequency list in
  the new `adapters/english_word_bands.py` (a most-common-band word gets IDF 0, which collapses the
  score, so ordinary English drops out with no stopword pass), while `CorpusBackground` asks whether
  everyone else in the deployment writes the term too and abstains below `min_population` (25),
  where the counts are too thin to mean anything. `CombinedBackground` takes the LOWEST opinion any
  source will give, because each source is authoritative about its own disqualification and ignorant
  of the other's: general English cannot know a deployment's house words, and a young population
  cannot know English. An abstention is skipped, never read as agreement on zero.
  `min_documents` defaults to 2 for a safety property, not tidiness: where this output feeds back
  into its own input (a lexicon handed to the speech recognizer that dictated the documents), a
  one-off mis-recognition is a plausible-looking rare term, and promoting it teaches the recognizer
  to produce the same error again. Corroboration from a second, independent document is what breaks
  that loop. Also included: the person's own dominant capitalization is what comes back (the casing
  IS part of a spelling hint), a fully deterministic order including equal scores,
  `drop_subsumed_terms` so a hard budget is not spent on "law", "attraction" AND "law of
  attraction", and `terms_within_budget` for packing a ranked list into a downstream API's caps.
  (`adapters/personal_lexicon.py`, `adapters/english_word_bands.py`; `docs/personal-lexicon.md`;
  `tests/test_personal_lexicon.py`.)
- **"Explain how I got this" (opt-in, off by default): a USER-FACING account of how a turn reached
  its answer, emitted as a new `EVENT_EXPLANATION` after the result and before `done`.**
  `core/answer_explanation.py` holds the whole feature: a `TurnTrace` assembled from data the loop
  already produced (observations, execution record, projected cards and sources, goal verdict,
  exit reason), a model-free `is_eligible` predicate, the `EXPLAIN_TOOL` forced-JSON schema, and
  the payload builder. `Orchestrator.write_answer_explanation` makes ONE cheap-tier call
  (`explain_tier`, default `"fast"`) constrained to that record.
  Three properties are deliberate and covered by tests:
  (1) **Ordering.** The call runs AFTER the terminal `EVENT_RESULT`, so the answer has already
  reached the consumer and the reader waits for nothing. Folding it into the answer call instead
  would delay the answer by its whole generation time (the answer is one blocking call, not a
  token stream) and would fight `REPLY_VOICE_SYSTEM`, which forbids the reply from carrying
  exactly this material.
  (2) **The gate is model-free.** An ineligible turn (nothing read, nothing executed, no assembled
  context, answered at the first step: i.e. plain small talk) emits nothing and costs nothing, so
  a consumer's toggle simply does not appear on it.
  (3) **Half the payload is a record, not prose.** `used` (cards, sources, reads, actions, web) and
  `signals` (exit reason, goal verdict, claim-honesty flag, steps) are assembled from the trace, so
  a failed or unusable generation call costs the written sections and nothing else. The written
  half is constrained by prompt to what the execution record shows.
  This is NOT the internal narration channel: it carries wording composed for the reader, never the
  run's own internal wording relayed. `OrchestratorResult.explanation` carries the same payload for
  a non-streaming consumer. Enable with `OrchestratorConfig.explain_answer` or env
  `QAR_EXPLAIN_ANSWER=1` (`QAR_EXPLAIN_TIER` overrides the tier).
  (`core/answer_explanation.py`, `core/adapters.py`, `core/orchestrator.py`, `cli.py`;
  `tests/test_answer_explanation.py`.)
- **The Textual terminal UI (`qar`/`qc`) now shows a one-line "Explain how I got this" summary**
  when the feature is on and a turn produced a payload: source/read/action counts pulled from the
  trace-recorded `used` section, in the same dim style as the existing `[Alt+C] Context it used`
  hint. The terminal has no expandable surface to put the six-section prose account behind (that is
  the chat surfaces' job), so this is deliberately just the record, not the model-written sections.
  (`textual_ui.py`, `_finish_turn`.)
- **`FileContextStore.bootstrap()` reuses a nested corpus's own already-bootstrapped cards
  instead of re-discovering them via the LLM.** When a directory under the walked corpus already
  has its own completed bootstrap (a `.quest-context/bootstrap_meta.json`), its cards are
  imported wholesale: file paths are rewritten to be relative to the wider corpus root, the card
  id is namespaced by the nested root's path (so ids from unrelated sub-corpora never collide),
  and the covered files are excluded from LLM topic discovery entirely. This is pure filesystem
  reuse (zero LLM calls) and works even with no `provider` wired at all. A card previously
  imported from a nested store that stops offering it (renamed/deleted/de-bootstrapped on the
  nested side) is pruned on the next bootstrap so imports never drift from their source. A nested
  store is trusted transitively: once a directory is identified as a completed nested root, its
  own subdirectories are not separately scanned for further nested stores, so multi-level corpora
  (e.g. a wide `~/hq`-rooted instance containing a narrower `product/`-rooted instance) reuse
  cleanly without double-importing the same content under two namespaces. On by default; opt out
  per store via the `reuse_nested_cards=False` constructor arg or env `QAR_REUSE_NESTED_CARDS=0`.
  (`adapters/file_context_store.py`: `_discover_nested_card_dirs`, `_import_nested_cards`, wired
  into `_bootstrap_inner`; `tests/test_context_assembler.py::TestNestedCardReuse`.)
- **Anticipation engine (opt-in, off by default): the assistant learns recurring ask patterns
  (time-of-day/day-of-week + keyword profile per scope), predicts the likely next ask, and
  precomputes its context BEFORE the user asks.** `core/anticipation.py` is the shared learning
  core: pure functions (`extract_features`, `similarity`, `score_outcome`, `update_weight`,
  `reinforce_or_create`, `rank_patterns`, `generate_predictions`, `match_actual`) plus a
  runner-lane wrapper (`FilePredictionStore`, one JSON file per scope key under
  `.quest-context/predictions/`, and `Anticipator`). Every turn, when enabled: at TURN START
  the actual message is scored against the predictions planned after the previous turn (the
  objective function — keyword similarity between predicted and actual text — is logged for
  EVERY prediction, hit or miss, to `predictions/prediction_log.jsonl`, so hit rate/mean score
  are measurable offline); a match at/above `MATCH_SERVE` seeds the turn with that prediction's
  PRECOMPUTED context as a cheap, discardable `--- ANTICIPATED CONTEXT` hint (the normal fresh
  assembly still runs and leads). At TURN END, a background daemon thread learns this turn's ask
  pattern (an online EMA weight update via `update_weight`, `w <- w + ALPHA * (score - w)`, so a
  pattern that keeps matching drifts toward 1.0 and one that keeps missing decays toward 0.0 and
  is eventually pruned) and plans + precomputes the next turn's predictions, never blocking the
  answer. Pattern-based only: NO LLM calls anywhere in this engine. Wired into
  `OrchestratorConfig.anticipation_enabled` (default `False` — with it off, or no `Anticipator`
  wired, a run is byte-for-byte identical: zero calls, zero threads, zero store files touched)
  and `config.resolve_anticipator` (built over the same `context_cards_dir` the card/recent-context
  stores use); opt in via env `QAR_ANTICIPATION=1`. Every touch point is guarded so a failure
  degrades to the normal path — anticipation can only ever save work, never break a turn. A
  consumer with its own storage (e.g. an async database) reuses the pure functions directly
  instead of porting a duplicate. (`quest_ai_runner/core/anticipation.py`, wired in
  `core/orchestrator.py`, `config.py`, `cli.py`; `docs/anticipation.md`;
  `tests/test_anticipation.py`.)

### Changed
- **Fast lane task-eligibility wire flag renamed `interactive` → `real_time` (client/server
  contract change only).** `QuestClient.list_interactive_due` and `.wait_for_interactive` now send
  the query param as `real_time=true` instead of `interactive=true`, matching the backend's
  corresponding rename of the assistant-task document field so any task-creation path (not just
  one hardcoded feature) can flag itself as low-latency-eligible. Pure rename: no change to the
  fast lane's control-flow, which was already gated on the boolean flag rather than on task type;
  the separate, still-unchanged concern is `_handle_one`'s `context_request`-payload check, which
  decides HOW a task executes (fast structured context assembly vs. the full goal/plan/execute
  loop), not whether the fast lane serves it. Method names (`list_interactive_due`,
  `wait_for_interactive`) are unchanged since they still describe the methods' behavior; only the
  wire param and surrounding docs/comments changed. (`runner/quest_client.py`, `runner/poller.py`,
  `config.py`; `tests/test_context_request_fast_lane.py`, `tests/test_runner.py`.)
- **`core/anticipation`: the stored-record rebuilders are now public API.** `_pattern_from_dict`
  and `_prediction_from_dict` are renamed to `pattern_from_dict` and `prediction_from_dict`. A
  consumer with its own storage (quest-backend's Mongo-backed twin of `FilePredictionStore`) was
  importing them by their underscore-private names, which is exactly the field mapping the design
  contract says to REUSE rather than port, so they should never have been private. The old
  underscore names remain as aliases for ONE release and will be removed after the next release.
- **Anticipation v2: patterns are the durable store, chips are recomputed read-time from the
  CURRENT moment instead of depending on a 30-minute-TTL live prediction.** Previously a chip only
  showed while a `plan_next` prediction from the *previous* turn was still unexpired
  (`PREDICTION_TTL_SECONDS` was 30 minutes and directly gated visibility), so a pattern learned
  days or weeks ago never resurfaced until the user happened to ask again inside that window. Now
  `chips_for_now(patterns, now, recent_texts)` (and `Anticipator.chips_for_now`) ranks the
  DURABLE, never-expiring patterns against the current time signature on every read, so a pattern
  learned days ago at this hour/weekday surfaces again with zero replanning; `PREDICTION_TTL_SECONDS`
  (now 4 hours) only bounds how long a stored prediction's PRECOMPUTED bundle stays trusted, never
  whether a chip shows. Each chip carries a stable id (`chip_id(scope, text)`); a tap can pass it
  back as `Anticipator.observe(..., anticipated_id=...)` for an EXACT-ID serve of that slot's
  precomputed bundle, bypassing keyword matching entirely (needed once display text can diverge
  from canonical text, see below). Added an OPTIONAL one-LLM-call-per-turn refresh
  (`Anticipator.refresh`, off unless a `refiner` is wired): refines each candidate's raw
  `canonical_text` into a natural `display_text` for the chip (the canonical text itself is never
  rewritten — it stays the scoring/linkage key), drops predictions the conversation just
  obsoleted, and adds up to `MAX_FOLLOWUPS` (2) new conversational follow-up predictions. Gated by
  a new `OrchestratorConfig.anticipation_llm_enabled` / env `QAR_ANTICIPATION_LLM` (default
  `False` — the engine stays fully model-free by default; when on, `resolve_anticipator` wires a
  refiner from `cfg.model_provider` at the `"balanced"` tier). `apply_refresh` and
  `parse_refresh_response` are pure and shared so a consumer with a centralized prompt store
  (e.g. quest-backend) can supply its own prompt and still reuse them. (`core/anticipation.py`,
  `core/orchestrator.py`, `config.py`, `cli.py`; `docs/anticipation.md`; `tests/test_anticipation.py`.)
- **`core/card_filter.py` cuts LLM calls in the card/file selection path (task #2462 latency
  reduction).** Within-card file ranking (`filter_cards_by_relevance`'s stage 2) is now ONE
  batched LLM call ranking files across ALL selected cards (bounded by `_RANK_MAX_CARDS` = 24
  cards and `_RANK_MAX_FILES` = 40 files/card shown in the prompt), replacing the old
  one-LLM-call-per-relevant-card loop (up to 8 parallel calls via `ThreadPoolExecutor`, each
  silently defaulting to `model=None` instead of the caller-resolved cheap tier); the batched call
  still returns the top 5 files per card, the same contract the per-card loop had, and a malformed
  or missing per-card entry falls back to that card's original file order. Both LLM selection
  entry points (`filter_cards_by_relevance`, `consolidate_context`) also gained a bounded,
  PER-PROVIDER LRU selection memo (`clear_selection_memo()`): a repeat ask whose candidate cards
  (ids + content) and task text are unchanged skips the LLM call(s) entirely and returns a
  copy of the prior verdict. The memo key hashes every input that can change the verdict (candidate
  signatures, the task signature, resolved model id, usage hint), so any change misses the cache; a
  fallback verdict (no provider, or a stage-1/consolidation call or parse failure) is never
  memoized, so a transient failure is retried rather than pinned. The task signature
  (`_task_signature`) is ORDER-SENSITIVE: it is the lowercased alphanumeric token sequence as
  written, nothing sorted and nothing deduped. An earlier revision of this same unreleased change
  used a sorted, deduped keyword SET, which made "move goal A under quest B" and "move quest B
  under goal A" share a key, so the second ask silently received the first ask's verdict; a memo
  is a pure speed optimization, so a needless miss costs one LLM call while a wrong hit feeds the
  wrong context into the answer. The one remaining collision boundary is documented and tested:
  asks that differ only in casing, punctuation or whitespace normalize to the same signature and
  do share an entry. Provider identity is handled by
  a `WeakKeyDictionary` keyed on the provider instance itself (not `id(provider)`, which is unsound
  since ids are reused after garbage collection): a different provider instance never shares a
  verdict, and a provider's entries die with it. (`core/card_filter.py`;
  `tests/test_card_filter_selection.py`.)
- **`VectorContextAssembler` can delegate its in-arm LLM hit review to the hybrid's downstream
  consolidating pass, so a hit's relevance is judged by exactly one model call per turn instead of
  two.** New `VectorContextAssembler(..., llm_review=True)` constructor flag (default preserves the
  prior in-arm review); `resolve_context_assembler` wires it `False` for the vector arm feeding
  `HybridContextAssembler`, since that hybrid already runs ONE holistic `consolidate_context` pass
  over the merged card set. `HybridContextAssembler`'s consolidation gate widens to also fire when
  the vector arm delegated its review and actually contributed cards (`llm_review_delegated`
  property), so those hits still get judged exactly once; on the no-provider / budget-exceeded /
  any-failure paths, delegated hits fall back to serving unreviewed (still confidence-gated on raw
  similarity and capped at `max_in_view`), the same keep-all degradation consolidation itself falls
  back to. (`adapters/vector_context_assembler.py`, `adapters/hybrid_context_assembler.py`,
  `config.py`.)

### Fixed
- **ANSI chat terminal (interactive.py, used whenever the Textual UI isn't available): the
  gather-phase spinner stopped overwriting in place during long plan/gather/replan loops and
  instead printed an endless stack of "Re-planning..." lines.** Root cause: `logging.basicConfig()`
  attaches the default handler to stderr, which has no knowledge of `_ContextPanel`'s cursor
  bookkeeping; background adapters logging at INFO from the feed thread (e.g.
  `bm25_content_store`'s index-build/update notices) could land mid-spin and shift the real
  terminal cursor without the panel knowing, permanently desyncing its `\x1b[nA` cursor-up math so
  every later frame printed as a new line instead of overwriting the last one. Only `textual_ui.py`
  had ever guarded against this (it clears the stderr handler and routes logs into its own RichLog).
  `interactive.py` now installs `_PanelAwareLogHandler` on the root logger, which pauses/erases the
  turn's active panel before writing a log line and resumes it after, same as the textual path.
  Also hardened `_ContextPanel` itself: `start()` now no-ops if a spin thread is already alive
  (previously it always spawned a new one, leaking the old thread as a second, uncoordinated
  writer), and a frame's cursor-up/clear/write sequence and its `_last_line_count` update are now
  built as one string and issued as a single atomic `write()` under the panel's lock, so a
  concurrent `stop()`/`erase()` from another thread can no longer land between two of a frame's
  writes and leave the terminal half-drawn. (`interactive.py`; tests added to
  `tests/test_spinner_panel_overwrite.py`.)
- **Startup/bootstrap notices (e.g. "Context index: computing tfdfidf signatures...") no longer
  print twice in the Textual UI.** `InteractiveSession`'s `notify_and_log` callback wrote every
  notice straight to the console via `_Console.dim` AND forwarded it to the live Textual notice
  callback (`_startup_notify`); under the Textual UI, the direct console write happens before the
  session's console is swapped to the TUI's `RichLogConsole`, so it leaks onto the real stdout
  underneath the TUI's alternate screen while the TUI itself displays the same message again via
  the callback. Extracted the closure into a standalone `_make_startup_notifier` (`interactive.py`)
  that writes to the console ONLY when no live callback is wired (plain ANSI fallback mode); the
  Textual UI's own display is now the sole path for these notices. Test added:
  `tests/test_interactive_startup_notice.py`.
- **Casual questions with no trailing "?" and no textbook interrogative opener no longer
  force-escalate to a deep task.** `_message_requests_change` (the message-intent fallback net
  that can override a planner's correct "answer" decision) recognized questions only via a fixed
  interrogative-opener list or a trailing "?"; a spoken-style question like "Not sure why the
  export looks broken" or "Any idea why the metrics look off" matched neither, so it fell through
  to the function's unconditional `True` and force-escalated into a real task even after the
  planner had already answered correctly. `_INFO_QUESTION_RE` now also recognizes casual/uncertain
  openers ("any idea/clue/chance/reason", "no idea why", "not sure why", "wondering/curious
  if/why/whether"); these route through the existing ambiguous-band LLM judgment
  (`judge_execution_directive`, which defaults to "not a directive" on any failure) instead of
  being decided by regex alone. Deliberate bug-report statements with no interrogative framing at
  all (e.g. "the system incorrectly assigns dates to actions") are unaffected and still escalate
  immediately, per the existing regression contract. (`core/orchestrator.py`; test added to
  `tests/test_orchestrator.py`.)
- **Mid-task steering: a message sent to a running or queued task now actually reaches it.**
  `core/inbox.py`'s `InputInbox` and its drain points (deep-retry loop, answer-improve loop) had no
  producer for background tasks for months, so "message a running task" was inert. A new
  `QuestClient.claim_task_messages` (`POST /api/assistant-tasks/{id}/messages/claim`, atomic: the
  backend stamps `delivered_at` so a re-poll returns `[]` and a message can never be folded into two
  prompts) feeds a throttled callable the executor now passes explicitly as `pending_inputs=` to
  `Orchestrator.run()`, rather than relying on the inbox's own auto-wiring — `_conv_key` only
  resolves via `quest_id`/`conversation_id`/`session_id`/`user_id`, and a goal-only personal task's
  `context_meta` is just `{"goal_id": ...}`, so auto-wiring alone would never see a message for that
  case. The executor also does one drain before the first prompt (catches a message sent while the
  task sat queued) and the outer plan → gather → re-plan loop now drains too (previously only the
  deep-retry and answer-improve loops did, so a message during plain planning was invisible until,
  if ever, an inner loop was reached). Honest limitation: a deep run is still one `claude -p`
  subprocess and cannot be re-prompted mid-flight; a message during one is only visible at the next
  attempt/verification boundary. (`runner/executor.py`, `runner/quest_client.py`,
  `core/orchestrator.py`.)
- **Failed and needs-you task replies no longer post to chat tagged as `kind="done"`.**
  `TaskExecutor._post_conv` tagged four failure/needs-you branches (no-instruction-text,
  orchestrator-exception, claim-corrected, unverified-deep-goal-failure) as `kind="done"`. Since
  quest-backend's chat fold-back check treats any `kind="done"` post as "a terminal reply was
  already posted" and suppresses further messages, a task that actually failed or needed a human
  could read in chat as silently done, with no failure message ever following it. Each branch now
  tags `failed`/`needs_you` correctly. (`runner/executor.py`; regression tests added.)
- **A `claude_cli` deployment no longer resolves its model tiers to models it cannot run.**
  `ClaudeCliProvider.list_models()` returned `[]` (the CLI has no `models.list`), so
  `ModelRegistry.bucket_top` fell through to `DEFAULT_FALLBACK_TOP`, whose `fast`/`balanced`/
  `quality` entries are Gemini ids. On a keyless, CLI-only lane every tier therefore resolved to
  a model no registered provider could execute, and runs died at the first planner call with
  `Gemini model 'gemini-3.1-flash-lite' requested but Gemini provider not registered.
  Available: []`. The only workaround was for each operator to pin `QAR_MODEL_FAST`,
  `QAR_MODEL_BALANCED`, `QAR_MODEL_QUALITY` and `QAR_MODEL_BEST` by hand, which every new lane
  silently forgot. The provider now advertises what it can actually run: the Claude FAMILY ids
  `claude-opus`, `claude-sonnet`, `claude-haiku` (families, not pinned versions, so the list does
  not age and each tier still means "latest of that family"). `bucket_top` buckets them into
  fast/balanced/quality, `MultiProvider` routes them by the `claude` prefix, and `cli_model()`
  maps each back to the bare CLI alias on invoke. Explicit `QAR_MODEL_*` overrides still take
  full precedence, since user fallbacks are applied last.
- **A nonzero worker exit with no stderr no longer asserts a cause it did not verify.**
  `SubprocessGoalRunner` reported "Goal run did not complete cleanly (exit N), likely hit the
  turn/budget limit before fully meeting the goal" whenever stderr was empty. That guess is the
  text a human reads on a failed task, and it sent diagnosis straight to budget tuning even when
  the worker had errored for an unrelated reason. It now states the exit code, says plainly that
  no error output was produced and that the goal was not confirmed met, lists the candidate
  causes as candidates, and appends the tail of the worker's own output so the reader can see
  what the run actually did.
- **A non-numeric file score from the model no longer discards the entire card selection.**
  `_rank_files_batched` in `core/card_filter.py` built its score map from raw JSON values, so a
  score the model returned as a string (`"0.9"`), a `null`, or a word landed in the map untouched;
  the `sorted(...)` call that consumes it sits OUTSIDE the try that guards the LLM call and the
  JSON parse, so comparing that value against the numeric default raised `TypeError` straight out
  of `filter_cards_by_relevance`. All three callers (`adapters/file_context_store.py`,
  `core/turn_context_store.py`, `core/guidance_provider.py`) wrap the call in a blanket `except`,
  so a single malformed score silently threw away the whole card-level LLM selection, not just the
  file ranking (the per-card loop this batched pass replaced contained such a failure per card).
  Each score is now coerced with `float()` inside a per-file try, falling back to the 0.5 neutral
  score, so a bad value costs at most that one file's rank position. Regression test:
  `tests/test_card_filter_selection.py::TestBatchedFileRanking::test_non_numeric_scores_are_coerced_and_never_kill_the_selection`.
- **`QdrantVectorStore` no longer creates one Qdrant collection per scope (collection sprawl),
  and scoped searches now see the shared corpus.** The store derived a collection name from a
  hash of every distinct `scope` dict and created that collection even on mere *search* — and
  since `VectorContextAssembler.assemble()` forwards its `meta` dict (e.g. the executor's
  per-goal `{"goal_id": ...}`) straight through as scope, every distinct goal/conversation left
  a permanent empty collection on the shared Qdrant server (observed: 327 empty collections,
  adding ~35s of sequential shard recovery to server startup). Worse, all real writes were
  unscoped (they landed in `{prefix}_default`), so those scoped searches queried freshly
  created empty collections and silently returned no context. The store now keeps ALL points
  in the single `{prefix}_default` collection with the scope digest as an indexed `_scope`
  payload filter (the same multitenancy model as `QdrantCardVectorStore`, and what Qdrant
  recommends over collection-per-tenant): unscoped points are shared (visible to every scoped
  search), scoped points are private to their exact scope, read paths never create
  collections, point ids are scope-namespaced so the same item id in two scopes cannot
  collide, and search hits now carry the original item id (preserved in the `_id` payload
  field) instead of an opaque numeric hash. Existing unscoped data is picked up unchanged (the
  unified collection is the same `{prefix}_default` the old layout wrote to). A new
  `prune_scope_collections()` maintenance method deletes the empty legacy per-scope
  collections left behind by the old layout (run once per deployment after upgrading).
  (`quest_ai_runner/adapters/qdrant_vector_store.py`, `docs/vector-context.md`.)
- **`QdrantVectorStore` collections are keyed on the embedder's true dimension, and the store
  adopts the real embedding size instead of trusting the declared `vector_size`.** Two stores
  with different embedder configurations (e.g. fastembed 384-d and Voyage 1024-d) pointing at
  the same shared server used to collide on one collection: whichever config created it pinned
  the dimension, and every write from the other config was declined point-by-point server-side
  ("Vector dimension error: expected dim: 384, got 1024") while the never-raises contract kept
  callers oblivious — observed live against a shared dev server. Worse, `vector_size` was only
  a declaration: the auto-wired Voyage/OpenAI embedder paths never set it, so the store could
  create a 384-d collection and then embed 1024-d vectors into it. The unified collection is
  now named `{prefix}_default_{size}`, the store adopts the REAL dimension observed from
  embedder output before any collection is created (warning when it differs from the declared
  size), and a bare legacy `{prefix}_default` is reused only when its configured size matches
  (mismatched legacy collections are left alone with a one-time warning). `prune_scope_collections()`
  never touches `{prefix}_default*` collections and gained a `pace_seconds` throttle for
  sweeping busy shared servers. (`quest_ai_runner/adapters/qdrant_vector_store.py`,
  `tests/test_qdrant_vector_store_scoping.py`.)
- **`cli send` is instant and offline again by default.** `select_card_ids_for_text`'s
  LLM-backed card relevance filter defaulted to `use_llm=True`, and `send`'s auto-card-selection
  called it with no `use_llm` argument -- so every `send` (an offline, instant command before
  the auto-card-selection feature) started making a model call for card selection by default,
  adding latency and cost, with the whole selection path (LLM provider setup AND the card store
  assemble) wrapped in a bare `except Exception: return AssembledContext()` that swallowed any
  failure with no trace. `use_llm` now defaults to `False` (keyword/IDF selection only, no model
  call); `search-context` is unaffected since it always passes `use_llm` explicitly (its own
  `--no-llm` flag). Swallowed exceptions in both the LLM-provider setup and the card-store
  assemble are now logged as warnings instead of vanishing silently. (`quest_ai_runner/cli.py`.)

- **A genuine question no longer silently opens a task.** Two gaps in the answer->deep escalation
  net let a question phrased with a change verb (or answered with fix-shaped language) get
  escalated to execution even when the planner correctly chose "answer": (1) `_message_requests_change`
  only treated a "?"-ending message as a question when it had NO change verb at all, so
  conversational questions like "can we improve conversion here?" or "should we optimize this
  query?" fell through to `True` and were escalated by regex alone instead of reaching the
  ambiguous-band LLM judgment (`judge_execution_directive`) built for exactly this case. Now any
  "?"-ending message defaults to `False` (a question) unless it was already caught as a
  "you"-directed polite command ("can you fix...?"); a verb/wrongness signal still routes it into
  the LLM judgment band rather than dropping it. (2) `_answer_describes_unexecuted_work` escalated
  purely off the ANSWER text ("the fix is to update X", "this needs to be updated") with no regard
  to whether the user's own message was a change request at all -- so answering an honest question
  ("why is X broken?") with an explanation of what a fix would involve could silently open a task.
  It is now additionally gated on `_message_requests_change(user_message)`, so a described-but-
  unexecuted fix only auto-escalates when the user actually asked for a change; a question that
  still carries an ambiguous signal gets the same fair LLM judgment instead of a blind regex verdict.
  (`quest_ai_runner/core/orchestrator.py`.)

- **A claude_cli deployment no longer dies on a foreign tier model.** With
  `QAR_MODEL_BACKEND=claude_cli` and no tier overrides, `ModelRegistry`'s generic last-known
  defaults resolve the fast/balanced/quality tiers to Gemini ids; the orchestrator's
  provider-routing then found no Gemini provider, fell back to the primary claude_cli provider,
  and passed the Gemini id straight to `claude --model` — which exits 1 having done nothing, so
  EVERY queued task failed at its first planner call. `cli_model()` now applies the same gate the
  deep runner's `_is_claude_model` has always applied: a non-Claude id maps to `None` (the CLI's
  default model) instead of leaking into `--model`. Additionally, `_invoke` no longer reports a
  useless "no stderr" on failure: in `--output-format json` mode the CLI puts the real error in
  the stdout envelope (`is_error`/`result`), which is now surfaced in the raised error.
  (`quest_ai_runner/adapters/claude_cli_provider.py`.)

- **Socket-level transport errors no longer escape `QuestClient` raw.** `_request` wrapped only
  `HTTPError`/`URLError`, but a read that times out (or a connection reset) escapes `urlopen` as
  a RAW `TimeoutError`/`OSError` — blowing through every "never raises" caller contract (e.g. the
  fast lane's `wait_for_interactive`) and spamming the poller log with a full traceback per
  iteration. These now wrap into `QuestApiError` like every other transport failure, so callers
  degrade calmly and retry. (`quest_ai_runner/runner/quest_client.py`.)

- **A budget-capped CHANGE REQUEST escalates to deep instead of wrapping up with words.** The
  read-budget fallback answered with a best-effort reply whenever ANY observation had been
  gathered — even for an explicit "do X" task with escalation available — and that reply path
  returns early, bypassing the `_answer_describes_unexecuted_work` net that guards the normal
  answer path. Caught live by an artifact-verified reliability battery (2026-07-19): a
  write-a-file probe was answered "I cannot execute this task in the read-and-answer step …
  the system will need to execute the following …" and reported `done` with nothing executed.
  Now `_message_requests_change` gates the wrap-up: a change request escalates to deep (brainstorm
  mode unchanged — escalation is unavailable there by design). The classifier also learned two
  confession shapes it missed: "I cannot execute/run …" and "the system will need to execute/
  write …". (`quest_ai_runner/core/orchestrator.py`.)

- **Removed the keyword-driven "confirm redundancy" gate (`_confirm_is_redundant` /
  `_CONFIRM_FORK_MARKERS`).** It auto-executed a planner-originated confirm (skipping the human
  fork) unless a fixed denylist of risky words (delete/send/pay/deploy/...) appeared in the
  request or in the planner's own confirm question. Gating a human-in-the-loop safety decision on
  substring matches against free-form model output is brittle by construction: the denylist
  silently leaked every outward-facing verb it did not anticipate (share/invite/grant/message/
  gift/transfer/...), so "share this doc with the team" auto-executed with no approval. QAR now
  honors the planner's confirm decision directly and never inspects keywords in the planner's text
  to decide whether to execute; an over-asking planner is fixed in the planner, not with a
  post-hoc filter on its output. (`quest_ai_runner/core/orchestrator.py`, `CLAUDE.md`.)

- **A missing per-team ai-profile no longer warns on every poll.** `get_ai_profile` /
  `update_ai_profile` logged a WARNING on any error; a 404 just means the member has no per-team
  AI rep profile (e.g. a registry-rep-only executor), an expected state hit twice per task by
  the rep-sync loop. 404s now log at debug; real failures still warn.
  (`quest_ai_runner/runner/quest_client.py`.)

### Added
- **`GoogleDriveAdapter`** (`quest_ai_runner/adapters/google_drive_adapter.py`) — a generic
  `RetrievalAdapter` over Google Drive files and folders, mirroring `GoogleChatAdapter` exactly:
  auth is injected via a token provider (reuses `google_chat_adapter`'s `service_account_token_provider`
  / `static_token_provider`, not duplicated; pass `scopes=DEFAULT_DRIVE_SCOPES`), HTTP is
  stdlib-only, and every method returns `Observation(kind="error")` rather than raising.
  `query({"action": "list", "folder_id": ...})` lists a folder's direct children (id, name,
  mimeType, webViewLink, modifiedTime, size); `query({"action": "read", "file_id": ...})` reads a
  file's text content — Google Docs export as plain text, Google Sheets export their FIRST sheet as
  CSV (a documented limitation), PDFs are extracted via the optional `pypdf` package (new `[drive]`
  extra; lazy-imported so it's never a hard dependency), and plain-text mimeTypes are decoded as
  UTF-8; any other mimeType returns a clear "not supported yet" error instead of raw binary.
  `read_section(file_or_folder_id_or_url)` delegates to the same read/list logic so the adapter
  works through the generic surface, not only `query()`; a folder id is auto-detected via its
  mimeType and listed. `grep` is not supported (Drive has no cheap full-text index here), matching
  the same "use query() instead" convention `CachedDbAdapter` / `QuestRetrievalAdapter` use.
  `parse_drive_url(url)` parses `drive.google.com/file/d/...`, `drive.google.com/drive/folders/...`,
  `docs.google.com/document/d/...`, and `docs.google.com/spreadsheets/d/...` into
  `{"kind": "file"|"folder", "id": ...}`. Discovery (`list_sources`/`describe_source`/
  `list_operations`/`describe_operation`) follows the same tone as the other adapters.
  (`quest_ai_runner/adapters/google_drive_adapter.py`, exported from `adapters/__init__.py`;
  `pyproject.toml` `[drive]` extra; tests in `tests/test_google_drive_adapter.py`.)
- **`GoogleDriveAdapter.query()` now accepts pasted URLs, not just bare ids** — `folder_id` (for
  `action: "list"`) and `file_id` (for `action: "read"`) are each resolved via `parse_drive_url`
  before use, exactly like `read_section` already did. Previously only `read_section` accepted a
  raw Drive/Docs/Sheets URL; a consumer whose planner only ever calls `query()` (e.g. one source
  among several routed through a single `query({"source": ..., ...})` dispatch) would fail on a
  pasted link because the Drive REST API needs a bare id, not a URL. Non-URL ids are unaffected
  (`parse_drive_url` returns `None` for a bare id and the original string is used as-is).
- **Narrator cross-turn repeat memory** (`prior_narration` on `Orchestrator.run` /
  `OrchestratorResult.narration_said`): a fresh `Narrator` is built for every turn, so it previously
  had no memory of narration it already spoke aloud in EARLIER turns of the same conversation, only
  of what it said within the current turn (`_said`). On a voice consumer that speaks every beat, the
  ack (`begin()`) is grounded only in message content, so it could open turn after turn with its own
  differently-worded but equally content-free "let me look into that" / "searching for that now"
  line. A caller now passes the last few lines it actually delivered to audio back in via
  `run(prior_narration=[...])` (a reasonable source is `OrchestratorResult.narration_said` from
  earlier turns, capped by the caller); they seed the repeat-detector (`_is_repeat`) and are shown to
  the model as an explicit "already said in earlier turns, do not repeat this shape" block, separate
  from the this-turn `_said` block. Also fixed a real gap in `_is_repeat` itself: a line that
  normalizes to NO content words at all (every word stopworded away, e.g. "Let me look into that for
  you") previously always returned "not a repeat" (nothing to word-overlap-compare), so purely
  generic filler could recur without limit; it is now capped to at most one per conversation. The
  ack path (`_gen_and_say`) is now also backstopped by `_is_repeat` (previously only `relay()` was).
  Absent `prior_narration`, behavior is unchanged. (`quest_ai_runner/core/orchestrator.py`.)
- **Card-scoped learning is now a shared, adapter-agnostic module** so any retrieval adapter can
  reuse it, not just `ClaudeConversationsAdapter`. The union-gate / intersection-learn / usage-stamp
  logic moved out of that adapter's private methods into
  `quest_ai_runner/adapters/card_scoped_learning.py`, exposing `active_card_terms(card_store,
  card_id)`, `gate_terms(query_terms, card_terms)`, `learnable_candidates(candidates, terms_of,
  query, card)`, and `learn_card_references(card_store, card_id, candidates, *, ref_type, locator_fn,
  why, now)`. Nothing in the module hardcodes `"conversation"` / `conv_id`: the caller supplies the
  `ref_type`, a `locator_fn(candidate) -> dict`, and the `why`, and any card store duck-typed like
  `FileContextStore` (`get_card` / `update_card` / `mark_sources_used`) participates. Behavior is
  unchanged — `ClaudeConversationsAdapter` now delegates to the shared functions. (`google_chat_adapter`
  now adopts this too — see the reference-resolution capability entry below.)
  (`quest_ai_runner/adapters/card_scoped_learning.py`, `claude_conversations_adapter.py`,
  `google_chat_adapter.py`.)
- **Reference resolution is now a formal, checkable `RetrievalAdapter` capability — and Google Chat is
  wired through it.** "Can this adapter's content be persisted as a learned card reference and
  re-fetched fresh later?" was tribal knowledge (you had to read code); the one `conversation` resolver
  was hard-wired to local Claude session files. `core.adapters.RetrievalAdapter` (and its ABC
  `RetrievalAdapterBase`) now carry three optional members — `reference_type: Optional[str]`,
  `make_locator(candidate) -> dict`, `resolve_reference(locator, *, max_chars) -> Optional[str]` — the
  same optional-capability convention `query()` already uses (NO second protocol). The ABC default is
  `reference_type = None`, so every existing adapter is structurally "unsupported" untouched, and the
  whole check is `adapter.reference_type is not None`. `ClaudeConversationsAdapter` formalizes its
  existing conv_id/session-file logic under `reference_type = "conversation"` (behavior unchanged).
  `GoogleChatAdapter` gains a REAL `reference_type = "chat_thread"` (a new distinct type, registered in
  `reference_resolver.CONTENT_TYPES`), a `make_locator` (`{"space", "thread_or_message_id"}`, thread-
  level when `group_by="thread"`), and a `resolve_reference` that re-fetches the thread FRESH through
  its own read path; with a `card_store` its `assemble()` now learns threads onto the active card via
  `card_scoped_learning`, exactly like the Claude adapter. `reference_resolver.collect_reference_resolvers`
  discovers resolvable adapters from `cfg.retrieval` (walks composites) and `build_resolver_registry`
  coerces a bare `resolve_reference` callable into a `ReferenceResolver`, so `config.py` wires a
  consumer's `GoogleChatAdapter` with no boilerplate; `FileContextStore.register_reference_resolver`
  wires the config-internal Claude assembler. Resolvability + learning are done; full thread-to-card
  TOPIC routing for team chat remains a separate, open problem. (`quest_ai_runner/core/adapters.py`,
  `adapters/reference_resolver.py`, `adapters/claude_conversations_adapter.py`,
  `adapters/google_chat_adapter.py`, `adapters/file_context_store.py`, `config.py`.)
- **Cross-session recall now becomes a LEARNED card reference instead of being recomputed from the
  whole history every turn.** `ClaudeConversationsAdapter.assemble` used to keyword-gate past Claude
  sessions against the query alone and return them as an ephemeral view, disconnected from the card
  store: no persistence, no `last_used_ts`/`use_count`. It now takes the turn's ACTIVE card
  (`meta["thread_card_id"]`, already threaded by the orchestrator) and an injected `card_store`, and:
  (a) WIDENS the relevance gate to the union of the query terms and the card's own topic terms (so a
  conversation about the card's idea surfaces even when it doesn't match this turn's wording), and
  (b) for the selected conversations relevant to BOTH the request and the card, attaches each as a
  `conversation` reference via `FileContextStore.update_card` and stamps it via `mark_sources_used`
  — so recall participates in the SAME usage-recency retrieval that files and collections already
  get, and future turns retrieve it by recency instead of re-scanning. With no active card (or no
  store) it degrades to the exact prior global keyword + TF-DF-IDF scan. Re-selecting the same
  conversation on a later turn re-warms the existing reference (dedupe by `conv_id`) rather than
  duplicating it. New `FileContextStore.get_card` read seam. Team-chat thread context
  (`google_chat_adapter`) is a deliberately out-of-scope follow-up (needs its own thread-to-card
  assignment logic). (`quest_ai_runner/adapters/claude_conversations_adapter.py`,
  `file_context_store.py`, `config.py`.)

### Fixed
- **`CARD_THREAD_GATE`'s continue-vs-new call now uses a concrete test, not a "match on meaning"
  vibe.** A sub-decision inside a plan (pricing, timeline, a vendor pick) routinely sounds like its
  own subject without being independently recallable, and the old wording had no real test for that
  mismatch, so it sometimes misfiled a sub-decision onto its own card instead of the effort it
  serves. Replaced with: "if the current card were deleted tomorrow, would anyone still want to look
  this up on its own?", plus worked few-shot examples (`quest_ai_runner/core/context_doctrine.py`).
  The "graduation" mechanism (a recurring sub-topic eventually earning its own card) remains
  intentionally out of scope for this per-turn gate.
- **The reference-reuse loop is closed: a deep run's findings now actually reach the next deep run.**
  A deep run explored an environment, wrote what it found as future context, and the updater turned
  that into a card of typed, resolvable file REFERENCES. Then the loop broke, silently, in three
  places at once, and the next run explored from scratch anyway:
  - **The learned card was unretrievable.** A card whose knowledge is references (an updater writes
    `name` + `description` + `content`, never a bootstrapped `summary`/`files`) was scored ONLY on
    its items' `why` text. Its name, its description, and the file paths its references point at
    were not indexed at all, so it landed under the confidence gate while the bootstrapped file
    cards it should have beaten sailed over it. Those fields are now indexed at the same
    intent-weighted levels (`_card_term_weights`), so the card that carries the run's hard-won paths
    is retrievable by its topic AND by the paths it points at.
  - **The relevance filter culled it.** The LLM filter judges a card by its title and the files it
    covers; a learned card was handed to it with an empty title and zero files, so it read as an
    untitled card about nothing. It now sees the card's real title (`_card_display_title`) and the
    paths its references cover (`_card_covered_paths`), and it renders with that title instead of
    "(no summary)".
  - **A rendered reference did not say WHERE it came from.** An item rendered as `- (file) <why>`
    plus the file's live contents, never the path. A body of code with no path is not a reference:
    the next worker cannot re-read, edit, or search around it. Every reference now names its target
    (`locator_label`): `- (file) src/x/y.py -- <why>`, and the same for a collection's name/id.
  Proven with real Claude Code deep runs over a throwaway codebase: a second run, from a DIFFERENT
  conversation, on a related question now arrives with the first run's paths (and their live
  contents) in its brief and makes 1 exploration tool call, against 8 for the same question with an
  empty card store, at 34% fewer worker tokens.

### Added
- **Per-source usage recency: a card knows which of its sources are hot and which have gone cold.**
  A file entry's `mtime`/`sha256` are FINGERPRINTS (has the file changed), and `usage_count` is
  CARD-level (was this card used). Neither could say WHICH of a card's sources actually carried the
  value, so a card that accumulated sources had no way to let the dead ones sink. Every source now
  also carries `last_used_ts` + `use_count`: a file entry and EVERY typed content item (file,
  collection, conversation, query, note, so a conversation warms exactly like a file). They are
  stamped at the seam where assembly RESOLVES AND RENDERS that source into the context view, so a
  source that is merely held by a selected card is never warmed and goes cold relative to what is
  actually used. The content ranker now blends used-recency with relevance and learned-recency, so
  under a render budget the hot sources win and the cold ones are outranked (never dropped, they
  still resolve when the budget reaches them). `mark_sources_used` is the public seam for any other
  assembler arm.
  - The stamp lands AFTER rendering and never enters the rendered text, and every rendered source is
    re-warmed by the same amount, so two identical turns render byte-identically: prompt-cache
    prefixes are unaffected (pinned by a test).
  - Debounced (60s): one turn assembles context several times (the run-level view, each deep goal, a
    widening retry), which is ONE use, so a turn cannot rewrite a card repeatedly or inflate a
    count.
  - Missing fields mean "never used". Legacy cards keep ranking exactly as before and are never
    rewritten in bulk: a card gains the fields the first time it is actually used.
- **The future-context ask now asks for the EXPENSIVE knowledge.** Both instructions (in-band and
  out-of-band) now lead with the environment references: the exact location of each thing the worker
  relied on, written the way that environment addresses it (a path relative to the working
  directory, a collection with its name AND id), the entry points, and what was RULED OUT so nobody
  pays for that search twice. Prose comes last, and a bullet that describes a thing without saying
  where it is is not acceptable. New reference items are timestamped by the brain (`_apply_card_edits`),
  since the updater model has no clock and an unstamped item ranks as maximally old and is trimmed
  first.

### Fixed
- **Future context no longer corrupts a strict-format deep runner's payload, and is no longer lost
  for one.** When the async card updater is on, the orchestrator appended
  `DEEP_FUTURE_CONTEXT_INSTRUCTION` to EVERY deep brief, telling the worker to end its output with a
  prose `=== FUTURE CONTEXT ... ===` section. That is right for a prose worker and fatal for a runner
  whose output IS the deliverable in a strict format: a code-generating deep runner emitted Python
  with a prose block appended, which failed syntax review in a large fraction of production-like runs.
  The consumer-side workaround (strip the instruction out of the brief) protected the payload but
  silently cost the feature: that runner was then never ASKED for future context, so
  `_parse_future_context` got nothing and the card updater learned NOTHING from the turns that did
  real work.

  The defect was the CHANNEL, not the capability, so every runner is still asked, and the answer now
  travels out of band:
  - `DeepResult.future_context`: a structured field carrying the bullets, separate from `output`.
  - `DeepRunner.future_context_channel`, declared per runner. `FUTURE_CONTEXT_VIA_OUTPUT` (the
    default, and what any runner that declares nothing gets) keeps today's behaviour byte for byte.
    A runner whose output is a strict format (generated code, JSON, a patch) declares
    `FUTURE_CONTEXT_VIA_FIELD`, is asked for future context by `DEEP_FUTURE_CONTEXT_FIELD_INSTRUCTION`
    ("return it in the future-context field, never inside your primary output"), and returns it in
    `DeepResult.future_context`. It is read with `getattr`, so duck-typed runners keep working.
  - **One normalization seam.** Every `DeepResult`, from every runner, is passed through
    `_normalize_future_context` the moment the runner returns: the delimited section is parsed into
    `future_context` and CUT from `output`. Payload corruption is now impossible by construction, not
    by each consumer remembering to strip: a worker that ignores the instruction and appends the block
    to generated code still yields a payload that parses. Consumers doing a downstream strip can keep
    it as a belt-and-braces net; it is no longer load-bearing.
  - Both channels feed the SAME destination they always did (the async card updater's one LLM call,
    and the "what I'll remember" panel), which now read `DeepResult.future_context` (falling back to
    parsing `output` for a result built outside the seam, e.g. reflected back through a queue).
- **A background context-index thread can no longer outlive its owner.**
  `config._bootstrap_if_needed` starts the index build/refresh on a daemon thread, and nothing ever
  joined or cancelled it. It kept walking the corpus and shelling out `git hash-object` per file long
  after whatever started it was gone, so its stray subprocess calls landed in whatever the process did
  next. In the test suite that surfaced as a different test failing on each run (whichever later test
  happened to have `subprocess` or the environment patched captured a stray `git ... hash-object`),
  which is how real regressions hide. Indexing is now owned:
  - `FileContextStore.close()` / `is_closed()`: stops that store's bootstrap/refresh at its next
    checkpoint (entry, each walked directory, before the LLM fan-out, before the fingerprint pass) and
    guarantees no further `git` subprocess is spawned. Cards already written are kept; indexing is
    incremental and resumes on the next start.
  - `config.shutdown_background_index(timeout=10.0)`: closes every store an index thread was started
    for and JOINS those threads, so after it returns the process has no index thread running. Call it
    when an orchestrator's owner goes away (a rebuilt wiring, a tenant shutdown, a CLI exiting).
  An open store fingerprints exactly as before, so production indexing is unchanged. A `conftest`
  fixture calls `shutdown_background_index()` after every test, which makes the suite deterministic
  (19 tests used to end with a live index thread; now none do).

### Added
- **Per-idea threading, where THE IDEA IS THE CARD (`core/card_thread.py`, opt-in via
  `OrchestratorConfig.card_thread_enabled`, default OFF).** A conversation is not one topic. People
  interleave: they open a plan, drop into a side question, come back with "back to the launch plan".
  There is deliberately NO thread object: a thread object would be a second registry of what context
  cards already are, and it would die the moment an idea outlived its conversation, which is the
  normal case. So a thread IS a card, and a card's transcript spans are its thread.
  - **The assignment costs ZERO extra model calls.** The planner emits ONE field on the call it
    already makes every turn: `"continue"` | `"switch_to:<card_id>"` | `"new:<label>"`. It is shown a
    cheap PRIOR first, built from the cards this turn's hybrid retrieval (keyword/IDF arm + vector
    arm) already scored, plus whatever the consumer always wants offered, so the prior costs no extra
    search either. The prior only NARROWS and SURFACES; the model's judgment decides.
  - **Fail-safe throughout.** Any parse failure, any ambiguity, any `switch_to:` naming a card that
    is not on the table: CONTINUE the current card (`parse_card_thread`). A planner failure keeps the
    thread rather than losing it. A topic assignment can never cost a turn.
  - **A topic switch is NOT a mode signal.** Moving to another idea inside a brainstorm-latched
    conversation leaves it latched, and returning to an old idea does not release it.
  - **Priority blending, not isolation.** `select_thread_floor` gives a turn its own card's recent
    turns plus a small global floor of the very last turns whatever their card, so "as I just said"
    survives an interleave; `rank_card_first` / `penalized_budget` keep every other idea reachable
    behind a penalty, so "combine those two ideas" still works.
  - **A card outlives the work it describes.** `lifecycle_note` renders finished work as finished,
    and the new `CARD_LIFECYCLE_GATE` (context_doctrine, on the reply's system prompt) tells the model
    to treat it as knowledge it may cite and build on, never as open work waiting to be picked up.
  - **A dedupe guard** (`find_duplicate_label`) so `new:` cannot litter the card space with
    near-duplicate twins of a card that already exists.
  - Reported on `OrchestratorResult.card_thread` and the new `EVENT_CARD_THREAD`; the orchestrator
    stays stateless about threads exactly as it is about modes, so the consumer owns the card store
    and stamps its own messages. `card_type` / `lifecycle` now ride through into `card_metadata`, so
    a consumer whose store also holds non-topic cards can say which types are real ideas.
  - With the flag off (the default) the planner prompt carries no topic block, the decide-tool schema
    has no `card_thread` field, a stray `card_thread` is ignored, no event is emitted, and no thread
    meta reaches the assembler: byte-identical to a build without the feature.
- **A task document can supply its own persona (`rep_preamble`).** The poller now falls back to a
  task's optional `rep_preamble` field (a non-empty string; anything else is ignored) when no AI rep
  resolves for that task: `self._pull_rep_for(task, target) or self._task_rep_preamble(task)`. A
  resolved rep's pulled persona still wins, so this only fills the gap. The string becomes the deep
  run's `context_preamble` AND the voice of the fold-back "done" report, with no resolver, profile,
  or consumer glue. The case it exists for: work deferred out of a live conversation, where the
  queueing side already knows the persona that conversation runs with, so the report posted back
  into that conversation speaks in the same voice as the replies already in it. The task document is
  passed through the client untouched (no schema strips unknown keys), so a backend only has to
  stamp the field. Documented as part of the task-document contract in
  `docs/quest-api-contract.md` (a new "The task document the runner reads" table) and in
  `docs/writing-a-consumer.md`.
- **Autopilot pass (`runner/autopilot.py`).** A new `AutopilotPass`, routed by the executor for
  any task whose **`task_kind == "autopilot"`** (a recurring pass task). Routing reads `task_kind`,
  the backend's PERSISTENT classification, never `handler` — the poller overwrites `handler` on
  every `claim()` with the claiming worker's label, so a re-polled/retried/resumed pass task would
  otherwise stop routing (`handler == "autopilot"` is still honored as a back-compat fallback).
  Scans a team's quests whose `autopilot.mode` is `suggest`/`act`; gates each quest, cheapest first
  (a team-wide daily budget via a new `RunnerConfig.autopilot_daily_budget`, per-quest cadence off
  `autopilot.last_pass_at`, backpressure from a still-open prior autopilot task, an open HOLD
  decision on the quest); targets the quest's current-scope incomplete `ai_help` goals (today's/this
  period's, or the single next incomplete one when unscoped); resolves a persona per goal (goal
  `assignee_rep_id` -> a day-matched/unrestricted `autopilot.personas` entry -> a consumer-injected
  `RunnerConfig.autopilot_persona_resolver` fallback) and batches goals sharing a persona into one
  task; proposes the quest's next goal instead when `planning=="plan_and_work"` and nothing is
  eligible. A pass task whose text contains "dry-run" reports what WOULD be created and creates
  nothing. Per-quest failures are isolated (one quest's error never aborts the pass) and every
  skipped quest is logged with its gate reason.

  Coded against the **verified** Quest API contract, which differs from the design sketch in ways
  that would each have failed silently:
  - Autopilot settings are read per quest from the full quest state (`get_quest_autopilot` ->
    `GET /api/quests/{id}/state`); the team quest *listing* carries no `autopilot` block, so
    reading opt-in from it would treat every quest as `off` forever.
  - Created work carries `task_kind="autopilot_work"` (**not** the pass's own kind, which would
    route each created task into another pass: an infinite loop). `source` stays within the API's
    closed enum (`chat`/`reflection`/`review`) — `source="autopilot"` is rejected with a 400.
  - The create route has no `status`, `quest_id`, or persona field: `suggested` is reached by
    create-then-PATCH (`QuestClient.update_task`, which raises on failure so an unapproved
    proposal can never silently stay queued and execute), a task's `goal_id` **is** its quest link,
    and the resolved persona is named in the task text (it still decides the batching).
  - Goal descriptions (the AI's brief) are fetched per target goal via `get_goal`; the grouping
    payload omits them.
  - Period ids use the backend's underscore format (`2026_W28` / `2026_07` / `2026_Q3`).
  - `update_quest_autopilot` targets `PATCH /api/quests/{id}/autopilot` (flat body, merges
    server-side). That endpoint does **not** yet accept `last_pass_at`/`miss_streak`, so the pass
    verifies the echoed settings and reports a loud `bookkeeping_warnings` entry when a write did
    not persist, rather than letting the cadence gate go silently inert.

  New `QuestClient` methods: `get_quest_autopilot`, `update_quest_autopilot`, `update_task`,
  `list_tasks` (server-side `status`/`goal_id`/`team_id`; `source`/`task_kind` applied client-side,
  since the API has no such query params), `list_open_decisions_for_quest`
  (`GET /api/teams/decisions/for-quest`, open-only filtering done client-side since the route
  returns open *and* resolved). `create_task` gains `env_id` and `task_kind`.
- **Per-task working directory.** The executor now resolves a task's `goal_id`/`quest_id` through
  the configured `RunnerConfig.quest_folder_map` and, when mapped, passes that folder as a per-run
  `working_dir` override to the deep run (a new optional `working_dir` kwarg on `DeepRunner.run_goal`,
  threaded through `Orchestrator.run(working_dir_override=...)` / `GoalRunner.run` /
  `SubprocessGoalRunner.run_goal`, opt-in by signature inspection like `context_preamble`); falls
  back to the deep-runner's configured global `working_dir` when no mapping exists. Applies to
  every task, not just Autopilot-created ones.
- **Truly asynchronous deferred deep work (queued deployments).** A consumer whose deep runner
  QUEUES a planner `deferred_deep` as a background task (returning
  `DeepResult(met=True, deferred=True, output=<hand-off sentinel>)` only after the enqueue is
  confirmed) can now set `OrchestratorConfig.deferred_deep_queued=True` and get truthful,
  loop-safe behavior end to end:
  - **Dynamic planner wording.** The planner doctrine sentence and the decide-tool
    `deferred_deep` description now match the configured mechanism: inline (default) keeps the
    same-turn wording; queued says the work is handed to the background task queue and the user
    is told in the conversation when it finishes (`DEFERRED_DEEP_INLINE_SEMANTICS` /
    `DEFERRED_DEEP_QUEUED_SEMANTICS`, `decide_tool_for(...)`). Both wordings are only ever shown
    when true.
  - **Confirmed hand-off contract.** A deferred met result activates the dormant
    `DeepResult.deferred` contract in the answer path too: the reply is re-synthesized through a
    new queued hand-off prompt (`SYNTHESIZE_AFTER_QUEUED_PROMPT`, reports the work as queued,
    never as done), the answer goal-verification loop is skipped for the turn (no re-verify of a
    sentinel, no remediation relaunch that could double-enqueue), and the result's
    `exit_reason` is `"deferred"`.
  - **Honest enqueue.** When the deployment queues deferred work and NO hand-off was confirmed
    (the enqueue failed), the reply is regenerated with a steer that forbids claiming the work
    was queued, started, or done; nothing silently retries the enqueue.
  - **Pinned queue runner.** A named runner registered under the reserved `deep_runners` key
    `DEFERRED_RUNNER_KEY` ("deferred") receives every deferred_deep hand-off directly, so the
    runner classifier can never re-route deferred work to an inline runner (`_run_deep` grew a
    `runner_override` for this).
- **Executor done posts read as the AI reporting its own finished work.** The runner executor's
  fully-met deep done message is now folded through a report synthesis
  (`Orchestrator.synthesize_task_report`, worker tier, same prompt shape as the interactive
  after-deep fold-back) and CLAIM-CHECKED against the run's execution record before posting
  (`Orchestrator.report_claims_unbacked`, the existing claims_unexecuted machinery): a rewrite
  with an unbacked completion claim, or one that cannot be checked, is discarded for the raw
  goal-verified output. An UNVERIFIED deep result (verification could not run) now posts a
  message that says so plainly, presenting any work output as unconfirmed, and keeps a non-done
  status; it is never a bare "Done".
- **Reserved `card_id` on conversation progress posts (no behavior).** A task carrying a
  `card_id` has it forwarded on every progress post body
  (`QuestClient.post_conversation_message(card_id=...)`) so a future backend can thread a
  task's posts under a per-idea context card. Absent, nothing changes.
- **Brainstorm mode now says out loud when it held a directive back.** Every reply a latched turn
  produces carries a no-action acknowledgment as its `reply_directive` (on the answering call's
  system prompt): nothing was executed because brainstorm mode is on, and the user can say to go
  ahead when ready, plus, when the turn itself was ready to act, that the work will begin on a
  go-ahead, and, when it wanted to ask something first, the question itself. The steer is guidance
  with explicit permission to skip or soften it when it would read as awkward or redundant (e.g. a
  reply that already makes the no-action state obvious, or a user who was plainly just musing),
  never an absolute rule; its one hard rule is that the reply may never say or imply that anything
  ran, is running, or is about to run. Zero extra LLM calls, and unlatched turns are unchanged (no
  steer text in any prompt). Tests in `tests/test_brainstorm_mode.py`. (See Fixed below for the two
  real-model defects this shape fixes.)
- **The brainstorm latch is introspectable from the public `core` namespace.** A consumer's compat
  probe can now key on stable exported names to tell whether the library build it loaded actually
  has the judged latch (a stale build must never be able to report the latch as working):
  `Orchestrator.judge_brainstorm_release`, the `OrchestratorConfig` fields `execution_mode` /
  `mode_signals_enabled` / `mode_release_tier`, `OrchestratorResult.mode_signal`, and the newly
  exported `MODE_RELEASE_TOOL` (`name == "brainstorm_release_verdict"`), `MODE_RELEASE_PROMPT`,
  `BRAINSTORM_NO_ACTION_ACK_NOTE`, `BRAINSTORM_HELD_WORK_ACK_NOTE` and `EVENT_MODE_SIGNAL`.
- **`QAR_VECTOR_BACKEND`: an explicit switch for the auto-built context vector store.**
  `resolve_context_assembler` previously always attempted to construct the auto-built
  Qdrant vector arm when no explicit `vector_store` was configured, logging a
  "Qdrant open failed" warning on every build for deployments that intentionally run
  without the `[qdrant]` extra. The new env var gates that attempt: `auto`
  (default/unset) keeps the previous behavior (attempt Qdrant, fall back to
  keyword-only with a warning), `none`/`off` skips the Qdrant attempt entirely (silent
  keyword-only fallback: no construction attempt, no warning), and `qdrant` requires
  Qdrant, logging an ERROR when it cannot be opened (still degrading to keyword-only so
  the runner starts). The switch never overrides an explicitly configured
  `vector_store` or the qdrant card backend's query-only vector arm. Unrecognized
  values are treated as `auto` with a warning. Documented in `cli.py`'s env reference
  and `docs/vector-context.md`.
- **Brainstorm execution mode: a consumer-owned no-action latch.**
  `OrchestratorConfig.execution_mode` ("normal", the default, or "brainstorm") lets a consumer
  run a turn in which the user is explicitly thinking out loud: reads, context assembly, and
  answers are untouched (full context, full intelligence), but nothing may ACT. A planner
  "deep" or "confirm" degrades to "answer", and every net that can only ADD execution to a turn
  (planner `deferred_deep`, the described-work and message-intent escalation fallbacks, overseer
  escalations -- including a late hook-B consult finishing in the background, which raises no
  decision-request while the latch is held -- and claim-remediation and insufficient-context
  deep re-runs) is skipped. Planner-detected mode changes are a separate OPT-IN flag,
  `OrchestratorConfig.mode_signals_enabled` (default off): when enabled, the planner detects an
  explicit request to change mode via an optional `mode_signal` field
  ("enter_brainstorm" | "exit_brainstorm" | null) on the structured decision it already returns
  every turn (LLM judgment of explicit user intent; zero extra calls, no phrase matching),
  surfaced to the consumer through the new `EVENT_MODE_SIGNAL` event and
  `OrchestratorResult.mode_signal`. With the flag off, the planner prompt carries no mode
  vocabulary, the decide tool schema has no `mode_signal` field, and a stray `mode_signal` in a
  response is ignored -- a consumer that never opted in can never have a turn's actions
  suppressed by a misread musing, and may still drive `execution_mode` purely from its own
  state. The orchestrator stays stateless: the consumer owns persisting the latch and passing
  the mode back in per run. Fail-safe throughout: an unrecognized `mode_signal` value
  normalizes to null (no mode change), an unrecognized `execution_mode` behaves as "normal",
  and with the defaults behavior is unchanged. An "exit_brainstorm" signal releases the gating
  for the same turn the user asked to proceed in; an "enter_brainstorm" signal engages it
  immediately.

### Fixed
- **A latched brainstorm turn now escalates nothing and acknowledges the hold on every reply.**
  Two holes, both found by driving the REAL orchestrator (not stubs) on a latched conversation:
  - **"clarify" escaped the hold.** The latch degraded only `deep` and `confirm`, while the
    planner note invited `clarify` as an available action and `clarify` surfaces its question
    through the ESCALATION SINK, so a conversation the user explicitly put on hold still parked a
    real decision-request. The same was true of the input-understanding stage, which short-circuits
    the turn with a `confirm` when it cannot resolve a short/anaphoric message. Now: the planner
    note offers only `read` and `answer`; `deep`/`confirm`/`clarify` are degraded to `answer` both
    in the loop and again at a single TERMINAL gate every path funnels through (whatever set the
    action: planner, read-loop safety escalation, overseer), and the understanding stage does not
    escalate while latched. The question is not lost: it rides into the reply text, so the user is
    still asked, in the conversation, with no pending ask created anywhere. The release judgment
    also runs BEFORE the understanding stage now, so nothing can escalate ahead of it. The
    invariant, and the test: a latched turn creates NO decision-request and executes NOTHING, via
    any action or any escalation path (deep, confirm, clarify, read-budget wrap-up, overseer,
    claim remediation, need-more-context).
  - **The no-action acknowledgment never reached the model.** It was folded into the answer
    GROUNDING at one call site, so the terminal paths that return earlier (clarify, the read-budget
    wrap-up) shipped replies that had never been told the turn was held; across real held turns the
    note appeared in ZERO model calls, the replies improvised, and one claimed execution was
    imminent ("the system will now execute this action") while nothing ran. It is now computed once
    per turn and passed as a `reply_directive` to EVERY reply generator (`_grounded_answer`,
    `_answer_subquestions`, the read-budget wrap-up, and every regeneration), landing on the
    answering call's SYSTEM prompt with the rest of the reply contract rather than inside a
    grounding block introduced as "answer FROM this, never mention that you read it". It also rides
    on every latched turn instead of only ones an intent detector flags: the cheap regex prefilter
    does not see "Set that up.", "Book it." or "Do the thing we discussed" as directives at all, so
    gating on it left exactly the turns that needed it most without it. Its hard rule is the honesty
    floor (never say or imply that anything ran, is running, or is about to run); naming the hold
    keeps its explicit permission to skip or soften when the user was plainly just musing.
  - **A genuine release now actually does the work.** When the release judge lifts the hold, the
    turn is a directive by definition (the user just said to stop holding back and act), but the
    work usually lives in the transcript rather than in the short release message, so the
    message-intent net could not see it and the turn ended with one more proposal, or worse a reply
    claiming it had acted. A released turn is now treated as a directive by the same net.
  - **The release judge holds an anaphoric imperative.** "Do the thing we discussed." was being read
    as a release. Its doctrine (LLM judgment, still no keyword lists) now says plainly that an
    imperative pointing back at the work is subject matter, and only words about the HOLD itself
    lift it.
  Evidence: a real-model matrix over 14 phrasings (9 held, 2 read-budget, 3 releases) passes 14/14:
  zero escalations and zero executions on every held turn, the acknowledgment present in 100% of
  reply calls, no reply claiming action, every actionable held reply naming the hold, and every
  genuine release exiting the latch and executing.
- **The brainstorm latch now holds against an imperative.** A conversation latched in
  `execution_mode="brainstorm"` released mid-turn on a plain subject-matter instruction ("create a
  goal called X and add it to my plan"), executed the work, and never gave the user the no-action
  acknowledgment. Verified against real models, not stubs: under the previous doctrine the real
  cheap planner emitted `mode_signal="exit_brainstorm"` for 3 of 8 must-hold phrasings at the
  `fast` tier and 6 of 8 at `balanced`, and the orchestrator honored it. Three changes, together:
  - **The exit is no longer the planner's to give.** While the latch is held, a planner
    `exit_brainstorm` is ignored; the release is decided ONCE per turn by a new dedicated judgment,
    `Orchestrator.judge_brainstorm_release` (one structured call, `MODE_RELEASE_TOOL` /
    `MODE_RELEASE_PROMPT`, at the new `OrchestratorConfig.mode_release_tier`, default `"balanced"`,
    hard-capped by `QAR_MODE_RELEASE_TIMEOUT_SECONDS`, default 8s). The planner runs at the cheap
    planner tier by design, and a cheap model reads any imperative as a request to proceed; this
    decision is too consequential for a judgment riding a call made for something else.
  - **The distinction is stated, not pattern-matched** (still pure LLM judgment: no keyword lists,
    no regex, no trigger phrases). An instruction about the SUBJECT MATTER ("create a goal called
    X", "email her about it", "book it") is how a person thinks out loud, and it is HELD. A release
    is the user speaking to you about the holding itself ("okay go ahead and do it now", "we are
    done brainstorming, act on this", "stop holding off, make it happen"). The same distinction is
    now in the planner's own MODE SIGNAL doctrine.
  - **The fail-safe direction is HOLD.** An unresolvable tier, provider failure, timeout, malformed
    verdict, or plain ambiguity all leave the latch on and produce the no-action acknowledgment.
    Holding costs the user one sentence; acting on a conversation they put on hold cannot be undone.
  Cost is bounded to brainstorm turns: the judge runs only while the latch is held (and only when
  `mode_signals_enabled`), so normal turns make exactly the calls they made before.
- **A queued deployment can no longer deny work it actually did.** Review follow-ups to the
  deferred hand-off, all in the honesty direction (every user-facing word must match what really
  happened this turn):
  - **Inline work is reported, never denied.** The honest-enqueue branch keyed only on "did a
    deferred hand-off come back?", so a queued consumer whose `deep_runners` map lacked (or
    typoed) the reserved `DEFERRED_RUNNER_KEY` fell through the normal wiring, RAN the deep work
    for real, and was then told the work had not been queued, started, or done, with the real
    output discarded. The turn now distinguishes "queued mode was configured but no hand-off came
    back" from "the work actually ran and produced output": a real inline result is folded back
    and reported exactly as an inline turn (plus a WARNING naming the missing key), and the
    not-queued reply is reserved for a turn that genuinely completed nothing.
  - **A hand-off is validated, not assumed.** The deferred short-circuit believed any result with
    `deferred=True, met=True`, ignoring `error` and an empty receipt, so a consumer runner that
    reports `met=True` on a FAILED enqueue was taken at its word and the user was promised a queue
    entry that did not exist. A hand-off is now trusted only when the deployment is in queued mode
    AND the result is deferred, met, error-free and carries a non-empty receipt; anything else
    takes the honest not-queued path.
  - **The reserved runner key is not classifier-selectable.** `DEFERRED_RUNNER_KEY` was still a
    candidate for the deep-runner classifier, so a classifier returning `"deferred"` for an
    ordinary deep turn handed the goal loop a queue receipt to verify as finished work (the
    executor would then mark the task done and report on a receipt). The classifier's choice of
    that key is now rejected (default runner takes the turn); it is reachable only through the
    hand-off's explicit `runner_override`.
  - **Honesty no longer depends on a model call.** If the honest-enqueue regeneration LLM call
    itself threw, the reply silently reverted to the pre-deep draft, which under queued doctrine
    may already claim "I have queued this". On regeneration failure a plain not-queued sentence
    (`NOT_QUEUED_NOTE`) is now appended deterministically, with no model call.
  - **A queue-only wiring can reach its queue.** `_run_deep`'s capability gate ran before
    `runner_override`, so a consumer that registered ONLY the queue runner (no default
    `deep_runner`, no classifier) found the deferred path unreachable. An explicit
    `runner_override` now IS the capability, the deferred escalation nets accept a wired queue as
    capability (`_has_deferred_queue_capability`), and the precondition is documented on
    `deferred_deep_queued`. Every other deep path (planner `deep`, overseer `escalate_deep`, claim
    remediation) still requires real inline capability, since those execute inline.
  - `Orchestrator.synthesize_task_report`'s docstring said its default `tier` was a "worker" tier;
    the default is `"balanced"` and no worker tier exists. Corrected.
  Regression tests in `tests/test_orchestrator.py`.
- **`deferred_deep` wording now matches its synchronous reality.** The planner doctrine and the
  decide-tool schema described `deferred_deep` as queuing a deep task for later, and the status
  line said "Queuing follow-up work", but the implementation has always run that work
  immediately after the answer, synchronously in the same turn (the answer ships first in the
  stream, then the deep run executes and its output is folded back into the final reply). The
  planner description, schema field descriptions, status line (now "Continuing with the
  follow-up work now") and code comments are rewritten to say exactly that. Words only, no
  behavior change; a real consumer-side queue remains a separate future build (the dormant
  `DeepResult.deferred` contract is untouched). A wording regression test in
  `tests/test_orchestrator.py` asserts the queued-for-later phrasing stays gone.
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
  `assembly_timed_out` -- both only when the partial actually carried content (an empty partial
  is named as such and never reported as "used"). An arm is skipped only when a completed arm
  actually holds content: when no completed arm has any (neither finished, or a finished arm
  crashed or came back empty) the hybrid blocks for the missing arm(s) exactly as before (an
  early empty return would read as "assembly found nothing" and poison the shared turn cache),
  so the hard-timeout path, the warm recent-card fallback, and the `TurnCardCache`
  late-recovery contract (a timed-out turn-start future is still recoverable mid-loop) all
  hold. A partial is scoped to the turn-start prompt it rescued: it is never cached as the
  query's completed result, so a later mid-loop read for the same query re-assembles fresh
  (deadline-free) and recovers the FULL fuse instead of being served the partial for the rest
  of the turn. Assemblers that ignore the meta hint, and callers that pass no deadline, behave
  byte-for-byte as before.

- **`cli send` no longer acknowledges tasks that were never enqueued.** Found by live testing:
  `create_task` defaulted to `source="cli"`, which the Quest API rejects (its enum is
  chat / reflection / review), and the client swallowed the 400 into `{}` - so `send` printed a
  confident "I'm looking into it" for a task that would never run. The default source is now
  `"chat"`, `create_task` raises on failure instead of swallowing it, and `send` refuses to ack
  unless the API returned a task id.

### Changed
- **BREAKING (public prompt): `PLANNER_PROMPT` gained a mandatory `{deferred_deep_semantics}`
  format slot.** The queued/inline doctrine sentence is selected per configuration, so a raw
  `PLANNER_PROMPT.format(...)` by an external consumer that does not pass that slot now raises
  `KeyError`. Non-breaking path, and the recommended way to render the prompt from now on: the new
  public `render_planner_prompt(**slots)` fills every slot the caller omits from
  `planner_prompt_defaults()` (the default `deferred_deep_semantics` is the INLINE wording, which
  is what the default configuration actually does), so a future slot cannot break consumers again.
  Both are exported from `quest_ai_runner.core`, alongside `DEFERRED_RUNNER_KEY`.
- **The learned-notes always-recent floor is relevance-gated.** `NoteContextStore.assemble`
  unconditionally included the 2 most recent notes in every turn's context, which bled the
  previous topic into an unrelated next turn. A floored note must now clear a minimal relevance
  bar against the current query, with three deliberately permissive ways through: a note
  learned within the last 5 minutes always passes (a just-given correction is almost certainly
  still in-topic, and style/behavior corrections relate semantically rather than lexically; the
  window is deliberately short -- it covers the just-synced/just-learned case, while a wide
  window would leave most of an active session's notes "fresh" and the gate inert against
  rapid topic switches; tunable via `QAR_NOTE_FLOOR_FRESH_MINUTES`, non-positive disables the
  bypass); a single shared meaningful keyword passes, compared on conservatively stemmed
  candidate sets (dependency-free suffix stripping, longest-suffix-first with a length floor
  and a restored trailing "e" variant, so a trivial inflection like "update" vs "updates",
  "statuses" vs "status", or "used" vs "use" cannot drop an applicable correction while short
  words like "car" stay distinct from "cares" -- ranked selection keeps exact-token scoring);
  and when either
  side yields no keywords the note is kept (cannot judge relevance, so do not drop). Only a
  clearly unrelated, non-fresh note is dropped. When nothing (floor included) relates to the
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
