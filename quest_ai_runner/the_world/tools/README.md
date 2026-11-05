# Tools

**Vision statement:** The AI acts only through tools the person's authority allows.

Breadcrumb: [Top](../../../esoi/README.md) > [World](../README.md) > Tools

This sub-intent arises as the implementation `the-tools:coding_agent_reach_judge` of its parent intent [`the_world`](../README.md).

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- Delegated Agent Sessions (solution with no page of its own, its implementations are in this intent's records)
- File Editing (solution with no page of its own, its implementations are in this intent's records)
- Quest Change Executor (solution with no page of its own, its implementations are in this intent's records): Change a quest exactly as the person asked, once.
- Detect A Stalled Run (solution with no page of its own, its implementations are in this intent's records): Tells a stuck run from a slow one by reading its live activity, so stuck work stops and slow work finishes.
- [Write Through MCP](connected_system_operations/README.md) (solution with implementations): Carry out one named change on an external tool server, and only when an explicit allowlist permits that operation.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Delegated Agent Sessions | `delegated-agent-sessions` | `quest_ai_runner.run("delegated-agent-sessions", ...)` | Subprocess goal runner (`subprocess`) | Outcome reporting correctness across deep runners, Goal loop outcomes against the done-standard, Deep worker launch settings ranked by cost per goal met |
| File Editing | `file-editing` | `quest_ai_runner.run("file-editing", 'NOTES.md')` | Contained backed-up writer (`files`) | Write boundary and reversibility over the scratch-tree cases |
| Quest Change Executor | `quest-change-executor` | `quest_ai_runner.run("quest-change-executor", 'Add a goal to ship the launch note by Friday')` | Planner-only orchestration (`qar_orchestrator`) | Final quest state against the request over the quest operation cases |
| Detect A Stalled Run | `detect-a-stalled-run` | `quest_ai_runner.run("detect-a-stalled-run", ...)` | Live model reviewer arm (`live_reviewer`) | Stop-or-continue verdict accuracy with false stops counted apart |
| Write Through MCP | `write-through-mcp` | `quest_ai_runner.run("write-through-mcp", ...)` | MCP operation writer (`mcp`) | Allowlist enforcement and exactly-once execution |

## Solutions from a library

- [Delegated Agent Sessions](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/tools/README.md) (`delegated-agent-sessions`): Hands open-ended work to an agent that runs in its own session and reports back. Adopted from the library `hub`.
- [File Editing](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/tools/README.md) (`file-editing`): Changes a file by instruction and reports what changed. Adopted from the library `hub`.
- [Connected System Operations](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/tools/README.md) (`quest-change-executor`): Calls an operation of a connected system, such as a write through an MCP server. Specializes the library `hub`.

## Where to go next

- Parent intent: [World](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_world/tools/connected_system_operations](connected_system_operations/README.md), [quest_ai_runner/the_world/tools/delegated_agent_sessions](delegated_agent_sessions/README.md), [quest_ai_runner/the_world/tools/file_editing](file_editing/README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Delegated Agent Sessions | `delegated-agent-sessions` | Subprocess goal runner (`subprocess`) | Outcome reporting correctness across deep runners, Goal loop outcomes against the done-standard, Deep worker launch settings ranked by cost per goal met |
| File Editing | `file-editing` | Contained backed-up writer (`files`) | Write boundary and reversibility over the scratch-tree cases |
| Quest Change Executor | `quest-change-executor` | Planner-only orchestration (`qar_orchestrator`) | Final quest state against the request over the quest operation cases |
| Detect A Stalled Run | `detect-a-stalled-run` | Live model reviewer arm (`live_reviewer`) | Stop-or-continue verdict accuracy with false stops counted apart |
| Write Through MCP | `write-through-mcp` | MCP operation writer (`mcp`) | Allowlist enforcement and exactly-once execution |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| One-call fast file editor | `delegated-agent-sessions:fast_edit` | [quest_ai_runner/the_body/configuration/config_resolvers.py](../../the_body/configuration/config_resolvers.py) |  |
| One-call MCP operation runner | `delegated-agent-sessions:mcp_write` | [quest_ai_runner/the_body/configuration/config_resolvers.py](../../the_body/configuration/config_resolvers.py) |  |
| Lean guarded launch of the deep worker | `delegated-agent-sessions:lean_guarded` | [quest_ai_runner/the_body/configuration/deep_launch.py](../../the_body/configuration/deep_launch.py) |  |
| Subprocess goal runner | `delegated-agent-sessions:subprocess` | [quest_ai_runner/the_intelligence/mind/delegation/goal_runner.py](../../the_intelligence/mind/delegation/goal_runner.py) |  |
| Planner-only orchestration | `quest-change-executor:qar_orchestrator` | [quest_ai_runner/the_body/area_body.py](../../the_body/area_body.py) |  |
| Production liveness review in the goal runner | `detect-a-stalled-run:goal_runner` | [quest_ai_runner/the_intelligence/mind/delegation/goal_runner.py](../../the_intelligence/mind/delegation/goal_runner.py) |  |
| Live model reviewer arm | `detect-a-stalled-run:live_reviewer` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](../../the_intelligence/critical_thinking/area.py) |  |
| Keyword and threshold rule floor | `detect-a-stalled-run:rule_floor` | [evaluation/components/deep_review/impls/rule_floor.py](../../../evaluation/components/deep_review/impls/rule_floor.py) |  |
| MCP operation writer | `write-through-mcp:mcp` | [quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py](connected_system_operations/mcp_write_adapter.py) |  |
| Agent-protocol deep runner | `delegated-agent-sessions:acp` | [quest_ai_runner/the_world/tools/delegated_agent_sessions/acp_deep_runner.py](delegated_agent_sessions/acp_deep_runner.py) |  |
| Persistent stream-json runner | `delegated-agent-sessions:stream_json` | [quest_ai_runner/the_world/tools/delegated_agent_sessions/goal_runner_stream.py](delegated_agent_sessions/goal_runner_stream.py) |  |
| Contained backed-up writer | `file-editing:files` | [quest_ai_runner/the_world/tools/file_editing/files_writer.py](file_editing/files_writer.py) |  |


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
