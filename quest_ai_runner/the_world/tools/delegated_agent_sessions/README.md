# delegated_agent_sessions

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Tools](../README.md) > delegated_agent_sessions

Part of [Tools](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Agent-protocol deep runner | `delegated-agent-sessions:acp` | An opt-in alternative worker that holds a two-way session with an agent over the Agent Client Protocol, so a message can be injected into the turn that is running through a steering poll or a direct call. It never raises and turns a missing package, old runtime or wedged turn into a clear unmet result. | [`quest_ai_runner/the_world/tools/delegated_agent_sessions/acp_deep_runner.py`](acp_deep_runner.py) |  |
| Persistent stream-json runner | `delegated-agent-sessions:stream_json` | Holds one live Claude Code session per run over its stream-json input and output, so the person can steer, stop or pause while it works. A message is claimed only at the moment it is written to the session, so it arrives once and only for its own run; the model's replay of it marks it seen. A stop sends an interrupt and keeps the session, and a worker that ignores it is terminated and then killed after a grace period. A pause releases the process and resumes later on the same session. It keeps the live progress stream, resume, the subscription login, the usage-limit wait and the liveness review, and hands the run to the one-shot runner when the installed Claude Code cannot report an interrupt. | [`quest_ai_runner/the_world/tools/delegated_agent_sessions/goal_runner_stream.py`](goal_runner_stream.py) |  |
| Hub's own tool loop | `delegated-agent-sessions:hub_loop` | Works a goal with the hub's own loop instead of an external coding agent: a pure completion replies with actions (read, edit, run, check, delegate, report) that hub code runs in a sandbox, starting from a folder map with the relevant files already in view. This is the hub's runner with this runner's settings, handed the lane's own operations (raising a decision, adding a note to a goal) as tools that run in process, and giving its commands the same environment as the Claude Code worker; a decision raised through a tool is the run's decision id. | [`quest_ai_runner/the_world/tools/delegated_agent_sessions/hub_loop_runner.py`](hub_loop_runner.py) |  |
| Hub's frame on the model's own tool loop | `delegated-agent-sessions:native_loop` | Works a goal in one lean Claude Code session on its own trained tools, prepared by the hub (the files that matter in view, an outline of the folder, what is left out and why, a short plan, discover before asking, this lane's skills folder named) and checked by the hub's code check of written text after it reports; stop, steer and resume are the stream runner's. This is the hub's runner with this runner's settings, named native_loop. | [`quest_ai_runner/the_world/tools/delegated_agent_sessions/native_loop_runner.py`](native_loop_runner.py) |  |
| Hub's framed session with sub-agents of the task's quest solutions | `delegated-agent-sessions:solution_agents_loop` | Works a goal in one hub-framed Claude Code session on its own tools, like native_loop, and offers it one sub-agent for each solution of the task's own quest specification (read from Quest at run time, per task: purpose, approach, out of scope, next unmet milestone, children, role) and no other agent; every other agent is refused by a hook and the calls are budgeted. The frame carries the lane's skills folder as an index (name, what it is for, path). The brief already carries the lane's retrieved context, so no second recall is passed. A quest with no solutions, or a run whose brief names no quest, gets the hub's fallback solutions. This is the hub's runner with this runner's settings, named solution_agents_loop. | [`quest_ai_runner/the_world/tools/delegated_agent_sessions/solution_agents_loop_runner.py`](solution_agents_loop_runner.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Tools](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
