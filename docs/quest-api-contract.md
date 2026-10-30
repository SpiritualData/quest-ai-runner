# Quest API contract

The runner speaks a small, explicit slice of the Quest API. This is implemented in
`quest_ai_runner/runner/quest_client.py` (`QuestClient` + `QuestDecisionSink`). If you run your own
Quest-compatible backend, these are the endpoints it must provide.

## Authentication & identity

```
Authorization: Bearer qsk_<hex>
```

The key **is** the executor identity. The acting identity is derived server-side from the key, so you
never pass the actor in a request body. Targets (a decision assignee, etc.) are passed explicitly.

Task **discovery is owner-scoped**: a `GET` returns only tasks in the queue of the user the `qsk_`
key authenticates as. Keep each lane on its own key/owner so lanes stay isolated.

## The client contract

`QuestClient` is an HTTP client for this API, and it follows one contract so a caller never has to guess what a
return value means. The in-process (embedded) access point and this one share the same operations; the client's job is to be a
good client of them.

### Writes return a typed result or raise, never an empty value

Every write that changes a person's data (goals, notes, asks, quest fields, strategies, habits, tasks, email, experiments,
profile and context entries) returns a `WriteResult` or raises `QuestWriteError`. It never returns `{}`, `[]` or `False`.

| `status` | Meaning | Returned or raised |
|---|---|---|
| `applied` | the change was made | returned |
| `held` | Quest took it but it waits for a person's approval (HTTP 202, `applied: false`, `pending_approval`, `pending`); nothing changed yet. `id` is the ask that holds it | returned |
| `noop` | it already existed or already held (`already_existed`, `duplicate`) | returned |
| `refused` | Quest said no (a 4xx other than 429): not allowed, invalid, a conflict. The message is Quest's reason | raised |
| `failed` | it did not happen: a 429 (`retry_after` is set), a 5xx, or Quest could not be reached (nothing was sent) | raised |
| `unknown` | the request was sent and no answer came back (a read timeout, a reset after sending): it MAY have been applied. Look before trying again | raised |

