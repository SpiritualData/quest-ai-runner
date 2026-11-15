# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Quest Change Executor

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | `orchestrator_pre_route` | 100% (22 of 22 cases) | $0.0500 |  |
| 2 | Direct tool-call ablation (`direct_tool_call`) | 95% (21 of 22 cases) | $0.0027 |  |
| 3 | Planner-only orchestration (`qar_orchestrator`) | 86% (19 of 22 cases) | $0.0679 | selected |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Final quest state against the request over the quest operation cases. In-memory quest state after the run over the quest_ops cases, a share held out: the right tool with the right arguments, every step of a multi-step request, no write on an ambiguous or sharing message, a delete that was not confirmed leaves the experiment in place, no unrequested write Read in this direction: maximise how exactly the final quest state matches the request; minimise unrequested writes, duplicate records, steps left undone in a multi-step request, and tokens and planner turns spent per change.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Fake-client allowlist check | `quest-change-executor:allowlist_writer` | Runs the production writer against a fake client at the process edge to check listing, exact tool and arguments once, aliased and bare names, refusals with zero calls, server error and dead connection. | [`evaluation/components/write_through_mcp/impls/allowlist_writer.py`](allowlist_writer.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
