# Acting in the person's account as the AI's own body

What the AI says about a person's account is the account as it is now, and what it changed in the account is read back after the change, so the person can trust both.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `act-as-the-body:card_and_reads` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: The Quest account is the AI's own consistent state, which opens questions beyond the parent solution: how the AI states what it can reach (inside the host, outside it, in the world now, or not reachable) and is held to that claim, how heavy work is handed to a deep runner within a fixed turn and cost bound and its result checked, how a file change is kept bounded to one folder and reversible, how one named change on an outside tool server is made only when an allowlist permits it, and who a new goal belongs to and whether an AI representative can take it over.

## How the solutions fit together

Body keeps what the AI says about a person's account true to the account and to what it changed in it. It changes the quest as the person asks through Quest change executor and writes files inside one folder through Bounded reversible file writer. It reads from the sources the person's work lives in through Multi-source evidence retriever, and hands a new goal to the right owner through Goal assignee and doability judge. It claims only the reach that World grants. Heavy work is the job of Bounded autonomous work runner. One named change on an outside tool server is the job of Allowlisted operation writer.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Bounded autonomous work runner<br/><i>run-deep-work</i>"]
    s1["Bounded reversible file writer<br/><i>write-files</i>"]
    s2["Allowlisted operation writer<br/><i>write-through-mcp</i>"]
    s3["World<br/><i>claim-reach</i>"]
    s0 -->|"A result stating whether the goal was met, the output text..."| s3
    s1 -->|"A write result stating success, whether the file was..."| s3
    s2 -->|"A write result with success, error and an audit detail of..."| s3
