# prioritization

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > prioritization

Part of [Quest Consciousness](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Quest ordering | `prioritization:quest_ordering` | The hub's Prioritization with Quest's own ordering rules: a person's rank map with the creator's legacy rank counting only for the creator, open asks before goals before ideas, the shorter goal horizon first, then the nearest deadline, open asks oldest first; what the person stated still comes first. It orders three uses (the work to start, the asks put to the person, the person's goals) and three goal levels: solution milestones, a quest's period goals and the day's goals, which are tasks (the person's own before a goal their AI rep handles). Scheduling a day (time blocks, sustained focus) has no rule or case in Quest yet and is not ordered here. One to one: a period goal is a milestone of the solution it is for, linked by Quest's spec_solution_id (a goal's spec_metric_id and metric_target_value are optional, so a goal with no metric value is still a milestone); its order is its milestone's, with no ranking of its own. A goal that names no solution yet is asked about once, never given a second milestone; the day's tasks sit under the period goal they serve. | [`quest_ai_runner/the_consciousness/prioritization/quest_ordering.py`](quest_ordering.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