A `WriteResult` carries `status`, `id`, `detail` (Quest's own words) and `data` (the raw answer), and reads like the answer it wraps
(`get`, `[]`, `in`, `==`). `QuestWriteError` subclasses `QuestApiError`, so an existing `except QuestApiError` still catches it, and carries
`write_status`, `status` (the HTTP status when there was one), `retry_after`, `outcome_unknown` and `detail`. A held, refused, failed or unknown write
is never done: the tools turn each into a receipt that says so, and the answer step reports only an `applied` receipt as done
(`docs/tools.md`).

Two kinds of call keep a different rule, on purpose:

* **Terminal task reports** (`report_done`, `report_failed`, `report_needs_you`, `report_incomplete`) never raise, because the lane must go on to its
  next task. They return a `WriteResult` that is falsy when the report did not land (`refused`, `failed` or `unknown`, with the reason in `detail`).
* **Reads where absence is normal** (`list_quests`, `get_quest`, `list_quest_notes`, `get_goal`, the reflection and insight reads, ...) return an empty value
  on a failure and log the reason, because the caller treats "nothing there" as an ordinary case. Reads that must tell "none" from "could not read"
  raise instead (`list_experiments`, `get_metric_card`) or return `None` (`list_asks`, `list_intents`). Task-lifecycle bookkeeping that is
  fail-open by design (`claim`, `is_task_cancelled`, `report_progress`, the heartbeat) keeps returning `None`, `False` or `{}`.

### Retry, backoff and idempotency

* A GET is retried up to `QAR_QUEST_READ_RETRIES` times (default 2, at most 5) on a transport error, a 429 or a 5xx, waiting 0.5 s, 1 s, 2 s ... capped at
  `QAR_QUEST_RETRY_MAX_DELAY_SECONDS` (default 30), with jitter. A 4xx other than 429 is a final answer and is never retried. `Retry-After` (seconds or an HTTP
  date) is honoured; one longer than the cap is reported on the error (`retry_after`) instead of waited out. The long-poll wait channel and the capability probe are never retried.
* Goal, note and ask creation (`POST /api/planning/goals`, `POST /api/quests/{id}/notes`, `POST /api/teams/{team}/decisions`) send an
  `Idempotency-Key` header: a fresh UUID per logical call, reused by every attempt of that call (pass `idempotency_key=` to choose it). A server that honours the key
  must return the first result for a repeat of the same key instead of creating a second record.
* A write is retried only when it carries a key AND write retry is on. `QAR_QUEST_WRITE_RETRY` is `auto` (the default), `on` or `off`. `auto` turns it on only when the
  backend lists `idempotency-key` under `capabilities` (see below); `on` forces it; `off` forbids it. A backend that ignores the header would make a second record
  per retry, so with no evidence of support a write is attempted once and a timeout is reported as `unknown`. Email sends, task creation and every other write carry no key and are
  never retried.
* The request timeout is `QAR_QUEST_TIMEOUT_SECONDS` (default 30).

### Version skew: the capability probe

On the first write that carries a key (under `auto`), the client reads `GET /api/health` once, caches the answer and looks for a `capabilities` list
(at the top of the body or inside `data`). It reads nothing else from the server about its version. A backend that lists none, answers an error or does not
answer is treated as supporting nothing optional: the client logs one line at INFO ("does not advertise idempotent writes; write retry is off") and carries on. It is never an error.
A backend advertises support by adding `"idempotency-key"` to that list.

## Endpoints

### Discover due tasks
```
GET /api/assistant-tasks?status=queued&due_before=<ISO-now>
```
Returns queued tasks whose `due` DATE has arrived: a superset, not an exact answer. The backend
compares only the date portion of `due_before` against `scheduled_date` and never reads
`scheduled_time`, so a task set for 06:30 is returned from the instant that calendar date begins in
UTC. West of UTC that is the previous afternoon (17:00 in US/Pacific), which is how a daily 06:30
brief came to run the evening before, dated for the wrong day, burning the occurrence its real slot
needed.

The runner closes that gap on its side: `Poller._due_now_locally` narrows the returned set to what
the LOCAL wall clock says has actually arrived (missing `scheduled_time` means midnight, an
unscheduled task is always due), and holding one back is lossless because it stays `queued` for a
later scan. **So do not read a returned task as "due now"**. It is due today, and the hour is the
runner's to enforce. Sending a local-time `due_before` instead would not fix it either: the
comparison would still drop the clock. The task document carries no timezone of its own, so a
runner in a different tz than the schedule's author remains a real limitation.

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
| `related_goal_id` | optional: a SPECIFIC goal inside a quest that this task is scoped to (distinct from `goal_id`, which holds the QUEST's own id on a quest-scoped task); when present, the goal-context fetch resolves this goal using `goal_id` as its quest id. Absent = unchanged single-id behavior. |
| `conv_id` | the conversation the task was delegated from; live progress and the done report post back into it |
| `card_id` | reserved: forwarded on conversation posts so a backend can thread them |
| `model` | per-task model/tier override (`model_hint`) |
| `rep_preamble` | the persona to run and report as, when the task has no rep of its own (see below) |
| `task_kind` (or `handler`) | routes special kinds, e.g. an autopilot pass |
| `real_time`, `context_request` | the fast lane (see below) |
| `status`, `updated_at`, `scheduled_time` | dedup signature |
| `team_id`, `user_id`, `env_id` | scoping (team lane, related-conversation search, environment pinning) |

On the task LISTING and WAIT routes, the `team_id` query param takes either a single team id or a
comma-separated list of them (`team_id=team_a,team_b`), which matches any of those teams. That is
what lets ONE lane serve several teams (`RunnerConfig.team_ids` / `QUEST_TEAM_IDS`) with one
request rather than one process per team. A single value's query string is unchanged, so a
single-team lane is byte-identical to before the list form existed.

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
- `done`: answered, or a deep run that met its goal.
- `failed`: a deep run hit its limit or errored.
- `needs_you`: a human-only step; carries the `decision_id` of the raised request.
- `incomplete`: the run worked but did not reach its goal (or could not be verified as having reached it).

Every terminal report may carry an OPTIONAL `result_data` object next to `result`. `done` stamps
`{"outcome": "done" | "nothing_done"}`. When the run has a metric result (what the relevant metric was before and after
its change, kept by the goal loop from its ledger, never written by a model), every terminal status adds
`"metrics": [...]` and, when a change ended with none recorded, `"metric_missing": true`; the entry shape is in
[metric-results.md](metric-results.md). A run with neither sends no `result_data` key beyond the outcome stamp, and a
backend that refuses the key (422) gets the same report without it. The live feed carries the same facts on the finished
goal's milestone post: `POST /api/assistant-tasks/{id}/progress` with `data: {"metrics": [...], "metric_missing": true}`.

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
POST /api/teams/{team_id}/decisions
  { "kind": "approve", "summary": "...", "quest_id": "...", "assigned_to_user_id": "...",
    "default_on_silence": "hold", "deadline": "2026-09-25T18:00:00+00:00", "review_url": "https://...", "executable": {...} }
```
Returns a `decision_id`. The executor stamps it onto the task via the `needs_you` report.

- `kind` is 1-64 chars, `^[a-z0-9_:-]+$` (e.g. `approve`, `explicit:spend`, `notice:prod-ops`).
  `QuestClient.create_decision` validates this client-side and falls back to `approve` on a bad
  value, so a malformed/hallucinated kind never loses the whole escalation to a rejected request.
- `review_url` is optional: one http(s) URL the person should open to decide (a draft, preview, doc,
  PR). The planner sets it via `PlanDecision.confirm_review_url` (`Escalation.review_url`); Quest
  rejects any non-http(s) value and the app shows it as a prominent button on the ask.
- `deadline` is optional, ISO 8601. The planner can specify one as a relative form ("in 48h", "in
  2 days") via `PlanDecision.confirm_deadline`; `core.adapters_values.parse_deadline` turns that into the
  real datetime sent here. With no deadline given anywhere, `QuestDecisionSink` still applies
  `QAR_DECISION_DEFAULT_DEADLINE_HOURS` if the deployment set it; otherwise no deadline is sent.
- `default_on_silence` is `"hold"` (default; the decision just sits open past its deadline) or
  `"proceed"` (the backend auto-resolves it at the deadline and the work is treated as approved).
- `assigned_to_user_id` is **never left empty**. `QuestDecisionSink.resolve_assignee` tries, in
  order: the `Escalation.assignee` the caller gave, the sink's configured
  `default_assignee_user_id` (`QAR_DECISION_ASSIGNEE`), then the quest's own owner (`GET
  /api/teams/{team_id}/quests/{quest_id}` -> `owner_user_ids[0]`). If none of those resolve, the
  sink raises rather than POSTing an unaddressed decision that would sit in nobody's queue.

### Read a quest's decisions (context + dedup)
```
GET /api/teams/decisions/for-quest?quest_id=...
  -> [ { decision_id, status, summary, kind, quest_id, resolution?, response_text?,
         resolved_by_name?, auto_resolved?, overdue? }, ... ]
```
Bare list, open **and** resolved, no server-side status filter. `QuestClient.list_decisions_for_quest`
returns it as-is; `list_open_decisions_for_quest` narrows to `status == "open"` (used by the
existing-open-decision dedup in `QuestDecisionSink._existing_open_decision`, and by Autopilot's
HOLD gate). `QuestRetrievalAdapter` renders both halves into quest context (open first, then the
most recent resolved ones, capped at 10) so a rep sees what has already been asked and answered on
this quest, instead of re-raising a duplicate or re-asking something already settled.

### Close the loop
```
GET  /api/teams/decisions/for-user
POST /api/teams/decisions/{id}/resolve
```

### The QAR-ESCALATED marker (deep/subprocess workers)

A worker spawned as a separate process (`SubprocessGoalRunner`'s `claude -p`) has no direct call
into this process's `EscalationSink` -- if its own instructions have it raise a decision directly
against this API (using its own inherited `QUEST_API_KEY`), it reports the resulting `decision_id`
back the only way it can: printing `QAR-ESCALATED: <decision_id>` in its final output.
`core.goal_runner.extract_escalation_id` matches the marker **anywhere within a line**, not only
at its start, and takes the first token after it (trailing punctuation stripped) -- a worker that
leads into it with other text ("Note: QAR-ESCALATED: dec_123") or a bullet point is still caught.

If the marker is missing or garbled entirely, the run would otherwise close done with an orphaned
open decision nobody's task links to. `Orchestrator._run_deep` guards against exactly that: it
snapshots the quest's open decision ids (via the optional `EscalationSink.open_decision_ids_for_quest`
capability) before each deep attempt, and if the attempt's result carries no `decision_id`, diffs
the snapshot again afterward. A new id that appeared is recovered and attached, and the run pauses
on it instead of reporting done. This is best-effort (an `EscalationSink` that implements only
`escalate` simply has no recovery capability, and the probe is silently a no-op).

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

### Action catalog (tools and connectors as workflow step actions)
`QuestClient.publish_action_catalog(owner_type, owner_id, publisher_id, actions, env_id=, publisher_label=)`
-> `PUT /api/{teams|orgs}/{id}/action-catalogs/{publisher_id}` (member of that team or org). Sent from
the heartbeat when the list changed or every 4 hours; see `docs/tools.md` ("Offering tools and
connectors to Quest Workflows"). A task for a published action arrives with `requested_action`
(`{publisher, kind: "tool"|"connector_operation", name, action_id, args}`) and is run directly.

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

### Create a goal (the real, typed kind, distinct from a task)

```
POST /api/planning/goals    { "title": "...", "period": "...", "quest_id": "..." (optional), ... }
  -> { "id": "...", "questId": "...", "title": "...", "period": "...", "deadline": "...", ... }
```
A **task** (above) is a unit of AI work to run; a **goal** is a real, period-scoped item with a
deadline that shows up on a quest's plan (or standalone, on the caller's own account, when
`quest_id` is omitted). These are different objects with different endpoints, so creating a task
does not create a goal, and vice versa. `quest_id` is optional but preferred: a goal without one
only shows up on the caller's own account, not on any quest's shared plan.

`period` is REQUIRED and must be one of five formats (the deadline is derived from it
server-side): `YYYY-MM-DD` (day), `YYYY_W##` (week, zero-padded), `YYYY_MM` (month, zero-padded),
`YYYY_Q#` (quarter), `YYYY` (year). `QuestClient.create_goal` validates the format client-side
before making the request, so a malformed period fails fast with a clear message instead of a
round trip to get a 400.

A goal's time of day is not a `scheduled_time` field: `PUT /api/planning/goals/{id}` accepts but ignores
it. The time is the goal's `scheduled_start`, a local ISO datetime without a zone (`2026-10-26T18:35:00`),
sent with `scheduled_date`; the daily plan renders it as `scheduled_time` ("6:35 PM"), and a day-scoped
goal's period and deadline follow the date. `QuestClient.schedule_goal(goal_id, date=, time=)` sends both
(`YYYY-MM-DD` and 24-hour `HH:MM`, validated client-side) and raises on failure.

Optional fields: `description`, `criteria` (completion criteria), `goal_type`, `parent_goal_id`
(for a sub-goal), `target_value` / `target_unit` (a measurable target), `assigned_to_user_id`
(assign the goal, at creation, to a HUMAN member of `quest_id` -- a Quest user_id, distinct from
`assignee_rep_id` below, which names an AI persona, not a person). Authorized the same way as the
backend's separate assign-after-creation endpoint: the caller must be able to edit the quest, and
the assignee must already be a member of it, or the whole create is refused with no goal left
behind. Requires `quest_id` -- a standalone goal has no quest membership to check the assignee
against.

`ai_help` and `assignee_rep_id` are **gone from a goal** on current Quest backends, and nothing in
this runner sends them any more. A goal is the plan that work serves, never a unit of AI work and
never an assignment to a character: what an autopilot run produces is defined by the persona's
standing instructions, and the day roster decides who works. `QuestClient.create_goal` still
accepts both keywords so a caller aimed at an older backend keeps working; a current backend
ignores them.

There is **no attribution field**: the endpoint's request model has none, ignores unknown keys, and
stamps every goal it creates `source="user"` server-side. So a caller cannot mark a goal as
AI-proposed through this route, and passing something like `created_by` looks like attribution
while carrying none. Autopilot instead surfaces its proposal as a task the person accepts or
rejects (`runner/autopilot.py`, `_maybe_create_goal`), which is where the "an AI proposed this"
signal actually lives.

Like `create_task`, `create_goal` raises `QuestApiError`/`QuestNotConfigured` on failure instead
of swallowing it: a caller that acknowledges "goal added" must know it actually was. CLI:
`quest-ai-runner create-goal "<title>" [--quest-id ID] [--period P] [--description ...] [...]`.

### Read the person's own reflections

```
GET /api/daily-plan/today[?date=YYYY-MM-DD]     (get_daily_reflection)
  -> { "has_plan": true, "plan_id": "...", "date": "YYYY-MM-DD",
       "yesterday_review": "...", "today_plan": "...", "goals_created": 3 }

GET /api/period-review/{week|month|quarter|year}/current[?use_previous=true&timezone=...]
  -> { "stats": {...}, "review": { "has_review": true, "reflection_past": "...",
                                   "reflection_future": "...", ... } }        (get_period_reflection)
```

Both are **user-scoped**: they authenticate as the caller and take no team id and no quest id.
`yesterday_review` is what the person wrote about how the previous day went (they write it while
planning the day named in `date`); `reflection_past` / `reflection_future` are the same two
questions asked of a whole period. `get_period_reflection` returns the `review` block only. The
`stats` half of the response is a separate, much larger concern, and this method exists to read what
the *person* wrote. A missing entry (`has_plan: false`) or an unsubmitted review (`has_review:
false`) is an ordinary state, not an error: both methods return `{}` or the bare flag rather than
raising, because "they have not written one" is a true and useful answer.

`runner/reflections.py` composes both into one `ReflectionContext`:

```python
from quest_ai_runner.runner.reflections import collect_reflections

ctx = collect_reflections(client, periods=("week", "month"))
ctx.as_text()      # a labeled, dated block for a prompt or a task's text
ctx.one_line()     # one condensed line, for an artifact that holds a single note
```

It takes today's daily entry, or walks back a couple of days when today's is not written yet (the
morning case), then the first period in `periods` with a submitted, non-empty review, retrying the
*previous* period when no current one has been submitted. Nothing there presumes which period type
matters: the caller passes the order. Every read is best-effort: a client without these methods, a
404, or a transport failure all degrade to an empty context.

Two consumers use it, and neither could see a reflection before:

- **The attended chat**, via `QuestRetrievalAdapter`'s new `reflection_context` query kind:
  `query({"kind": "reflection_context", "periods": ["week", "month"], "include_daily": true,
  "use_previous": false})`, advertised to the planner as `get_reflection_context` in
  `list_operations` / `describe_operation` exactly like `goal_context`. All spec fields are
  optional and no ids are needed. When nothing is on record it returns `kind="query"` with a plain
  statement to that effect, **not** `kind="error"`, since an error reads as "this lookup is broken" and
  sends the planner back to asking the person to paste text it just verified does not exist. This
  is the gap it closes: asked to choose work "based on my daily reflection", the assistant had no
  action that could go and read one, so the best it could do was say so and ask for a paste.
- **Autopilot** (`runner/autopilot.py`): each pass reads the reflection once (user-scoped, so it is
  cached for the pass rather than re-read per quest) and `compose_batch_text` carries it into every
  batch it creates, with the period order derived from the quest's own scope (a month-scoped quest
  asks for the month review first). `next_steps_from_pass` puts one condensed line in the artifact's
  `note` slot, as context for the list rather than a step on it. Everything else in a batch is
  derived from rows the system recorded; the reflection is the one input the person wrote, so it is
  what breaks ties about which eligible goal actually matters. A client without the reflection
  methods composes exactly the batch it composed before.

### Read the person's own insights

```
GET   /api/data/insights/collection              (get_insights_collection)
  -> { "id": "...", "name": "Insights", "customFields": [...] }        (created on first call)

GET   /api/data/collections/{collection_id}/entries?page=0&limit=50    (list_collection_entries)
  -> { "items": [ { "id": "...", "createdAt": "...",
                    "fieldValues": { "insight": "...", "categories": ["health"],
                                     "acted_on": false, "action_taken": "" } } ],
       "pagination": { "total": N, "page": 0, "has_next": false, ... } }

GET   /api/data/insights/by-quest?quest_id=Q&limit=10&include_acted=false   (list_quest_insights)
  -> { "collection_id": "...", "has_quests_field": true,
       "items": [ same entry shape as above, newest first, only insights assigned to Q, unacted ] }
     (a collection with no quests field answers items [] and has_quests_field false;
      an older backend without the route answers 404, which the client reports as {};
      the items are the CALLER's own insights only, so a lane whose key belongs to a separate
      service account reads none for a quest owned by someone else)

PATCH /api/data/insights/mark-acted-on           (mark_insight_acted_on)
  body { "entry_id": "...", "collection_id": "...", "action_taken_description": "..." }
```

Quest auto-creates one **Insights** collection per person: quick capture for the idea that arrives
away from any goal, with the free-text **category tags** they chose (`categories`), an `acted_on`
checkbox, and an `action_taken` description. It is the only place in Quest holding something the
person recorded that has **not** yet become a goal or a task, which is exactly why a background
pass that never reads it composes its brief as if the capture never happened.

Insights carry the quest ids they were assigned to (`fieldValues.quests`, set at capture).
Context assembly reads **only** the quest in front of it, through `by-quest`; it never reviews the
person's whole backlog, so unrelated insights are not pulled in and a capture is not re-clarified
on every request or pass. A request or task with no quest gets no insights. Wire facts that still
matter: items come back **newest first**, the envelope is **camelCase** (`fieldValues`,
`createdAt`) while the keys inside are the collection's field ids (`insight`, `categories`,
`acted_on`, `quests`), and an in-process caller sees snake_case, so `runner/insights.py` reads both.

