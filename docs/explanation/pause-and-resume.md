# Pause, resume and replay: pick up where we left off, and watch it again

Status: design only. No code written, no model run. Every "today" claim was read from code in the named files; what was not
read or run is in section 10. Related: `deep-run-continuation.md`, `live-work-trace.md` (the Trail),
`incremental-replies-and-asks.md`, `conversation-cards.md`.

## 1. The requirement

After a pause (stops typing, closes the app, pauses a run, loses connection) the AI resumes as if nothing happened, like an
interactive Claude Code session.

1. A temporary in-memory cache holds the current working state, stays while the person keeps going, is bounded by TOTAL
   size across all users, and evicts sensibly.
2. If the entry is gone, the system resumes cold from durable records with no visible breakage.
3. It works for deep runs, other task types and AHOHS solutions (anything that runs through the hub).
4. The person can REPLAY and RESUME with an explicit action, not only by typing another message.

## 2. What exists today (read from code)

**Claude Code session resume (deep runs).** The hub stream runner (`hubofhubs/deep/stream_runner.py`) launches
`--session-id <new>` or `--resume <id>`; a pause is an interrupt ending the run as `DeepResult.paused` with its
`session_id`. Turn-limit continuation is in `deep-run-continuation.md`. A task may carry `resume_session_id`; resume is best
effort (an unknown id exits 1 and the runner starts cold), and `session_replay_tokens` skips a resume that would overflow
the window. A person's pause of a queued task is `PausesMixin.hand_back_paused_task` (`runner/executor_pauses.py`): back to
`queued` with the session, `pause_requested` kept until `POST /api/assistant-tasks/{id}/resume`, unclaimed steering messages
delivered by the resumed run. Usage-limit and outage pauses reuse the put-back; `runner/auto_followup.py` runs up to 3
follow-ups. Weakness: the session lives in CLI files on one machine and project directory, so another lane process, a
pruned session, or an answer turn (no session) gets nothing.

**Context engine.** `core/recent_context.py` (`FileRecentContextStore`: scopes `conv:`, `quest:`, `global`; 8 turns and 24
cards per conversation; 14-day prune; lexical relevance gate; item usage memory) is used at every entry point and per
subgoal on the task path. `core/turn_context_store.py` makes past turns into cards. `TurnCardCache`
(`hubofhubs/orchestrate/turn.py`) caches assembly for one turn and dies with it. History is lossless and relevance-selected.
This remembers WHAT WAS DISCUSSED; it holds no plan, partial output or tool results.

**Stream reconnect, events, caches.** Backend `stream_registry.py` (`InFlightStreamRegistry`, keyed by conversation, user,
message hash, short TTL) re-attaches a retry to a running stream: a dropped socket, not a return an hour later. Trail events
come from `core/work_trace.py` (`TurnTrace`), are projected by `hubofhubs/ports/trace.py` (`project`: bounded text, URLs to
domains, `viewer_may_see_titles`) and persisted by the backend; interims and asks are built behind a flag. The terminal's
`_DeepRunTracker` (`interactive_session_tracker.py`) is an in-memory view model only. Caches: hub `WebCache` (LRU 512
entries, per-entry TTL, disk tier: entry-count bounded, no byte budget, no user namespace), `CachedDbAdapter`, the hub
tool-call ledger (per turn).

## 3. What a pause loses today

Durable and kept: conversation, cards, recent items, Trail events, interims, asks. Kept only in one Claude session on one
machine: plan, gathered context, tool results, partial text. Lost for answer turns and on any new process: plan and step
status, the turn's assembled context, tool results (paid again), unsaved partial output, verdict, attempt count, budget used.

Failure modes: stale state; cross-user leak (`WebCache` is keyed by query only; team threads add a privacy axis); unbounded
growth (entry-count caps ignore size and user count); double resume (tap plus auto follow-up, or two lanes; guarded for
queued tasks by the claim and `pause_requested`, not for answer turns); fake continuity; repaid work (the 2026-09-09 incident).

## 4. Design: one Run Log, one Working State, one port

### 4.1 Two records, deliberately different

**Run Log (durable, append-only, source of truth).** Per run, in the backend database beside the run: Trail events (already
projected), interim message and ask ids (references), tool results by id, checkpoints, state changes (`started`, `paused`,
`resumed`, `replayed`, `finished`). Each event has `seq`, `ts`, `kind`, `v` (schema version), `payload`. Size-bounded:
payload cap per event, event cap per run, total cap per user; over a cap, low-value events (heartbeats, repeated status) are
compacted into a count and the log says so, never silently dropped. Redacted on write by the Trail projector rules plus the
hub secret redaction (`act/secret_redaction`): no keys, tokens, cookies, raw prompts or hidden reasoning. Retention follows
the run, with a hard TTL (proposal: 30 days) and user delete.

