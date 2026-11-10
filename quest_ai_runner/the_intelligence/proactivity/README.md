# proactivity

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > proactivity

Part of [Intelligence](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Eligibility gate mixin | `quest-autopilot:autopilot_gates` | Methods on the autopilot pass that decide, in order, daily budget, whether the quest is due (asked of the hub's Proactivity: its cadence in its zone, a Run now, a reactive trigger from the asks Quest events report), roster day and backpressure, and read open asks, so a quest is skipped with a reason before any goal is read. | [`quest_ai_runner/the_intelligence/proactivity/autopilot_gates.py`](autopilot_gates.py) |  |
| Single-purpose routines | `quest-autopilot:autopilot_routines` | A quest's named single-purpose autopilots beside its ordinary one: each routine has a purpose (the one kind of work), its own instructions, days and hour (the hub's Proactivity, as a window) and the purpose's own gate, which refuses a pass with nothing due. A pass writes only its one work task and its own bookkeeping, never a goal, a task or a plan. The learning purpose asks the hub's Learning what reviews are due. | [`quest_ai_runner/the_intelligence/proactivity/autopilot_routines.py`](autopilot_routines.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
