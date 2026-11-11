# proactivity

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > proactivity

Part of [Intelligence](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Eligibility gate mixin | `quest-autopilot:autopilot_gates` | Methods on the autopilot pass that decide, in order, daily budget, whether the quest is due (asked of the hub's Proactivity: its cadence in its zone, a Run now, a reactive trigger from the asks Quest events report), roster day and backpressure, and read open asks, so a quest is skipped with a reason before any goal is read. | [`quest_ai_runner/the_intelligence/proactivity/autopilot_gates.py`](autopilot_gates.py) |  |
| Single-purpose routines | `quest-autopilot:autopilot_routines` | A quest's named single-purpose autopilots beside its ordinary one: each routine has a purpose (the one kind of work), its own instructions, days and hour (the hub's Proactivity, as a window) and the purpose's own gate, which refuses a pass with nothing due. A pass writes only its one work task and its own bookkeeping, never a goal, a task or a plan. The learning purpose asks the hub's Learning what reviews are due. | [`quest_ai_runner/the_intelligence/proactivity/autopilot_routines.py`](autopilot_routines.py) |  |
| Structure watch binding | `quest-autopilot:structure_watch_binding` | Maps a quest's goals, experiments and investigations to the hub's structure digest and runs the hub's best-tier watch once per autopilot or routine run of that quest, with the lane's own model provider asked by the call role's tier and its tokens and cost read per call. Every item is a decision request on the quest and no record is edited; no call is made when that quest's records did not change since its last watch, in a dry run, or when the lane switched it off (autopilot_structure_watch). Passed in by default by the poller to both passes. One real call over the hub's own records on 2026-11-10: 1233 tokens in, 8 out, about 0.005 dollars on the keyless provider (its harness adds to the prompt's own tokens), no items. | [`quest_ai_runner/the_intelligence/proactivity/autopilot_structure_watch.py`](autopilot_structure_watch.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