**Working State (cache, rebuildable, an optimization).** A small versioned JSON object written at checkpoints.
Fields: `v`, `run_id`, `user_ns`, `scope_ns`, `seq_at_checkpoint`, `created_at`, `goal_ref`; `plan` (step id, status, one
line); `attempt`; `budget_used`; `verdict` (met, reason, next action, blocker); `context_refs` (card and item ids);
`tool_result_ids`; `partial_output_ref` (file paths or message ids plus a bounded text tail); `session_ref`; `pending` (ask
and steering ids); `fingerprint` (hash of what makes it stale: quest revision, card store revision, file mtimes).

No secrets, no raw tool payloads (ids only), nothing of another user, size cap per record (proposal 16 KB; larger is refused
and the run falls back to cold, which is correct). Anything reconstructible is NOT stored: conversation text, card bodies,
file contents, decision text; they are re-read by id. The cache may vanish at any moment; the Run Log may not, and Working
State is always derivable, less precisely, from the Run Log plus durable records. That derivation is the cold path.
### 4.2 The cache

A hub port `WorkingStateStore`: `get(ns, key)`, `put(ns, key, value, ttl)`, `touch`, `evict`, `evict_namespace`, `stats`,
with `ns = (user_id, run_id)`. The store itself rejects a `get` whose caller differs from the entry's user.

- **Arm A, in-process LRU (simplest, ship first).** `OrderedDict` per process. One byte budget across all users (proposal
  64 MB, a setting), measured on the serialized record at `put`. Idle TTL (proposal 2 hours, refreshed by `touch` while
  the chat or run is active). Per-user fairness cap (proposal 25% of the budget). Eviction order when over budget: expired
  first; then settled runs; then least recently used paused runs, taking from the user furthest over their share first;
  never the entry of a run executing now (pinned until it pauses or ends).
- **Arm B, shared backend (what a multi-process lane needs).** The same port over a database collection with a TTL index.
  A lane process that did not pause the run misses arm A by construction, so cross-process warm resume needs B; with one
  process per lane and sticky routing, A is enough. End state: A in front of B. The first slice ships A and proves the
  cold path, which B needs anyway.
- A corrupt, wrong-version or oversized entry is a miss and is evicted. A failed `put` never fails the run.
### 4.3 Tool-result cache (no repeated paid calls)

Expensive calls (web search, page fetch, scholarly search, embeddings) are recorded as `{tool_result_id, tool, args_hash,
scope_ns, fetched_at, ttl, size, body_ref}`, keyed by `(user_ns or "public", tool, normalized args)`. A result is shared
across users only when the tool is declared shareable and the args hold nothing from the person's records. Bodies live in a
byte-budgeted store of their own (proposal 128 MB); the Run Log keeps the id and a bounded summary. On resume, recorded
calls are answered by id: no network, no spend. An expired result on a time-sensitive tool is re-run and logged as
"refreshed", never silently substituted. `WebCache` is the starting point; it needs a byte budget, a user namespace and
the Run Log id.

## 5. The resume protocol

**Triggers** (all call one `resume_run(run_id, caller)`): the explicit action (5.5); the next message in a paused
conversation; the executor reclaiming a paused or limit-held task; an automatic follow-up.

1. **Guard.** Take a resume lease: atomic compare-and-set of the run state `paused` to `resuming` with owner id and expiry.
   A lost race returns the winner's stream (the stream-registry re-attach). This removes double resume, including tap versus
   auto follow-up.
2. **Authorize.** Owner, or a team member the thread allows; the same rule as the existing task resume endpoint.
3. **Look up.** Hit, same `v`, valid `fingerprint`: WARM. Anything else: COLD.
4. **Warm.** Restore plan, attempt, budget, verdict; reattach the Claude session if `session_ref` exists and
   `session_replay_tokens` fits; load cards by id; tool results by id; continue at the first unfinished step. If the
   fingerprint shows the quest, files or decisions changed since the checkpoint, give the AI a short "what changed" note
   instead of pretending nothing did.
5. **Cold.** Build a brief from durable data only, in order: the task row or turn (goal and standard), the Run Log (what
   finished, tool results by id, interims sent, asks and answers), the last settled partial output, conversation and
   recent context by relevance. Cached tool results are reused, missing ones re-run and logged. The Claude session is resumed
   if it exists, else a fresh one gets the brief. Steps the log marks done are listed as done with evidence ids and not redone.
6. **Record** `resumed {path, reason}` in the Run Log, then keep appending.
7. **Version mismatch.** Older Working State `v` is migrated if a migration exists, else cold and evicted. An unknown-`v`
   Run Log event is skipped in replay with a visible "step not shown" marker.

### 5.3 What the AI may and may not claim

