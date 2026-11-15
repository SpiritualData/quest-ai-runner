# prioritization

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > prioritization

Part of [Quest Consciousness](../README.md)

### Top implementations of Prioritization

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Quest ordering (`quest_ordering`) | score 1.00 (55 of 55 cases) | free | selected |
| 2 | `hub_stated_fields` | score 0.80 (39 of 55 cases) | free |  |
| 3 | `hub_as_given` | score 0.33 (2 of 55 cases) | free |  |

**How it is scored:** Quest's work in the order Quest's rules give. The work that competes for a person's attention comes back in the order Quest's own rules give, in three uses of Prioritization: the standing work the autopilot starts (open asks before goals before ideas, the shorter goal horizon first, then the nearest deadline: the hub's rules with Quest's added), the asks put to the person (the overdue and nearer deadline first, the undated newest first, open asks oldest first) and the person's goals (their own rank first with the creator's legacy rank counting only for the creator, completed goals last).
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Quest ordering | `prioritization:quest_ordering` | Composes the hub's stated_fields arm (called through hubofhubs.api) and overrides only Quest's own rules. Everything generic is the hub's: what the person stated, a blocked item after the unblocked, the deadline, the age of an ask, people waiting, what answering an ask or reaching a milestone frees, effort, met milestones and the batch limit. What Quest adds, handed to the hub in the fields it reads: a person's rank map with the creator's legacy rank counting only for the creator, open asks before goals before ideas for the work to start, the shorter goal horizon before the deadline, completed goals after open ones, a goal the AI rep handles after the person's own, undated asks put to the person newest first, a metric measured longest ago first. It orders the three subjects of the hub (work, asks, goals) and three goal levels: solution milestones, a quest's period goals and the day's goals, which are tasks. Scheduling a day has no rule or case in Quest yet and is not ordered here. One to one: a period goal is the milestone of the solution it is for, linked by Quest's spec_solution_id (a goal's spec_metric_id and metric_target_value are optional), and is ordered as that milestone, with no ranking of its own. | [`quest_ai_runner/consciousness/prioritization/quest_ordering.py`](quest_ordering.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
