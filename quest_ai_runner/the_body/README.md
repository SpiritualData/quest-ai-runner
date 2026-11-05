# the_body

*ESOI records kept beside the code in `quest_ai_runner/the_body`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote, and every answer and action rests on the person's own goals, material and preferences, and never on another person's.

These are the solutions of [Grounded AI that advances people's goals](../../esoi/README.md).

## How the solutions fit together

quest-ai-runner turns a person's request or queued task into grounded work in one flow, and the Grounded goal agent is that flow. It takes the request in through Request intake, says what the person is working toward through Higher mind and chooses the context the request needs through Context choice. It reasons step by step through Mind, acts on the account through Body and Quest account access, and answers questions beyond the person's own material through Science. It has every run watched through Nervous System and holds the work to the metric that serves it through Critical Thinking. It asks the model through the Tiered model provider layer and stays comparable by metric through Integrity. It recalls what a request needs through Subconscious. Subconscious recalls the cards that match a request through Hub card store. Tiered model provider layer accounts for every call through Cost estimation, daily budget and host load guard. Mind starts each step on the model tier it picks through Cost-ordered model tier selector with outage and allowance recovery. Nervous System has the model oversee a run through Tiered model provider layer. Queued-task runner claims, runs and reports a queued task, asking the model through Tiered model provider layer and working in the account through Quest account access. It writes each prompt as a registered spec with an owner and a metric through Justified model calls. Body reads and changes the quest through Context card persistence backend, Quest account access and Queued-task runner. Higher mind saves what it learned through Context card persistence backend. Request intake hears from a live channel through Live messaging channel transport. Body changes the quest as the person asks through Quest change executor and writes files inside one folder through Bounded reversible file writer. Body reads from the sources the person's work lives in through Multi-source evidence retriever and hands a new goal to the right owner through Goal assignee and doability judge. Body does heavy work through Bounded autonomous work runner, makes one named change on an outside tool server through Allowlisted operation writer and claims only the reach that World grants. Quest change executor reaches the account through Quest account access and decides through Tiered model provider layer. Goal assignee and doability judge asks the model whether a goal is doable through Tiered model provider layer. World reaches a person through Live messaging channel transport and judges through Tiered model provider layer.

## Overview diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Quest Context<br/><i>quest-context</i>"]
    s1["Quest Requests<br/><i>quest-requests</i>"]
    s2["Quest Body<br/><i>quest-body</i>"]
    s3["Quest Account Truth<br/><i>quest-account-truth</i>"]
    s4["Quest Change Executor<br/><i>quest-change-executor</i>"]
    s5["Quest Intents<br/><i>quest-intents</i>"]
    s6["Quest Self<br/><i>quest-self</i>"]
    s0 -->|"Evidence from the person's own sources, with where each item came from"| s1
    s0 -->|"Evidence from the person's own sources, with where each item came from"| s3
    s1 -->|"The request taken in once and its outcome reported"| s3
    s1 -->|"The request taken in once and its outcome reported"| s6
    s2 --> s1
    s2 --> s3
    s2 --> s4
    s2 --> s6
    s3 -->|"The reply with its account claims checked"| s6
    s4 -->|"The change made and the state read back"| s3
    s5 -->|"The quest the message serves and the intent frame for the work"| s6
```

## Quest Body (`quest-body`)

Give the assistant one declared place where a person's Quest data lives and where requests come from.

- Entry call: no entry declared
- Selected implementation: Quest HTTP client from the environment (`quest_client`)
- Metric: Parity of Quest operations across access points
- Implemented by: [`quest_ai_runner/the_body/area_body.py`](area_body.py), [`quest_ai_runner/the_body/internal_context/quest/__init__.py`](internal_context/quest/__init__.py), [`quest_ai_runner/the_body/internal_context/quest/cached_db_adapter.py`](internal_context/quest/cached_db_adapter.py), [`quest_ai_runner/the_body/internal_context/quest/interface.py`](internal_context/quest/interface.py), [`quest_ai_runner/the_body/internal_context/quest/quest_retrieval_adapter.py`](internal_context/quest/quest_retrieval_adapter.py)

## Quest Account Truth (`quest-account-truth`)

Make what the AI says about an account the account as it is now.

- Entry call: no entry declared
- Selected implementation: Managed quest card plus optional live reads (today) (`card_and_reads`)
- Metric: Account-state consistency across scripted sessions
- Implemented by: [`quest_ai_runner/the_body/area_body.py`](area_body.py), [`quest_ai_runner/the_body/internal_context/quest/__init__.py`](internal_context/quest/__init__.py), [`quest_ai_runner/the_body/internal_context/quest/cached_db_adapter.py`](internal_context/quest/cached_db_adapter.py), [`quest_ai_runner/the_body/internal_context/quest/interface.py`](internal_context/quest/interface.py), [`quest_ai_runner/the_body/internal_context/quest/quest_retrieval_adapter.py`](internal_context/quest/quest_retrieval_adapter.py), [`quest_ai_runner/the_intelligence/critical_thinking/goal_completion/__init__.py`](../the_intelligence/critical_thinking/goal_completion/__init__.py), [`quest_ai_runner/the_intelligence/critical_thinking/goal_completion/interface.py`](../the_intelligence/critical_thinking/goal_completion/interface.py), [`quest_ai_runner/the_intelligence/critical_thinking/goal_completion/orchestrator_judges.py`](../the_intelligence/critical_thinking/goal_completion/orchestrator_judges.py) and 82 more

## Quest Change Executor (`quest-change-executor`)

Change a quest exactly as the person asked, once.

- Entry call: no entry declared
- Selected implementation: Planner-only orchestration (`qar_orchestrator`)
- Metric: Final quest state against the request over the quest operation cases, Allowlist enforcement and exactly-once execution
- Implemented by: [`evaluation/components/quest_ops/impls/direct_tool_call.py`](../../evaluation/components/quest_ops/impls/direct_tool_call.py), [`evaluation/components/write_through_mcp/impls/allowlist_writer.py`](../../evaluation/components/write_through_mcp/impls/allowlist_writer.py), [`quest_ai_runner/the_body/area_body.py`](area_body.py), [`quest_ai_runner/the_body/configuration/__init__.py`](configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](configuration/acp_deep_runner_config.py), [`quest_ai_runner/the_body/configuration/card_store_settings.py`](configuration/card_store_settings.py), [`quest_ai_runner/the_body/configuration/cli_config.py`](configuration/cli_config.py), [`quest_ai_runner/the_body/configuration/config.py`](configuration/config.py) and 89 more

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Quest Body<br/><i>quest-body</i>"]
    s1["Quest Account Truth<br/><i>quest-account-truth</i>"]
    s2["Quest Change Executor<br/><i>quest-change-executor</i>"]
    s0 --> s1
    s0 --> s2
    s2 -->|"The change made and the state read back"| s1
```

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Subprocess goal runner | `run-deep-work:subprocess` | [`quest_ai_runner/the_intelligence/mind/delegation/goal_runner.py`](../the_intelligence/mind/delegation/goal_runner.py) |  |
| Coding agent with the host reach summary and reach judge (built, off by default) | `the-tools:coding_agent_reach_judge` | [`quest_ai_runner/the_intelligence/mind/planner/reach_judge.py`](../the_intelligence/mind/planner/reach_judge.py) |  |
| Offline lifecycle harness | `quest-requests:qar_runner` | [`quest_ai_runner/the_body/area_body.py`](area_body.py) | selected |
| Managed quest card plus optional live reads (today) | `quest-account-truth:card_and_reads` | [`quest_ai_runner/the_body/area_body.py`](area_body.py) | selected |
| Quest HTTP client from the environment | `quest-body:quest_client` | [`quest_ai_runner/the_body/area_body.py`](area_body.py) | selected |
| Planner-only orchestration | `quest-change-executor:qar_orchestrator` | [`quest_ai_runner/the_body/area_body.py`](area_body.py) | selected |
| Coding agent, reach described in the prompt (today) | `the-tools:coding_agent_prompt_reach` | [`quest_ai_runner/the_body/area_body.py`](area_body.py) |  |
| Two small model verdicts when a goal is created (today) | `quest-representatives:model_judge` | [`quest_ai_runner/the_body/area_body.py`](area_body.py) |  |

## Metrics

- Final quest state against the request over the quest operation cases (`metric-operate-a-quest`): leader `orchestrator_pre_route`, 22 of 22 cases
- Outcome reporting correctness across deep runners (`metric-run-deep-work`): leader `acp_runner`, 11 of 11 cases
- Write boundary and reversibility over the scratch-tree cases (`metric-write-files`): leader `bounded_writer`, 20 of 20 cases
- Allowlist enforcement and exactly-once execution (`metric-write-through-mcp`): leader `allowlist_writer`, 15 of 15 cases
- Hand-offs a person keeps per unit of cost and time (`metric-hand-off-a-goal`): leader `model_judge`, 17 of 18 cases
- Goal loop outcomes against the done-standard (`metric-goal-met-by-evidence-and-coverage`): leader `goal_contract`, 53 of 53 cases
- Deep worker launch settings ranked by cost per goal met (`metric-harness-context-subagents-recursion-ranked`): leader `acp_runner`, 11 of 11 cases
- Account-state consistency across scripted sessions (`metric-act-as-the-body`): leader `card_and_reads`, 8 of 9 cases
- Parity of Quest operations across access points (`metric-quest-operations-parity`)

## Problems

- Contradictory Account State (`prob-account-state-contradicts`): What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it can report the same state two ways, repeat a change it already made, or propose again what it said was done.
- Different Science Evidence In Two Chats (`prob-science-search-duplicates-research-chat`): One science question can get different evidence in Quest chat than in Quest's research chat.
- Unrequested Or Partial Changes (`prob-unrequested-or-partial-changes`): A request to change a quest leads to changes that were not asked for, to only part of what was asked, or to the same change made twice.

## Ideals

- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Exact Change (`ideal-exact-change`): The final state matches the request: everything asked, nothing else, once.
- Consistent Science Evidence (`ideal-science-through-one-search`): A science question gets the same evidence, weighed the same way, in Quest chat and in the research chat.

## Where to go next

- Up: [quest_ai_runner](../README.md)
- Intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Down: [quest_ai_runner/the_body/configuration](configuration/README.md)
- Down: [quest_ai_runner/the_body/host](host/README.md)
- Down: [quest_ai_runner/the_body/internal_context/quest](internal_context/quest/README.md)
- Repository: [top README](../../README.md)