- The path (warm or cold) is chosen by code from the cache result, never by the model or from wording.
- Warm: it continues its own work, claiming nothing absent from state, log or records.
- Cold: it may say what the record shows ("the earlier searches are kept; I did not redo them") and continue. It may NOT
  say it remembered or was "in the middle of" something, or describe reasoning not in the log or conversation. The cold
  brief says so plainly and forbids em dashes in anything the person reads.
- Neither path claims work done without an evidence id in the log; a resumed answer must agree with the pre-pause state.
### 5.4 Visible behavior

- On pause: the Trail settles to a calm "Paused" state with what is done and the Resume action. Nothing that sounds like
  a failure.
- Return, warm: no announcement; the run continues in the same thread.
- Return, cold: one short line, only when it changes what the person should expect, for example "Picked up from the saved
  record: the earlier searches are kept and the draft is being rebuilt." Nothing if the rebuild changed nothing material.
- Never "picked up where we left off" unless the path was warm or the cold rebuild covered every finished step. Never
  mention caches, TTLs or sessions. If something relevant changed during the pause, say that once.
### 5.5 Replay, and an explicit Resume/Replay action

Replay has two meanings, both driven by the Run Log.

**User-facing replay.** A "Replay" control on a settled or paused Trail re-plays the recorded events step by step at the
recorded pace (long gaps shortened, speed control, skip to end). It reads stored, already redacted events only: no tools,
no model, no cost, no change to the run. A teammate who joins late can replay too, under the shared-thread privacy rule:
replay is rendered by the same projector with the VIEWER's `viewer_may_see_titles`, so a member sees only what they could
have seen live; events drawn from another member's private records show as counts, not content.

**Engine-side replay.** "Re-run from the last checkpoint": load Working State (or rebuild cold) and run the remaining steps,
answering recorded tool calls from the cache by id so no paid call repeats. Recorded calls return identical bodies, done
steps are skipped, the prompt version is pinned; model text is not promised identical.

**One-tap action and endpoint.** A paused or interrupted run shows a primary Resume button and a secondary Replay link; a
finished run shows Replay (and Continue if unmet). Continuing never needs typing "continue" or sending a message.
- `POST /api/quest-ai/runs/{run_id}/resume`: idempotent; first event `resumed {path}`. `GET
  /api/quest-ai/runs/{run_id}/replay?from=seq`: paged, redacted events. Queued tasks keep
  `POST /api/assistant-tasks/{id}/resume`; all call the same function.
- Warm: continues at once, Trail picks up, no message added. Cold: the 5.4 line when material, then the Trail continues. If
  the log is gone past retention, the button becomes "Start again from the record" with one honest sentence and starts a new
  run seeded with the conversation.

## 6. Hub port versus per-feature caches

Three caches exist (`WebCache`, `CachedDbAdapter`, `TurnCardCache`), bounded differently, none with a user namespace or byte
budget; Claude session resume is a hidden fourth. Recommendation: a hub seat `keep-working-state` with `WorkingStateStore`
and a small `RunLog` Protocol (`append`, `read(from_seq)`, `compact`); a solution declares `checkpoint()` and `restore()`.
One byte budget and one privacy check need one store, and AHOHS solutions get pause and resume with no QAR code. Cost: a
port to agree on; ship the Protocol shape inside QAR first, move it to the hub when a second consumer appears.

## 7. Composition

- **Trail:** the Run Log stores projected Trail events and replay renders them; `paused` and `resumed` are new event kinds
  and the Trail gets one more state, "Paused".
- **Interims and asks:** stored as references. Open asks stay open and are not re-asked (deduped by gap id); an answered
  ask enters the brief as the person's decision; no duplicate interim is posted on resume.
- **Solo and team:** Working State sits under the starter's namespace. A teammate resuming needs the thread's permission,
  uses the team scope for cards, and does not inherit the starter's private tool results unless the thread allows. Team
  threads accept only closing message kinds today, so a paused team run shows only on the Trail.
- **Deep runs and queued tasks:** the session resume and put-back remain the engine; the port adds plan, verdict, budget,
  tool results and cross-lane reach. Auto follow-up calls `resume_run`, so its lease blocks a manual tap too.

## 8. ESOI framing

- **Problem:** proposed `prob-pause-loses-the-thread`: after a pause the AI repeats work, pays twice, loses its place or
  pretends to remember, and the person must type to restart. Related: `prob-long-run-silent-until-the-end`.
- **Seat:** new hub seat `keep-working-state` (store, run log, resume); first consumers `run-deep-work`, `show-progress`,
  `answer-a-turn`. Implementations: `inprocess-lru-state`, `db-backed-state`, `run-log-replay`.
- **Arms:** A0 today (session resume for deep runs, recent-context cards, nothing else); A1 Run Log plus cold rebuild, no
  cache; A2 A1 plus in-process LRU and tool-result cache; A3 A2 plus database tier; A4 A2 without byte budget and fairness
  cap (shows what the bounds cost). Replay arms: R0 none; R1 user-facing; R2 user-facing plus engine-side.
