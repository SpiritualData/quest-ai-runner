# planner

Breadcrumb: [Top](../../../../esoi/README.md) > [Intelligence](../../README.md) > [Mind](../README.md) > planner

Part of [Mind](../README.md)

### Top implementations of Planner

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | `single_prompt` | 96% (52 of 54 cases) | $0.0011 |  |
| 2 | Full planner decision (`qar_planner`) | 91% (49 of 54 cases) | $0.0051 |  |
| 3 | `pre_route_then_planner` | 89% (48 of 54 cases) | $0.0015 |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

1 more compared; the full list is in the results log.

**How it is scored:** Routing correctness over the classification cases. The router's whole job over the classification cases, a share held out: route (answer, tool, deep, confirm, clarify), the tool, the arguments it fills in, every tool of a multi-step request, no confirming argument on a first destructive ask, no tool call where no change was asked Read in this direction: maximise correct route, tool and argument choice over labelled cases; minimise deep runs launched for plain questions, destructive requests sent without confirmation, and tool calls made where no change was asked; minimise model spend per routed message.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| The loop planner decides the route | `planner:planner_route` | The loop makes one planning call over the model and its decision is the route, returned as data; no tool runs and no worker starts. | [`quest_ai_runner/intelligence/mind/planner/message_routing.py`](message_routing.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Mind](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
