# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Delegated Agent Sessions

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Agent-protocol runner scenarios (`acp_runner`) | 100% (11 of 11 cases) | free |  |
| 2 | `real_process` | 100% (11 of 11 cases) | free |  |
| 3 | Subprocess runner scenarios (`subprocess_runner`) | 100% (11 of 11 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Outcome reporting correctness across deep runners. Per runner, the same scenarios through the subprocess runner and the ACP runner with the outside peer faked at its process edge: how each outcome is reported back (met, silent no-op, paused with a decision id, partial or crashed, missing binary), and no scenario raises (deep_runner).
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Subprocess runner scenarios | `delegated-agent-sessions:subprocess_runner` | The selected reference worker driven through the common run entry with the process launch replaced by a scripted peer (exit code, output, stderr, missing binary, crash). Only the process edge is faked, and the way each outcome is reported is compared. | [`evaluation/components/deep_runner/impls/subprocess_runner.py`](subprocess_runner.py) |  |
| Agent-protocol runner scenarios | `delegated-agent-sessions:acp_runner` | The opt-in protocol worker driven through the same run entry, with its single connection seam replaced by a scripted session (message chunks, a stop reason, a missing agent, a crash). The same scenarios allow a like-for-like comparison with the subprocess runner. | [`evaluation/components/deep_runner/impls/acp_runner.py`](acp_runner.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
