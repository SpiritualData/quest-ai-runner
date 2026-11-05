# Quest Body

**Vision statement:** Quest is the AI's body.

Breadcrumb: [Top](../../esoi/README.md) > Quest Body

This sub-intent arises as the implementation `quest-body:quest_client` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A person's Quest data and the tools around it change for reasons of their own, so the one declared place where that data lives lets every other part stay unaware of how Quest stores it.

## How the solutions fit together

Quest Body is the form of the assistant and the Quest data that belongs to the person. Quest Body checks every claim about an account through Quest Account Truth, and takes requests in from where people work through Intake Sources. Quest Account Truth reads the account as it stands when the AI speaks, through Quest Context, which keeps each person's own notes, goals and decisions apart from everyone else's.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Quest Context<br/><i>quest-context</i>"]
    s1["Quest Account Truth<br/><i>quest-account-truth</i>"]
    s0 -->|"Evidence from the person's own sources, with where each item came from"| s1
```

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- [Quest Context](internal_context/README.md) (sub-intent): The AI knows each person's own Quest, nobody else's.
- Quest Account Truth (solution with no page of its own, its implementations are in this intent's records): Make what the AI says about an account the account as it is now.
- Intake Sources (solution with no page of its own, its implementations are in this intent's records)

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Quest Context | `quest-context` | `quest_ai_runner.run("quest-context", 'NOTES.md')` | Parallel multi-source composite (`composite`) | Retrieval recall of the right document over handbook questions, Pointer-resolution safety over offline cases |
| Quest Account Truth | `quest-account-truth` | `quest_ai_runner.run("quest-account-truth", '')` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Intake Sources | `intake-sources` | `quest_ai_runner.run("intake-sources")` | MCP gateway bridge transport (`openclaw`) | Channel transport conformance to the gateway wire contract |

## Solutions from a library

- [Internal Context](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_body/README.md) (`quest-context`): Declares where the system's data lives and which World tool reads it. Specializes the library `hub`.
- [Response Quality](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`quest-account-truth`): Judges a response against a quality profile: a named set of criteria with weights and target values. Specializes the library `hub`.
- [Intake Sources](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_body/README.md) (`intake-sources`): Declares where this system's requests come from (a chat, a task queue, a place where meeting action items are written) and reads the requests waiting at each, handing each over once. Adopted from the library `hub`.

## Problems and ideals

**Problems**

- Contradictory Account State (`prob-account-state-contradicts`): What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it can report the same state two ways, repeat a change it already made, or propose again what it said was done.
- Help That Does Not Know The Person (`QP2`): A general assistant answers from general knowledge.

**Ideals**

- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Personally Grounded Help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.

## Metrics and their leaders

- Final quest state against the request over the quest operation cases (`metric-operate-a-quest`): leader `orchestrator_pre_route`, 22 of 22 cases
- Outcome reporting correctness across deep runners (`metric-run-deep-work`): leader `acp_runner`, 11 of 11 cases
- Write boundary and reversibility over the scratch-tree cases (`metric-write-files`): leader `bounded_writer`, 20 of 20 cases
- Allowlist enforcement and exactly-once execution (`metric-write-through-mcp`): leader `allowlist_writer`, 15 of 15 cases
- Hand-offs a person keeps per unit of cost and time (`metric-hand-off-a-goal`): leader `model_judge`, 17 of 18 cases
- Goal loop outcomes against the done-standard (`metric-goal-met-by-evidence-and-coverage`): leader `goal_contract`, 53 of 53 cases
- Deep worker launch settings ranked by cost per goal met (`metric-harness-context-subagents-recursion-ranked`): leader `acp_runner`, 11 of 11 cases
- Account-state consistency across scripted sessions (`metric-act-as-the-body`): leader `card_and_reads`, 8 of 9 cases

## Where to go next

- Sub-intent: [Quest Context](internal_context/README.md)
- Parent intent: [Quest AI Runner](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records beside the code: [quest_ai_runner/the_body/configuration](configuration/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- Contradictory Account State (`prob-account-state-contradicts`): What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it can report the same state two ways, repeat a change it already made, or propose again what it said was done.
- Help That Does Not Know The Person (`QP2`): A general assistant answers from general knowledge.

### Ideals

- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Personally Grounded Help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Quest Context | `quest-context` | Parallel multi-source composite (`composite`) | Retrieval recall of the right document over handbook questions, Pointer-resolution safety over offline cases |
| Quest Account Truth | `quest-account-truth` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Intake Sources | `intake-sources` | MCP gateway bridge transport (`openclaw`) | Channel transport conformance to the gateway wire contract |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Read back what a change touched (proposed) | `quest-account-truth:read_after_write` |  |  |
| A live read behind every state claim (proposed) | `quest-account-truth:live_read_only` |  |  |
| Managed quest card plus optional live reads (today) | `quest-account-truth:card_and_reads` | [quest_ai_runner/the_body/area_body.py](area_body.py) |  |
| Cached live-database reader | `quest-context:cached_db` | [quest_ai_runner/the_body/internal_context/quest/cached_db_adapter.py](internal_context/quest/cached_db_adapter.py) |  |
| Quest API context reader | `quest-context:quest` | [quest_ai_runner/the_body/internal_context/quest/quest_retrieval_adapter.py](internal_context/quest/quest_retrieval_adapter.py) | [internal_context](internal_context/README.md) |
| Production registry under scripted host resolvers | `quest-context:card_resolvers` | [evaluation/components/resolve_references/impls/card_resolvers.py](../../evaluation/components/resolve_references/impls/card_resolvers.py) |  |
| MCP gateway bridge transport | `intake-sources:openclaw` | [quest_ai_runner/the_world/tools/connected_system_operations/openclaw_channel.py](../the_world/tools/connected_system_operations/openclaw_channel.py) |  |
| Parallel multi-source composite | `quest-context:composite` | [quest_ai_runner/the_world/tools/connected_system_operations/composite_retrieval_adapter.py](../the_world/tools/connected_system_operations/composite_retrieval_adapter.py) |  |
| MCP server reader | `quest-context:mcp` | [quest_ai_runner/the_world/tools/connected_system_operations/mcp_retrieval_adapter.py](../the_world/tools/connected_system_operations/mcp_retrieval_adapter.py) |  |
| Notion database reader | `quest-context:notion` | [quest_ai_runner/the_world/tools/connected_system_operations/notion_adapter.py](../the_world/tools/connected_system_operations/notion_adapter.py) |  |
| Drive document reader | `quest-context:google_drive` | [quest_ai_runner/the_world/tools/connected_system_operations/google_drive_adapter.py](../the_world/tools/connected_system_operations/google_drive_adapter.py) |  |
| Chat conversation reader | `quest-context:google_chat` | [quest_ai_runner/the_world/tools/connected_system_operations/google_chat_adapter.py](../the_world/tools/connected_system_operations/google_chat_adapter.py) |  |
| Local file tree reader | `quest-context:files` | [quest_ai_runner/the_world/tools/file_reading/files_adapter.py](../the_world/tools/file_reading/files_adapter.py) |  |
| Remote file server reader | `quest-context:remote_files_adapter` | [quest_ai_runner/the_world/tools/file_reading/remote_files_adapter.py](../the_world/tools/file_reading/remote_files_adapter.py) |  |
| Registry of built-in and host resolvers | `quest-context:note` | [quest_ai_runner/the_body/configuration/config_assembler.py](configuration/config_assembler.py) |  |

### Metrics

- Final quest state against the request over the quest operation cases (`metric-operate-a-quest`): `esoi run quest_ops --impl qar_orchestrator`
- Outcome reporting correctness across deep runners (`metric-run-deep-work`): `esoi run deep_runner --impl subprocess_runner; esoi run deep_run --impl goal_contract`
- Write boundary and reversibility over the scratch-tree cases (`metric-write-files`): `esoi run write_files --impl bounded_writer`
- Allowlist enforcement and exactly-once execution (`metric-write-through-mcp`): `esoi run write_through_mcp --impl allowlist_writer`
- Hand-offs a person keeps per unit of cost and time (`metric-hand-off-a-goal`): `esoi run hand_off_a_goal --impl model_judge`
- Goal loop outcomes against the done-standard (`metric-goal-met-by-evidence-and-coverage`): `esoi run deep_run --impl goal_contract`
- Deep worker launch settings ranked by cost per goal met (`metric-harness-context-subagents-recursion-ranked`): `esoi run deep_runner --impl subprocess_runner`
- Account-state consistency across scripted sessions (`metric-act-as-the-body`): `esoi run act_as_the_body --impl card_and_reads`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
