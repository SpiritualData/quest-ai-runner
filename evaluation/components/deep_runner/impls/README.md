# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Subprocess runner scenarios | `delegated-agent-sessions:subprocess_runner` | The selected reference worker driven through the common run entry with the process launch replaced by a scripted peer (exit code, output, stderr, missing binary, crash). Only the process edge is faked, and the way each outcome is reported is compared. | [`evaluation/components/deep_runner/impls/subprocess_runner.py`](subprocess_runner.py) |  |
| Agent-protocol runner scenarios | `delegated-agent-sessions:acp_runner` | The opt-in protocol worker driven through the same run entry, with its single connection seam replaced by a scripted session (message chunks, a stop reason, a missing agent, a crash). The same scenarios allow a like-for-like comparison with the subprocess runner. | [`evaluation/components/deep_runner/impls/acp_runner.py`](acp_runner.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
