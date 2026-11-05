# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Direct tool-call ablation | `quest-change-executor:direct_tool_call` | One model call is given the quest record and the tool schemas and returns the tool calls as JSON, which the registry then executes. There is no planner and no answer step, so it is the upper bound on what the direct path alone can do for a plain edit. | [`evaluation/components/quest_ops/impls/direct_tool_call.py`](direct_tool_call.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
