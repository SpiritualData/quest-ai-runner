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

#### The task document the runner reads

A task is a plain JSON object. The client does not model it with a strict schema: it hands the
document through to the poller/executor as-is, so a backend may carry any extra fields it likes and
they reach a consumer's resolvers untouched. These are the fields the runner itself reads (all
optional but the instruction text):

| Field | Used for |
|---|---|
| `id` (or `task_id`) | the task's identity: claim, report, progress stream |
| `text` (or `title` / `description`) | the instruction to run |
| `goal_id`, `quest_id` | the linked goal/quest, fetched for context and used to resolve a quest folder |
| `conv_id` | the conversation the task was delegated from; live progress and the done report post back into it |
| `card_id` | reserved: forwarded on conversation posts so a backend can thread them |
| `model` | per-task model/tier override (`model_hint`) |
| `rep_preamble` | the persona to run and report as, when the task has no rep of its own (see below) |
| `task_kind` (or `handler`) | routes special kinds, e.g. an autopilot pass |
| `real_time`, `context_request` | the fast lane (see below) |
| `status`, `updated_at`, `scheduled_time` | dedup signature |
| `team_id`, `user_id`, `env_id` | scoping (team lane, related-conversation search, environment pinning) |

`rep_preamble` is a **fallback persona supplied by whoever queued the task**: a cache-stable
persona/system prompt string. The runner uses it as the deep run's persona, and therefore as the
voice of the fold-back done report, only when no AI rep is resolved for the task (a resolved rep's
own persona always wins). Anything that is not a non-empty string is ignored. The case it exists for
is a task deferred out of a live conversation: stamp that conversation's persona on the task and the
report that lands back in the conversation speaks in the same voice as the replies already there.

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

### Fast lane for real-time tasks (cross-environment context requests)

A REAL-TIME task (`"real_time": true` on the task doc) is one its creator flagged as
low-latency-eligible because something is waiting on the answer right now. This is a generic flag
any task-creation path can set -- today the only producer is another environment's quest-context
hub asking THIS runner for local context (a `context_request` task: `{"query", "user_id",
"quest_ids", "visited", "max_chars"}`), but the flag itself is not tied to that task type. Whether a
task carries a `context_request` payload is a separate, execution-routing decision: the runner never
runs the goal loop for one of those -- it assembles context locally via its own `context_assembler`
and reports done.

```
GET /api/assistant-tasks/wait?real_time=true&timeout=<secs>[&team_id=&env_id=]
  -> { "task": {...} | null }
```
Long-poll: the backend BLOCKS (re-checking its store internally, ~0.25-0.5s cadence) until a
real-time task is queued for the caller's env/team, or `timeout` elapses (server-capped at 30s)
with nothing to deliver. `QuestClient.wait_for_interactive` calls this in a tight loop from its own
thread (`Poller._fast_lane_loop`), reconnecting immediately after every return -- empty or not --
so a real-time task is answered close to instantly whenever the runner is up, without the
latency of the normal background scan (`poll_interval_seconds`, default 900s). `QAR_WAIT_CHANNEL=0`
disables this and falls back to a plain short-interval poll:
```
GET /api/assistant-tasks?status=queued&real_time=true[&team_id=&env_id=]
```
via `QuestClient.list_interactive_due`, on `QAR_CONTEXT_POLL_SECONDS` (default 5s; `0` disables the
fast lane entirely).

Reporting a context-request's result uses the same `PATCH .../{id}` as any task, with an OPTIONAL
`result_data` alongside the plain-text `result` (`QuestClient.report_done_with_data`) so a runner
can carry structured extras -- today, card metadata -- without overloading the text field every
other caller reads as-is:
```
PATCH /api/assistant-tasks/{id}    { "status": "done", "result": "...",
                                     "result_data": {"card_metadata": [...]} }
```

`discover_due` (the normal background scan) is also `env_id`-aware now:
```
GET /api/assistant-tasks?status=queued&due_before=<ISO-now>&env_id=<env>
```
so a multi-environment team's runners each discover only their own pinned work (plus any unpinned
task) -- the backend already scoped this; the client/poller just pass `env_id` through.

### Create a goal (the real, typed kind — distinct from a task)

```
POST /api/planning/goals    { "title": "...", "period": "...", "quest_id": "..." (optional), ... }
  -> { "id": "...", "questId": "...", "title": "...", "period": "...", "deadline": "...", ... }
```
A **task** (above) is a unit of AI work to run; a **goal** is a real, period-scoped item with a
deadline that shows up on a quest's plan (or standalone, on the caller's own account, when
`quest_id` is omitted). These are different objects with different endpoints — creating a task
does not create a goal, and vice versa. `quest_id` is optional but preferred: a goal without one
only shows up on the caller's own account, not on any quest's shared plan.

`period` is REQUIRED and must be one of five formats (the deadline is derived from it
server-side): `YYYY-MM-DD` (day), `YYYY_W##` (week, zero-padded), `YYYY_MM` (month, zero-padded),
`YYYY_Q#` (quarter), `YYYY` (year). `QuestClient.create_goal` validates the format client-side
before making the request, so a malformed period fails fast with a clear message instead of a
round trip to get a 400.

Optional fields: `description`, `criteria` (completion criteria), `goal_type`, `parent_goal_id`
(for a sub-goal), `target_value` / `target_unit` (a measurable target), `ai_help` (bool),
`assignee_rep_id`.

Like `create_task`, `create_goal` raises `QuestApiError`/`QuestNotConfigured` on failure instead
of swallowing it — a caller that acknowledges "goal added" must know it actually was. CLI:
`quest-ai-runner create-goal "<title>" [--quest-id ID] [--period P] [--description ...] [...]`.

## Don't hand-roll HTTP

Use `QuestClient` — it covers discover / claim / report / escalate / loop-close / whoami / heartbeat
and is what the `Poller` and `TaskExecutor` call. The runner tests drive a mock client
(`tests/test_runner.py`) so you can verify behavior offline.