- **Ideals:** never repeat paid work; never claim memory you lack; never leak across users; the person never has to type
  to continue; bounded cost at any user count.
- **Metric** (proposed `metric-resume-truthful-cheap-and-visible`):
  `offline [sample=full | real=no | gate=all-pass | when=scripted logs and fake tools through store, resume function, lease
  and projector; hit, miss, expiry, eviction, version mismatch, double resume, two users] > one [sample=sample:1 | real=yes
  | gate=all-pass | when=one real Haiku turn through claude_cli (free), paused mid-run, resumed warm, hand read] > ten
  [sample=sample:10 | real=yes | gate=all-pass | when=half resumed warm, half cold by evicting the entry] > thirty
  [sample=sample:30 | real=yes | gate=thresholds | when=final tier, same free route, so cost is usage headroom and wall
  clock]`.
- **Checks** (from records, never reply wording):
  1. Warm exactness: continues at the first unfinished step; no logged step redone.
  2. Consistency: the resumed answer agrees with pre-pause state by structural diff of claims against the log (a blinded
     different-model judge is a reading, not the gate).
  3. No repeated spend: zero repeated paid calls on warm resume (by tool-result id); on cold, only absent or expired ids.
  4. No cross-user leakage: two users on one process, planted canary strings; no get, tool hit, replay or log read returns
     the other's data, including after eviction and team-thread resume.
  5. Cold honesty: every factual claim maps to an evidence id; no memory claims; the cold line appears iff something
     material was redone.
  6. Bounds: total bytes never exceed the budget under many-user load; per-user cap holds; a running run is never evicted.
  7. Double resume: concurrent calls produce one run; the second gets the first's stream.
  8. Replay fidelity: a replay shows the same steps and counts as the live run (steps, sources, papers, records, messages,
     asks) with the viewer's redaction, and makes zero tool or model calls.
  9. No harm: met rate and tokens per met turn no worse than A0 within noise.
- **Free evals from recordings:** real Haiku turns are recorded once as Run Logs (with tool results), then replayed into the
  Trail UI, the projector and the scorer with no model or network. They become the offline tier's fixtures for later changes.

## 9. Build plan (smallest first)

**Phase 0, smallest slice (about 1 to 2 days):** Run Log writer and replay read for answer turns and deep runs, reusing the
persisted Trail events; `paused` and `resumed` kinds; replay endpoint; offline replay-fidelity tests (check 8) from
scripted logs. Useful alone (late joiners, eval fixtures); changes no resume behavior. Files: hub `ports/trace.py`, new
`ports/run_log.py`; QAR `core/work_trace.py`, new `core/run_log.py`; backend run-log storage and replay endpoint.
**Phase 1:** `WorkingStateStore` Protocol and the in-process LRU arm; offline bounds and leakage tests (checks 4, 6). Files:
hub `ports/working_state.py`, `orchestrate/working_state_lru.py`; QAR `core/working_state.py`.
**Phase 2:** `resume_run` with lease, warm and cold paths, queued tasks first; checkpoints at attempt boundaries and on
pause; cold brief builder. Files: QAR `runner/executor_pauses.py`, `runner/executor.py`, `core/orchestrator_deep_run.py`,
new `core/resume.py`; backend lease field and the resume endpoint.
**Phase 3:** tool-result cache (byte budget, user namespace, by-id replay) under the hub web and paper tools; check 3. Files:
hub `websearch/cache.py`; QAR `know_the_world/web_search_backends.py`.
**Phase 4:** chat answer turns and terminal checkpoint and resume; frontend Resume and Replay controls, Paused Trail state,
the cold line (all locales). Files: quest-frontend Trail components and chat adapter, backend stream endpoint, QAR
`interactive_session_persist.py`, `interactive_session_tracker.py`.
**Phase 5:** database-backed shared tier; engine-side replay; ESOI rows after the owner confirms; first real Haiku
recordings (one, then ten), then the thirty run. Team-thread replay last, counts-only until reviewed.

## 10. Risks and unverified items

- Nothing here was executed: no cost or latency number exists. "Free" Haiku runs rely on the subscription CLI route other
  QAR evals use.
- Unverified: locating a session file from another process; completeness of persisted Trail events for replay; real Working
  State size (16 KB is a guess); the lane topology, which decides arm A versus B.
- Privacy: team replay is the sharpest risk; projector rules for another viewer's private records are not yet written.
- Staleness: the fingerprint may miss changes; the AI verifies pre-pause facts older than the TTL. Determinism is partial:
  tests assert structure, not text. Phase 0 must follow the Trail and interim-reply contracts, not fork them.
- Open decision: replay first (Phase 0) or resume first (Phase 2). Recommended: Phase 0, it creates the fixtures.
