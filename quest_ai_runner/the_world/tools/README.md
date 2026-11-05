# Tools

**Vision statement:** The AI acts only through tools the person's authority allows.

Breadcrumb: [Top](../../../esoi/README.md) > [World](../README.md) > Tools

This sub-intent arises as the implementation `the-tools:coding_agent_reach_judge` of its parent intent [`the_world`](../README.md). Opportunities: Tools differ in what they can change and how it can be undone, so each category the person allows is reached through its own measured solution and the person's authority bounds it all.

## How the solutions fit together

Tools lets the assistant act only through what the person's authority allows. Delegated Agent Sessions runs a goal in a headless agent within a turn and cost bound. Detect A Stalled Run reads a run's live activity to tell a stuck run from a slow one. File Editing changes files inside one folder and keeps each change reversible. Quest Change Executor changes a quest exactly as the person asked. Write Through MCP writes to an outside tool server only when an allowlist permits it.

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
- [Delegated Agent Sessions](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/tools/README.md) (`detect-a-stalled-run`): Hands open-ended work to an agent that runs in its own session and reports back. Specializes the library `hub`.
- [Connected System Operations](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/tools/README.md) (`write-through-mcp`): Calls an operation of a connected system, such as a write through an MCP server. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Unrequested Or Partial Changes (`prob-unrequested-or-partial-changes`): A request to change a quest leads to changes that were not asked for, to only part of what was asked, or to the same change made twice.

**Ideals**

- Exact Change (`ideal-exact-change`): The final state matches the request: everything asked, nothing else, once.

## Where to go next

- Parent intent: [World](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_world/tools/connected_system_operations](connected_system_operations/README.md), [quest_ai_runner/the_world/tools/delegated_agent_sessions](delegated_agent_sessions/README.md), [quest_ai_runner/the_world/tools/file_editing](file_editing/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv)

---

## The full record

### Problems

- Unrequested Or Partial Changes (`prob-unrequested-or-partial-changes`): A request to change a quest leads to changes that were not asked for, to only part of what was asked, or to the same change made twice.

### Ideals

- Exact Change (`ideal-exact-change`): The final state matches the request: everything asked, nothing else, once.

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