`runner/insights.py` composes them into one `InsightsContext`:

```python
from quest_ai_runner.runner.insights import collect_quest_insights

ctx = collect_quest_insights(client, quest_id, since=last_pass_at)   # or no `since`, for the window
ctx.as_text()                 # a dated, tagged block for a prompt
ctx.one_line()                # one condensed line, for an artifact that holds a single note
```

It re-applies the `acted_on` check and the cutoff (the later of `since` and `now - days_cap`,
default 14 days) defensively, keeps newest first, caps the list and clips each entry. Every read is
best-effort: a client without the method, a 404, or a transport failure all degrade to an empty
context.

**The category tags are context for the reader, never a filter in this code.** Nothing compares a
tag to a quest name, a goal title, or any other string. Each insight is rendered with its tags as
the person typed them and the block ends by saying plainly that the tags are their labels for their
own thinking rather than a routing rule. Relevance is decided once, when the insight is assigned
to a quest. A hardcoded tag match is a fixed string
rule that silently drops every wording it did not anticipate ("dissertation" vs. "thesis" vs. no
tag at all), which is what hard rule #3 in `CLAUDE.md` forbids.

**Autopilot** (`runner/autopilot.py`) reads each quest's own insights (cache keyed by quest id, so
two quests in one pass never share a set), bounded by that quest's `autopilot.last_pass_at`, the
same stamp the cadence gate reads. A quest with no `last_pass_at` sees the whole window, because on
a first pass everything recent is new.
`next_steps_from_pass` puts one condensed line in the artifact's `note` slot; an insight is never
promoted to a *step*, since the person captured it rather than committing to it.

