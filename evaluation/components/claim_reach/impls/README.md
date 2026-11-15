# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Tools

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Scripted ideal decisions, no model call (offline) (`scripted_ideal`) | 100% (12 of 12 cases) | free |  |
| 2 | Rules only, no model (floor) (`rules_only`) | 83% (10 of 12 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Combined world score. Measures which configuration should do the AI's work in the world, combining several things into one score.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Scripted ideal decisions, no model call (offline) | `the-tools:scripted_ideal` | Runs the real orchestrator and tool catalogue with a planner that replays each case's ideal decisions, so every change a case requires lands once on the right quest and nothing else is written. | [`evaluation/components/claim_reach/impls/scripted_ideal.py`](scripted_ideal.py) |  |
| Rules only, no model (floor) | `the-tools:rules_only` | No model and no orchestrator: a request shaped like a goal or a note is written as one, whatever else it asked. | [`evaluation/components/claim_reach/impls/rules_only.py`](rules_only.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
