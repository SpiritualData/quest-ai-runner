# Run a second autopilot on a quest that does one kind of work

A quest's ordinary autopilot works the quest: its goals, tasks and plan. A **routine** is a second autopilot on the same quest that does ONE
kind of work and nothing else, on its own days and hour, with its own instructions and its own gate. Use one when a standing job (a weekly
review, a registry refresh) should not inherit the quest's goals, and the quest's own pass should not inherit the job.

## The settings

Routines live in the quest's autopilot settings (`PATCH /api/quests/{quest_id}/autopilot`, field `routines`, up to five, replaced as a list):

| Field | Meaning |
|---|---|
| `name` | slug, unique on the quest |
| `purpose` | the one kind of work; the runner must know it (`learning` ships) or the routine is skipped with a logged reason |
| `instructions` | the specification of the job, verbatim in every pass (8000 characters) |
| `days`, `run_time`, `run_timezone`, `grace_minutes` | when the pass opens: the hub's Proactivity reads them as a window, due once per occasion |
| `enabled` | keep the settings without running them |

The runner stamps each pass in `routine_runs` (name to time, merged key by key, so a stamp never rewrites your settings). A routine runs only
while the quest's own `mode` is on.

## What a pass does

1. The poller reads the routines from the settings snapshot it already keeps and asks the hub's Proactivity which are due (no extra Quest call
   for a quest with none).
2. The purpose's gate (`RoutinePurpose.due_items`) says what there is to do. **Nothing due refuses the pass**: it stamps the occasion as spent and
   creates nothing.
3. Otherwise it creates ONE `autopilot_work` task (queued in Act, suggested otherwise) whose text is the quest outcome, the purpose's bounds and
   standing rules, what is due, and the routine's instructions. The pass reads and writes no goals, lists and updates no tasks, and never plans.

## Adding a purpose

Write a `RoutinePurpose` (scope, rules, the `due_items(now)` question, the allowed Quest writes) and `register_purpose` it
(`autopilot_purpose_learning.py` is the example: it asks the hub's Learning what is due). A consumer's own purposes ride the same registry.

## Checking one without writing anything

`RoutinePass(client).run_quest(quest_id, dry_run=True, ignore_schedule=True)` reads, asks the purpose's gate, composes the work text and
returns it; it creates no task and stamps nothing.