**On `mark_insight_acted_on`:** the method exists and is the documented way to close the loop, but
the autopilot pass deliberately does **not** call it. A pass creates a task, it does not do the
work, so ticking the box at pass time would claim an action that has not happened, and since a
ticked insight drops out of every unacted list (including the one the person's weekly review is
built from), an insight marked acted-on for a task that is then never approved, or that fails, has
been silently removed from their list with nothing to show for it. Call it from a surface that
knows the work actually landed, and write the description as a statement of what exists now.

**What a pass reports, and the `parent_task_id` link.** A pass creates work and finishes long
before that work runs, so its own result can only ever say what it set in motion. It says that in
plain words, naming the work and the quest (never a bare task id), and it stamps its OWN task id as
`parent_task_id` on everything it creates. That link is the hook a consumer needs to answer "what
did autopilot actually do" with the work itself: when a created task reaches a terminal status, the
consumer can write that task's own output onto the pass row (quest-backend does exactly this in
`app/business/quests/autopilot_rollup.py`). The result is one text, authored once by the run that
did the work, read on the quest and mailed where a quest mails its work. A backend whose create
route ignores `parent_task_id` loses the rollup, not the task.

### The quest folder's standing next steps (a context-entry upsert)

```
GET   /api/quests/{quest_id}/context-entries          (list_context_entries)
POST  /api/quests/{quest_id}/context-entries          (create_context_entry)
PUT   /api/quests/{quest_id}/context-entries/{id}     (update_context_entry)
```

