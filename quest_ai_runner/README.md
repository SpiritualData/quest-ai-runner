# quest_ai_runner

*ESOI records kept beside the code in `quest_ai_runner`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

A Quest user's AI system continuously evolves to help the user continuously evolve towards their intents.

These are the solutions of [Quest AI Runner](../esoi/README.md).

## How the solutions fit together

Quest AI Runner turns what a Quest user asks into work that moves their intents, and Quest Self is the one assistant that does it. Quest Self asks Quest Consciousness who is asking and what they are growing toward, reads their account and their own material through Quest Body, reasons about the request through Intelligence and reaches what lies outside through World. Quest Consciousness takes each request in through Quest Requests, hands it to the representative that fits through Quest Representatives, asks a person only for what is theirs through Quest Decision Requests, learns from feedback through Feedback Learning and proposes what is most likely to work for their intents through Intent Evolution. Quest Body keeps the person's Quest data in Quest Context, changes it exactly as asked through Quest Change Executor and keeps what is said about an account true through Quest Account Truth. Intelligence plans each request through Mind, recalls what it needs through Subconscious, keeps the long view through Higher Mind with Guidance and Quest Intents, and holds the work to a measure through Critical Thinking with Goal Completion and Response Quality. World asks models through Model Plumbing with Model Tier Chooser and Vendor Model Caller, uses outside tools through Tools with Delegated Agent Sessions and File Editing, and answers from public research through Science. Nervous System watches every run, Intake Sources brings requests in from where people work and Integrity keeps every part a specified solution that is measured.

## Overview diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Integrity<br/><i>system-integrity</i>"]
    s1["Intake Sources<br/><i>intake-sources</i>"]
    s2["Nervous System<br/><i>nervous-system</i>"]
    s3["Vendor Model Caller<br/><i>vendor-model-caller</i>"]
    s4["Science<br/><i>the-science</i>"]
    s5["Critical Thinking<br/><i>critical-thinking</i>"]
    s6["Goal Completion<br/><i>goal-completion</i>"]
    s7["Response Quality<br/><i>response-quality</i>"]
    s8["guidance"]
    s9["Tools<br/><i>the-tools</i>"]
    s10["Quest Context<br/><i>quest-context</i>"]
    s11["Quest Requests<br/><i>quest-requests</i>"]
    s0 --> s11
    s1 --> s9
    s1 --> s11
    s3 --> s0
    s3 --> s2
    s3 --> s9
    s3 --> s11
    s6 --> s2
    s6 --> s5
    s7 --> s0
    s7 --> s5
    s8 --> s5
    s10 -->|"Evidence from the person's own sources, with where each item came from"| s4
    s10 -->|"Evidence from the person's own sources, with where each item came from"| s11
    s11 -->|"The request taken in once and its outcome reported"| s5
