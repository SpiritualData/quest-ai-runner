# prioritization

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > prioritization

Part of [Quest Consciousness](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Quest ordering | `prioritization:quest_ordering` | Composes the hub's stated_fields arm (called through hubofhubs.api) and overrides only Quest's own rules. Everything generic is the hub's: what the person stated, a blocked item after the unblocked, the deadline, the age of an ask, people waiting, what answering an ask or reaching a milestone frees, effort, met milestones and the batch limit. What Quest adds, handed to the hub in the fields it reads: a person's rank map with the creator's legacy rank counting only for the creator, open asks before goals before ideas for the work to start, the shorter goal horizon before the deadline, completed goals after open ones, a goal the AI rep handles after the person's own, undated asks put to the person newest first, a metric measured longest ago first. It orders the three subjects of the hub (work, asks, goals) and three goal levels: solution milestones, a quest's period goals and the day's goals, which are tasks. Scheduling a day has no rule or case in Quest yet and is not ordered here. One to one: a period goal is the milestone of the solution it is for, linked by Quest's spec_solution_id (a goal's spec_metric_id and metric_target_value are optional), and is ordered as that milestone, with no ranking of its own. | [`quest_ai_runner/the_consciousness/prioritization/quest_ordering.py`](quest_ordering.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
