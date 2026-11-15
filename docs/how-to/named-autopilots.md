# Run named autopilots on a quest

A quest holds any number of **named autopilots**, each a standing piece of work with its own AI rep, schedule, approval, model tier, email and prompt. Each says what it is for in one sentence, so the list reads at a glance. This page is what the Quest AI Runner does with them. Writing them, the schedule and the screens belong to the Quest backend and frontend.

## Who does what

| Part | Does |
|---|---|
| The backend | Stores the autopilots, decides WHEN each runs (its scheduler asks the hub's Proactivity, with `days`, `cadence` and `on`, `run_time`, the quest's zone and `grace_minutes`), and queues ONE assistant task per run: `task_kind: "autopilot_run"`, with `autopilot_id`, `occurrence_key`, the autopilot's rep, tier and the quest's environment. |
| The Quest AI Runner | Executes that task. It writes no schedule, no pass series and no bookkeeping: the run rows are the record. |
| The hub's Proactivity | `due` says what is due now, `next_due` says when each schedule is due next (a `recurrence` of daily, weekly, monthly, quarterly or yearly with the day `on` of its period, a `window`, or a bare cadence). The backend uses the same call for the screen's "next run" and for the scheduler. |

## What a run does

The executor reads the autopilot from the quest by the task's `autopilot_id` field (never from its text), then, inside that one task:

1. **Skips with a reason** when the autopilot does not run: the quest is archived or paused, the autopilot is disabled, past the plan's cap (`paused_by_plan`) or gone. A scheduled run that waited longer than its autopilot's `grace_minutes` is skipped as "missed: the lane was down"; Run now and extra runs never expire.
2. **Holds the run** only for what the runner alone can see. Automatic extra runs (`occurrence_key` starting `reply:` or `extra:`, such as a run a reply reopened) are limited to 3 per autopilot a day and to the team's daily budget (`QAR_AUTOPILOT_DAILY_BUDGET`); a scheduled run and Run now are never counted. With backpressure on (`QAR_AUTOPILOT_BACKPRESSURE`), an unfinished earlier run of the same autopilot, or an unresolved HOLD decision on the quest, holds it.
3. **Asks its work source**, when it has one. A source (an internal `source` slug, today `learning`) says what is due and states the work's bounds; nothing due closes the run as skipped, with no model call.
4. **Routes goals by scope.** Once per quest per day a fast-tier judgment gives each eligible goal (current period, incomplete, marked for AI help) to the ONE on-duty autopilot whose scope (purpose and prompt) it fits best, or to none. A goal a rep holds stays with an on-duty autopilot of that rep. A goal given to nobody is left alone, and every run sees all goals as the frame. The day is the quest-zone date (UTC when the quest names no zone), and who is on duty is the backend's `status.runs_today`, so a monthly autopilot is on duty only on its day. The judgment is written to the quest's routing record for the day and read back by later runs. There is no goals switch: an autopilot works the goals that fit it, plus whatever its prompt asks for.
5. **Skips when it has nothing to do**: no source items, no goals routed to it and an empty prompt close the run as skipped, reason "no goals relevant to this scope".
6. **Composes one brief**: the quest's outcome, the quest's "About this quest" brief, the autopilot's own instructions (after it, as the person's words), the goals routed to it, the goal frame, and for a source run its bounds and what is due.
7. **Acts on the mode.** In `suggest` the task is parked as `suggested` with the brief and runs as composed once a person approves it. In `act` the deep agent runs on the brief in the quest's folder.

## Resources

- At most one autopilot run per quest folder (or per quest without one) is in flight at a time; quests are taken in turn, oldest first, so one busy quest does not hold the others back.
- `QAR_MAX_PARALLEL` stays the process-wide cap.

## Adding a work source

Write a `WorkSource` (scope, rules, the `due_items(now)` question, the allowed Quest writes) and `register_source` it (`autopilot_sources.py` has the type; `autopilot_purpose_learning.py` is the example: it asks the hub's Learning what is due). A consumer's own sources ride the same registry, and the system, not the person, sets an autopilot's `source`.

## Checking one without writing anything

A run task whose text contains `dry-run` reports the brief it WOULD work on and changes nothing.

## Measuring

`esoi run gate_an_autopilot_pass --impl live_pass` checks the run gates and `esoi run route_goals_to_autopilots --impl offline_routing` checks the routing code around the judge, both with no model call. How well a real judge reads scope is the same cases run with a model provider, by decision.