A quest-linked folder carries ONE canonical "what to do next here": the `QAR:MANAGED:next_steps`
block in its `QUEST_SYNC.md` (`runner/quest_folder_sync.py`). It is a REPLACE, never a log, on both
sides. Locally the block is regenerated in place; on Quest it is a single **context entry** matched
by its fixed name (`NEXT_STEPS_ENTRY_NAME`), created once and PUT over afterwards, because the notes
API is add + list only and a daily refresh living in notes would leave a year of near-identical
rows. A failed entry LISTING writes nothing that round rather than blind-creating a duplicate; a
client with no context-entry support falls back to a `[next-steps]`-marked note and says so.

**Both readers write it, which is what keeps them from drifting apart:**

- **Autopilot** (`runner/autopilot.py`) feeds the standing artifact into each pass's batch as the
  plan of record, then writes its own conclusion back. Only a pass that produced work refreshes it;
  a dry run and a quiet/gated pass leave it alone.
- **The attended chat** (`runner/session_next_steps.py`) reads it once at session start and threads
  it into every turn's `rep_preamble`, labelled as the current authoritative answer rather than one
  retrieved file among many, so "what should I do next" starts from the artifact instead of being
  re-derived. A turn writes back only when it ran real work and left some of it unfinished (a
  completed `kind="deep"` turn with at least one `DeepResult` and at least one goal not finished);
  a turn that finished everything knows what it completed but not what comes next, so it leaves the
  considered answer in place for the next pass rather than replacing it with an empty block.

```python
from quest_ai_runner.runner.session_next_steps import (
    load_standing_next_steps, render_standing_next_steps, refresh_from_turn,
)

standing = load_standing_next_steps(cfg)                    # local read; None = nothing to add
preamble = render_standing_next_steps(standing)             # labelled block, every turn
refresh_from_turn(client, standing, goals=..., deep_results=...)   # None = nothing written
```

Which quest a folder belongs to is resolved from `RunnerConfig.quest_folder_map` first (via
`quest_for_path`, which also returns the mapped ROOT when the session starts in a subfolder), and
otherwise from the `quest_id` the sync file already stamps in its frontmatter
(`quest_id_in_folder`), so a folder that was ever pulled needs no configuration to be written back
to. Every step degrades to today's behavior: no folder, no artifact, no quest id, no client, or a
failed write all leave the session exactly as it was.

## Don't hand-roll HTTP

Use `QuestClient`: it covers discover / claim / report / escalate / loop-close / whoami / heartbeat
and is what the `Poller` and `TaskExecutor` call. The runner tests drive a mock client
(`tests/test_runner.py`) so you can verify behavior offline.
