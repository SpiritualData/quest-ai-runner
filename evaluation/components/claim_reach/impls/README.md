# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Scripted ideal decisions, no model call (offline) | `the-tools:scripted_ideal` | Runs the real orchestrator and tool catalogue with a planner that replays each case's ideal decisions, so every change a case requires lands once on the right quest and nothing else is written. | [`evaluation/components/claim_reach/impls/scripted_ideal.py`](scripted_ideal.py) |  |
| Rules only, no model (floor) | `the-tools:rules_only` | No model and no orchestrator: a request shaped like a goal or a note is written as one, whatever else it asked. | [`evaluation/components/claim_reach/impls/rules_only.py`](rules_only.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