```

## Intelligence (`the-intelligence`)

- Entry call: no entry declared
- Selected implementation: none selected
- Metric: none yet

## World (`the-world`)

- Entry call: no entry declared
- Selected implementation: none selected
- Metric: none yet

## Critical Thinking (`critical-thinking`)

- Entry call: `quest_ai_runner.run("critical-thinking", ...)`
- Selected implementation: Code-enforced before and after on deep runs only (today) (`deep_runs_only`)
- Metric: Requests held to their ESOI metric by code
- Implemented by: [`evaluation/components/act_as_the_body/__init__.py`](../evaluation/components/act_as_the_body/__init__.py), [`evaluation/components/act_as_the_body/broken/__init__.py`](../evaluation/components/act_as_the_body/broken/__init__.py), [`evaluation/components/act_as_the_body/broken/blind_replay.py`](../evaluation/components/act_as_the_body/broken/blind_replay.py), [`evaluation/components/act_as_the_body/cases.json`](../evaluation/components/act_as_the_body/cases.json), [`evaluation/components/act_as_the_body/impls/__init__.py`](../evaluation/components/act_as_the_body/impls/__init__.py), [`evaluation/components/act_as_the_body/impls/card_and_reads.py`](../evaluation/components/act_as_the_body/impls/card_and_reads.py), [`evaluation/components/act_as_the_body/interface.py`](../evaluation/components/act_as_the_body/interface.py), [`evaluation/components/act_as_the_body/score.py`](../evaluation/components/act_as_the_body/score.py) and 922 more

## Goal Completion (`goal-completion`)

- Entry call: `quest_ai_runner.run("goal-completion", ...)`
- Selected implementation: Production judges under a scripted provider (`qar_judges`)
- Metric: Judge deterministic-half correctness with a scripted provider
- Implemented by: [`evaluation/components/judge_live_decisions/impls/live_judges.py`](../evaluation/components/judge_live_decisions/impls/live_judges.py), [`evaluation/components/judge_live_decisions/impls/no_call_floor.py`](../evaluation/components/judge_live_decisions/impls/no_call_floor.py), [`quest_ai_runner/the_consciousness/judgment_calls/__init__.py`](the_consciousness/judgment_calls/__init__.py), [`quest_ai_runner/the_consciousness/judgment_calls/ask_policy.py`](the_consciousness/judgment_calls/ask_policy.py), [`quest_ai_runner/the_consciousness/judgment_calls/executor_pauses.py`](the_consciousness/judgment_calls/executor_pauses.py), [`quest_ai_runner/the_consciousness/judgment_calls/interface.py`](the_consciousness/judgment_calls/interface.py), [`quest_ai_runner/the_intelligence/critical_thinking/__init__.py`](the_intelligence/critical_thinking/__init__.py), [`quest_ai_runner/the_intelligence/critical_thinking/area.py`](the_intelligence/critical_thinking/area.py) and 65 more

## Response Quality (`response-quality`)

- Entry call: `quest_ai_runner.run("response-quality", 'See https://example.org/a for details.')`
- Selected implementation: Production guard engines under test (`qar_guards`)
- Metric: Reply guard correctness over link, recipe and anticipation cases, Judge agreement with labelled reference replies
- Implemented by: [`evaluation/components/esoi_judge/impls/model_judge.py`](../evaluation/components/esoi_judge/impls/model_judge.py), [`evaluation/components/esoi_judge/impls/regex_only.py`](../evaluation/components/esoi_judge/impls/regex_only.py), [`quest_ai_runner/prompts/evolvable_structure.md`](prompts/evolvable_structure.md), [`quest_ai_runner/the_intelligence/critical_thinking/__init__.py`](the_intelligence/critical_thinking/__init__.py), [`quest_ai_runner/the_intelligence/critical_thinking/area.py`](the_intelligence/critical_thinking/area.py), [`quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py`](the_intelligence/critical_thinking/goal_iteration.py), [`quest_ai_runner/the_intelligence/critical_thinking/interface.py`](the_intelligence/critical_thinking/interface.py), [`quest_ai_runner/the_intelligence/critical_thinking/response_quality/__init__.py`](the_intelligence/critical_thinking/response_quality/__init__.py) and 51 more

## Higher Mind (`higher-mind`)

- Entry call: no entry declared
- Selected implementation: none selected
- Metric: none yet

## Guidance (`guidance`)

- Entry call: `quest_ai_runner.run("guidance", 'What should I work on first this week?')`
- Selected implementation: Computed frame in the prompt (`frame-line`)
- Metric: Intent-grounded pass rate, Two-sided long-term guidance
- Implemented by: [`quest_ai_runner/the_body/hands/quest_operations/tools_quest_intents.py`](the_body/hands/quest_operations/tools_quest_intents.py), [`quest_ai_runner/the_body/internal_context/quest/client/quest_client_intents.py`](the_body/internal_context/quest/client/quest_client_intents.py), [`quest_ai_runner/the_intelligence/critical_thinking/__init__.py`](the_intelligence/critical_thinking/__init__.py), [`quest_ai_runner/the_intelligence/critical_thinking/area.py`](the_intelligence/critical_thinking/area.py), [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/__init__.py`](the_intelligence/critical_thinking/esoi_application/__init__.py), [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/autopilot_esoi.py`](the_intelligence/critical_thinking/esoi_application/autopilot_esoi.py), [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/interface.py`](the_intelligence/critical_thinking/esoi_application/interface.py), [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/repo_specs.py`](the_intelligence/critical_thinking/esoi_application/repo_specs.py) and 67 more

## Tools (`the-tools`)

- Entry call: `quest_ai_runner.run("the-tools", 'Send the launch note to the whole team')`
- Selected implementation: Coding agent, reach described in the prompt (today) (`coding_agent_prompt_reach`)
- Metric: Combined world score
- Implemented by: [`evaluation/components/act_as_the_body/__init__.py`](../evaluation/components/act_as_the_body/__init__.py), [`evaluation/components/act_as_the_body/broken/__init__.py`](../evaluation/components/act_as_the_body/broken/__init__.py), [`evaluation/components/act_as_the_body/broken/blind_replay.py`](../evaluation/components/act_as_the_body/broken/blind_replay.py), [`evaluation/components/act_as_the_body/cases.json`](../evaluation/components/act_as_the_body/cases.json), [`evaluation/components/act_as_the_body/impls/__init__.py`](../evaluation/components/act_as_the_body/impls/__init__.py), [`evaluation/components/act_as_the_body/impls/card_and_reads.py`](../evaluation/components/act_as_the_body/impls/card_and_reads.py), [`evaluation/components/act_as_the_body/interface.py`](../evaluation/components/act_as_the_body/interface.py), [`evaluation/components/act_as_the_body/score.py`](../evaluation/components/act_as_the_body/score.py) and 923 more

## Quest Context (`quest-context`)

Give every request the person's own Quest material and nobody else's.

- Entry call: `quest_ai_runner.run("quest-context", 'NOTES.md')`
- Selected implementation: Parallel multi-source composite (`composite`)
- Metric: Retrieval recall of the right document over handbook questions, Update-offering correctness over synthetic quest channels, Pointer-resolution safety over offline cases
- Implemented by: [`evaluation/components/feed_context_sources/impls/update_engine.py`](../evaluation/components/feed_context_sources/impls/update_engine.py), [`evaluation/components/resolve_references/impls/card_resolvers.py`](../evaluation/components/resolve_references/impls/card_resolvers.py), [`quest_ai_runner/the_body/configuration/__init__.py`](the_body/configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](the_body/configuration/acp_deep_runner_config.py), [`quest_ai_runner/the_body/configuration/card_store_settings.py`](the_body/configuration/card_store_settings.py), [`quest_ai_runner/the_body/configuration/cli_config.py`](the_body/configuration/cli_config.py), [`quest_ai_runner/the_body/configuration/config.py`](the_body/configuration/config.py), [`quest_ai_runner/the_body/configuration/config_assembler.py`](the_body/configuration/config_assembler.py) and 119 more

## Quest Requests (`quest-requests`)

Take every request made in Quest in once, through the same AI, and report its outcome to the person.

- Entry call: `quest_ai_runner.run("quest-requests", 'team-1')`
- Selected implementation: Offline lifecycle harness (`qar_runner`)
- Metric: Task lifecycle correctness over offline cases, Exactly-once mid-run delivery per run, and how fast it is seen, Answer pass rate gained per image described, Ways in that pass their own metric
- Implemented by: [`quest_ai_runner/the_body/area_body.py`](the_body/area_body.py), [`quest_ai_runner/the_body/configuration/__init__.py`](the_body/configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](the_body/configuration/acp_deep_runner_config.py), [`quest_ai_runner/the_body/configuration/card_store_settings.py`](the_body/configuration/card_store_settings.py), [`quest_ai_runner/the_body/configuration/cli_config.py`](the_body/configuration/cli_config.py), [`quest_ai_runner/the_body/configuration/config.py`](the_body/configuration/config.py), [`quest_ai_runner/the_body/configuration/config_assembler.py`](the_body/configuration/config_assembler.py), [`quest_ai_runner/the_body/configuration/config_context_index.py`](the_body/configuration/config_context_index.py) and 161 more

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Critical Thinking<br/><i>critical-thinking</i>"]
    s1["Goal Completion<br/><i>goal-completion</i>"]
    s2["Response Quality<br/><i>response-quality</i>"]
    s3["guidance"]
    s4["Quest Context<br/><i>quest-context</i>"]
    s5["Quest Requests<br/><i>quest-requests</i>"]
    s1 --> s0
    s2 --> s0
    s3 --> s0
    s4 -->|"Evidence from the person's own sources, with where each item came from"| s5
    s5 -->|"The request taken in once and its outcome reported"| s0
```

## Metrics

- Task lifecycle correctness over offline cases (`metric-run-queued-tasks`): leader `qar_runner`, 43 of 43 cases
- Requests held to their ESOI metric by code (`metric-think-critically`): leader `deep_runs_only`, 4 of 10 cases
- Retrieval recall of the right document over handbook questions (`metric-gather-evidence`): leader `composite`, 19 of 21 cases
- Combined world score (`metric-claim-reach`): leader `scripted_ideal`, 12 of 12 cases
- Ways in that pass their own metric (`metric-take-in-the-world`)

## Problems

- Help That Does Not Know The Person (`QP2`): A general assistant answers from general knowledge.
- Unreliable Help With The App (`prob-app-help-unreliable`): Questions about how to use Quest, or what its AI can do for the person, are answered from guesses instead of the product's documentation and actual reach: the AI describes a capability it does not have, or says it cannot do something it can.
- Lost Or Unreported Tasks (`QP1`): Quest tasks attached to goals can be lost, run twice, or end without an outcome the person sees.
- Different Answers On Different Surfaces (`QP4`): The same request can get a different answer depending on the Quest surface it came from.

## Ideals

- Personally Grounded Help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- Self-Explaining App (`ideal-self-explaining-app`): Any question about how to use the app gets a correct answer from its own documentation, and the AI knows what it can do.
- Right Representative (`ideal-right-rep`): Each request is handled by the representative whose role and assigned quests fit it, and work spawned from a thread keeps that thread's representative unless the person chooses another.
- Dependable Delivery (`ideal-dependable-delivery`): What is handed to the system arrives once, in order, and is accounted for.
- Same Assistant Everywhere (`QI4`): A person gets the same assistant, with the same answer to the same request, on every Quest surface.

## Where to go next

- Intent: [Quest AI Runner](../esoi/README.md)
- Down: [quest_ai_runner/the_body/configuration](the_body/configuration/README.md)
- Down: [quest_ai_runner/the_body/host](the_body/host/README.md)
- Down: [quest_ai_runner/the_consciousness/representatives](the_consciousness/representatives/README.md)
- Down: [quest_ai_runner/the_body/internal_context/quest](the_body/internal_context/quest/README.md)
- Down: [quest_ai_runner/the_intelligence/critical_thinking/esoi_application](the_intelligence/critical_thinking/esoi_application/README.md)
- Down: [quest_ai_runner/the_intelligence/mind/response_writer](the_intelligence/mind/response_writer/README.md)
- Down: [quest_ai_runner/the_intelligence/subconscious/cards_from_files](the_intelligence/subconscious/cards_from_files/README.md)
- Down: [quest_ai_runner/the_world/model_plumbing/model_tier_chooser](the_world/model_plumbing/model_tier_chooser/README.md)
- Down: [quest_ai_runner/the_world/model_plumbing/spend](the_world/model_plumbing/spend/README.md)
- Down: [quest_ai_runner/the_world/model_plumbing/vendor_model_caller](the_world/model_plumbing/vendor_model_caller/README.md)
- Down: [quest_ai_runner/the_world/tools/connected_system_operations](the_world/tools/connected_system_operations/README.md)
- Down: [quest_ai_runner/the_world/tools/delegated_agent_sessions](the_world/tools/delegated_agent_sessions/README.md)
- Down: [quest_ai_runner/the_world/tools/file_editing](the_world/tools/file_editing/README.md)
- Down: [quest_ai_runner/the_world/tools/file_reading](the_world/tools/file_reading/README.md)
- Down: [quest_ai_runner/the_world/tools/web_search](the_world/tools/web_search/README.md)
- Repository: [top README](../README.md)