```

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Quest change executor | `operate-a-quest` | `quest_ai_runner.run("operate-a-quest", 'Add a goal to ship the launch note by Friday')` | `qar_orchestrator` | Final quest state against the request over the quest operation cases |
| Multi-source evidence retriever | `gather-evidence` | `quest_ai_runner.run("gather-evidence", 'NOTES.md')` | `composite` | Retrieval recall of the right document over handbook questions |
| Bounded autonomous work runner | `run-deep-work` | no entry declared | `subprocess` | Outcome reporting correctness across deep runners, Goal loop outcomes against the done-standard, Deep worker launch settings ranked by cost per goal met |
| Bounded reversible file writer | `write-files` | `quest_ai_runner.run("write-files", 'NOTES.md')` | `files` | Write boundary and reversibility over the scratch-tree cases |
| Allowlisted operation writer | `write-through-mcp` | no entry declared | `mcp` | Allowlist enforcement and exactly-once execution |
| World | `claim-reach` | `quest_ai_runner.run("claim-reach", 'Send the launch note to the whole team')` | `coding_agent_prompt_reach` | Combined world score |
| Goal assignee and doability judge | `hand-off-a-goal` | `quest_ai_runner.run("hand-off-a-goal", ...)` | `model_judge` | Hand-offs a person keeps per unit of cost and time |

## Problems and ideals

**Problems**

- `prob-unrequested-or-partial-changes`: A request to change a quest leads to changes nobody asked for, to only part of what was asked, or to the same change made twice.
- `prob-reach-claims-unfounded`: The AI states what it can and cannot do outside the host's own data without a source of truth for it: it describes a capability that does not exist, reports a setting as blocking when the setting was...
- `prob-work-not-planned-backwards-from-done`: Deep work can start from a task description with no written, checkable condition for finishing, so the run plans forward from its first step and has nothing to be checked against when it stops.
- `prob-new-goal-lands-on-the-wrong-owner`: A new goal is given to the wrong owner, or to an AI representative that cannot do it, so it sits undone or is done badly while the person believes it is handled.
- `prob-evidence-spread-across-sources`: A planner decides or answers without the material that sits in the sources a person's work lives in, because each source is read its own way and one that fails stops the rest.

**Ideals**

- Backward-Planned Work (`ideal-plan-from-the-done-standard`): Work is planned backwards from a written, checkable done-standard, so every step serves a condition that can be seen to be met.

## Metrics and their leaders

- Final quest state against the request over the quest operation cases (`metric-operate-a-quest`): leader `orchestrator_pre_route`, 22 of 22 cases
- Retrieval recall of the right document over handbook questions (`metric-gather-evidence`): leader `composite`, 19 of 21 cases
- Outcome reporting correctness across deep runners (`metric-run-deep-work`): leader `acp_runner`, 11 of 11 cases
- Write boundary and reversibility over the scratch-tree cases (`metric-write-files`): leader `bounded_writer`, 20 of 20 cases
- Allowlist enforcement and exactly-once execution (`metric-write-through-mcp`): leader `allowlist_writer`, 15 of 15 cases
- Combined world score (`metric-claim-reach`): leader `scripted_ideal`, 12 of 12 cases
- Hand-offs a person keeps per unit of cost and time (`metric-hand-off-a-goal`): leader `model_judge`, 17 of 18 cases
- Goal loop outcomes against the done-standard (`metric-goal-met-by-evidence-and-coverage`): leader `goal_contract`, 53 of 53 cases
- Deep worker launch settings ranked by cost per goal met (`metric-harness-context-subagents-recursion-ranked`): leader `acp_runner`, 11 of 11 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- `prob-unrequested-or-partial-changes`: A request to change a quest leads to changes nobody asked for, to only part of what was asked, or to the same change made twice.
- `prob-reach-claims-unfounded`: The AI states what it can and cannot do outside the host's own data without a source of truth for it: it describes a capability that does not exist, reports a setting as blocking when the setting was never read, or says outside work has started when nothing ran. It also says it cannot do something it can.
- `prob-work-not-planned-backwards-from-done`: Deep work can start from a task description with no written, checkable condition for finishing, so the run plans forward from its first step and has nothing to be checked against when it stops.
- `prob-new-goal-lands-on-the-wrong-owner`: A new goal is given to the wrong owner, or to an AI representative that cannot do it, so it sits undone or is done badly while the person believes it is handled.
- `prob-evidence-spread-across-sources`: A planner decides or answers without the material that sits in the sources a person's work lives in, because each source is read its own way and one that fails stops the rest.

### Ideals

- Backward-Planned Work (`ideal-plan-from-the-done-standard`): Work is planned backwards from a written, checkable done-standard, so every step serves a condition that can be seen to be met.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Quest change executor | `operate-a-quest` | qar_orchestrator | Final quest state against the request over the quest operation cases |
| Multi-source evidence retriever | `gather-evidence` | composite | Retrieval recall of the right document over handbook questions |
| Bounded autonomous work runner | `run-deep-work` | subprocess | Outcome reporting correctness across deep runners, Goal loop outcomes against the done-standard, Deep worker launch settings ranked by cost per goal met |
| Bounded reversible file writer | `write-files` | files | Write boundary and reversibility over the scratch-tree cases |
| Allowlisted operation writer | `write-through-mcp` | mcp | Allowlist enforcement and exactly-once execution |
| World | `claim-reach` | coding_agent_prompt_reach | Combined world score |
| Goal assignee and doability judge | `hand-off-a-goal` | model_judge | Hand-offs a person keeps per unit of cost and time |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Planner-only orchestration | `operate-a-quest:qar_orchestrator` | [quest_ai_runner/area_act_as_the_body.py](../area_act_as_the_body.py) |  |
| Direct tool-call ablation | `operate-a-quest:direct_tool_call` | [evaluation/components/quest_ops/impls/direct_tool_call.py](../../evaluation/components/quest_ops/impls/direct_tool_call.py) |  |
| Local file tree reader | `gather-evidence:files` | [quest_ai_runner/adapters/files_adapter.py](../adapters/files_adapter.py) |  |
| Cached live-database reader | `gather-evidence:cached_db` | [quest_ai_runner/act_as_the_body/cached_db_adapter.py](cached_db_adapter.py) |  |
| Parallel multi-source composite | `gather-evidence:composite` | [quest_ai_runner/act_as_the_body/composite_retrieval_adapter.py](composite_retrieval_adapter.py) |  |
| MCP server reader | `gather-evidence:mcp` | [quest_ai_runner/act_as_the_body/mcp_retrieval_adapter.py](mcp_retrieval_adapter.py) |  |
| Remote file server reader | `gather-evidence:remote_files_adapter` | [quest_ai_runner/act_as_the_body/remote_files_adapter.py](remote_files_adapter.py) |  |
| Notion database reader | `gather-evidence:notion` | [quest_ai_runner/act_as_the_body/notion_adapter.py](notion_adapter.py) |  |
| Drive document reader | `gather-evidence:google_drive` | [quest_ai_runner/act_as_the_body/google_drive_adapter.py](google_drive_adapter.py) |  |
| Chat conversation reader | `gather-evidence:google_chat` | [quest_ai_runner/adapters/google_chat_adapter.py](../adapters/google_chat_adapter.py) |  |
| Quest API context reader | `gather-evidence:quest` | [quest_ai_runner/act_as_the_body/quest_retrieval_adapter.py](quest_retrieval_adapter.py) |  |
| Subprocess goal runner | `run-deep-work:subprocess` | [quest_ai_runner/core/goal_runner.py](../core/goal_runner.py) |  |
| Agent-protocol deep runner | `run-deep-work:acp` | [quest_ai_runner/act_as_the_body/acp_deep_runner.py](acp_deep_runner.py) |  |
| One-call fast file editor | `run-deep-work:fast_edit` | [quest_ai_runner/config_resolvers.py](../config_resolvers.py) |  |
| One-call MCP operation runner | `run-deep-work:mcp_write` | [quest_ai_runner/config_resolvers.py](../config_resolvers.py) |  |
| Subprocess runner scenarios | `run-deep-work:subprocess_runner` | [evaluation/components/deep_runner/impls/subprocess_runner.py](../../evaluation/components/deep_runner/impls/subprocess_runner.py) |  |
| Agent-protocol runner scenarios | `run-deep-work:acp_runner` | [evaluation/components/deep_runner/impls/acp_runner.py](../../evaluation/components/deep_runner/impls/acp_runner.py) |  |
| Goal contract rules check | `run-deep-work:goal_contract` | [evaluation/components/deep_run/impls/goal_contract.py](../../evaluation/components/deep_run/impls/goal_contract.py) |  |
| Contained backed-up writer | `write-files:files` | [quest_ai_runner/act_as_the_body/files_writer.py](files_writer.py) |  |
| Scratch-tree boundary check | `write-files:bounded_writer` | [evaluation/components/write_files/impls/bounded_writer.py](../../evaluation/components/write_files/impls/bounded_writer.py) |  |
| MCP operation writer | `write-through-mcp:mcp` | [quest_ai_runner/act_as_the_body/mcp_write_adapter.py](mcp_write_adapter.py) |  |
| Fake-client allowlist check | `write-through-mcp:allowlist_writer` | [evaluation/components/write_through_mcp/impls/allowlist_writer.py](../../evaluation/components/write_through_mcp/impls/allowlist_writer.py) |  |
| Persistent stream-json runner | `run-deep-work:stream_json` | [quest_ai_runner/act_as_the_body/goal_runner_stream.py](goal_runner_stream.py) |  |
| Coding agent, reach described in the prompt (today) | `claim-reach:coding_agent_prompt_reach` | [quest_ai_runner/area_act_as_the_body.py](../area_act_as_the_body.py) |  |
| Coding agent with the host reach summary and reach judge (built, off by default) | `claim-reach:coding_agent_reach_judge` | [quest_ai_runner/core/reach_judge.py](../core/reach_judge.py) |  |
| Coding agent with one declared inventory and receipts (proposed) | `claim-reach:coding_agent_declared_inventory` |  |  |
| Best open-source agent harness by the combined metric, with declared inventory (proposed) | `claim-reach:best_open_source_harness` |  |  |
| Two small model verdicts when a goal is created (today) | `hand-off-a-goal:model_judge` | [quest_ai_runner/area_act_as_the_body.py](../area_act_as_the_body.py) |  |
| Creator keeps the goal, no model call (floor, proposed) | `hand-off-a-goal:creator_keeps_it` | [evaluation/components/hand_off_a_goal/impls/creator_keeps_it.py](../../evaluation/components/hand_off_a_goal/impls/creator_keeps_it.py) |  |
| Scripted ideal decisions, no model call (offline) | `claim-reach:scripted_ideal` | [evaluation/components/act_in_the_world/impls/scripted_ideal.py](../../evaluation/components/act_in_the_world/impls/scripted_ideal.py) |  |
| Rules only, no model (floor) | `claim-reach:rules_only` | [evaluation/components/act_in_the_world/impls/rules_only.py](../../evaluation/components/act_in_the_world/impls/rules_only.py) |  |
| Lean guarded launch of the deep worker | `run-deep-work:lean_guarded` | [quest_ai_runner/core/deep_launch.py](../core/deep_launch.py) |  |

### Metrics

- Final quest state against the request over the quest operation cases (`metric-operate-a-quest`): `esoi run quest_ops --impl qar_orchestrator`
- Retrieval recall of the right document over handbook questions (`metric-gather-evidence`): `esoi run retrieval --impl composite; esoi run retrieval_sources --impl files_adapter`
- Outcome reporting correctness across deep runners (`metric-run-deep-work`): `esoi run deep_runner --impl subprocess_runner; esoi run deep_run --impl goal_contract`
- Write boundary and reversibility over the scratch-tree cases (`metric-write-files`): `esoi run write_files --impl bounded_writer`
- Allowlist enforcement and exactly-once execution (`metric-write-through-mcp`): `esoi run write_through_mcp --impl allowlist_writer`
- Combined world score (`metric-claim-reach`): `esoi run act_in_the_world --impl scripted_ideal`
- Hand-offs a person keeps per unit of cost and time (`metric-hand-off-a-goal`): `esoi run hand_off_a_goal --impl model_judge`
- Goal loop outcomes against the done-standard (`metric-goal-met-by-evidence-and-coverage`): `esoi run deep_run --impl goal_contract`
- Deep worker launch settings ranked by cost per goal met (`metric-harness-context-subagents-recursion-ranked`): `esoi run deep_runner --impl subprocess_runner`
