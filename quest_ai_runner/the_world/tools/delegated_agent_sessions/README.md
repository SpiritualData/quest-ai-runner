# delegated_agent_sessions

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Tools](../README.md) > delegated_agent_sessions

Part of [Tools](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Agent-protocol deep runner | `delegated-agent-sessions:acp` | An opt-in alternative worker that holds a two-way session with an agent over the Agent Client Protocol, so a message can be injected into the turn that is running through a steering poll or a direct call. It never raises and turns a missing package, old runtime or wedged turn into a clear unmet result. | [`quest_ai_runner/the_world/tools/delegated_agent_sessions/acp_deep_runner.py`](acp_deep_runner.py) |  |
| Persistent stream-json runner | `delegated-agent-sessions:stream_json` | Holds one live Claude Code session per run over its stream-json input and output, so the person can steer, stop or pause while it works. A message is claimed only at the moment it is written to the session, so it arrives once and only for its own run; the model's replay of it marks it seen. A stop sends an interrupt and keeps the session, and a worker that ignores it is terminated and then killed after a grace period. A pause releases the process and resumes later on the same session. It keeps the live progress stream, resume, the subscription login, the usage-limit wait and the liveness review, and hands the run to the one-shot runner when the installed Claude Code cannot report an interrupt. | [`quest_ai_runner/the_world/tools/delegated_agent_sessions/goal_runner_stream.py`](goal_runner_stream.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Tools](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
