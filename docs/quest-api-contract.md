# Quest API contract

The runner speaks a small, explicit slice of the Quest API. This is implemented in
`quest_ai_runner/runner/quest_client.py` (`QuestClient` + `QuestDecisionSink`). If you run your own
Quest-compatible backend, these are the endpoints it must provide.

## Authentication & identity

```
Authorization: Bearer qsk_<hex>
```

The key **is** the executor identity. The acting identity is derived server-side from the key — you
never pass the actor in a request body. Targets (a decision assignee, etc.) are passed explicitly.

Task **discovery is owner-scoped**: a `GET` returns only tasks in the queue of the user the `qsk_`
key authenticates as. Keep each lane on its own key/owner so lanes stay isolated.

## Endpoints

### Discover due tasks
```
GET /api/assistant-tasks?status=queued&due_before=<ISO-now>
```
Returns queued tasks whose `due` time has arrived. A future `due` is simply not returned yet — this
is how scheduling works without separate plumbing.

### Claim a task
```
PATCH /api/assistant-tasks/{id}    { "status": "in_progress" }
```
The claim step; the backend is the source of truth for who owns a task.

### Report a result
```
PATCH /api/assistant-tasks/{id}    { "status": "done" | "needs_you" | "failed",
                                     "result": "...", "decision_id": "..." (optional) }
```
- `done` — answered, or a deep run that met its goal.
- `failed` — a deep run hit its limit or errored.
- `needs_you` — a human-only step; carries the `decision_id` of the raised request.

The runner never PATCHes `"cancelled"` itself (see Cancellation below): once a task is cancelled
the backend already owns that terminal status, and a PATCH against it returns `409`.

### Cancellation (stop a task while it's in progress)
```
GET /api/assistant-tasks/{id}   -> { ..., "status": "...", "cancel_requested": bool }
```
A human can cancel a task while it is `in_progress` (e.g. `POST
/api/assistant-tasks/{id}/undo`, or a conversation-level stop). The backend sets the task's
`status` to `"cancelled"` and `cancel_requested` to `true`, and rejects any subsequent `PATCH` that
tries to change its status with `409`.

`QuestClient.is_task_cancelled(task_id)` re-fetches the task and reports True when either signal is
set; it is fail-open (any API error returns False, so a transient hiccup never mistakenly stops a
legitimate run). `TaskExecutor` builds a throttled version of this check (at most once per ~15s of
real time) and passes it to `Orchestrator.run(cancel_check=...)`, which polls it at natural loop
boundaries (each plan/gather/replan step, each deep-goal retry attempt) and stops cleanly with
`OrchestratorResult(kind="cancelled")` when it reports True. On a cancelled outcome the executor
does **not** PATCH the task or post a done/failed message into the chat: the backend already set
the terminal status and appends its own "cancelled" chat message, so the executor only posts a
best-effort status note onto the task's own progress stream. See `docs/streaming-and-modes.md` for how
`cancel_check` fits alongside the other `run()` event/streaming parameters.

### Escalate (human-only step)
```
POST /api/teams/{team_id}/decisions    { ..., "default_on_silence": "hold" }
```
Returns a `decision_id`. The executor stamps it onto the task via the `needs_you` report.

### Close the loop
```
GET  /api/teams/decisions/for-user
POST /api/teams/decisions/{id}/resolve
```

### Identity check
```
GET /api/teams/whoami
```
Validates the key and returns the executor identity (`quest-ai-runner --check` uses this).

### AI-rep profile (skill-file sync)
```
GET /api/teams/{team_id}/members/{user_id}/ai-profile
  -> { user_id, display_name, persona, learned_notes: [{id, text, created_at, source, message_id?}], updated_at }
PUT /api/teams/{team_id}/members/{user_id}/ai-profile    { display_name?, persona?, learned_notes? }
POST /api/teams/{team_id}/members/{user_id}/corrections  { correction, message_id? }  -> updated learned_notes
```
The single source of truth for a team AI rep. `QuestClient.get_ai_profile` / `update_ai_profile` /
`add_rep_correction` speak this; `runner.rep_sync` renders the profile into the rep's local Claude
skill file and back, so `sync_rep(...)` keeps the two in sync with one call. The acting identity is
the key's; `user_id` (the rep) is passed explicitly.

### Team-environment heartbeat (capabilities)
The poller posts what the lane can honestly do (`web` / `corpus` / `code`, derived from the wired
adapters) each cycle, so Quest's router only sends work the lane can handle.

## Don't hand-roll HTTP

Use `QuestClient` — it covers discover / claim / report / escalate / loop-close / whoami / heartbeat
and is what the `Poller` and `TaskExecutor` call. The runner tests drive a mock client
(`tests/test_runner.py`) so you can verify behavior offline.
