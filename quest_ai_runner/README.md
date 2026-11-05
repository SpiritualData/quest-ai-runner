# quest_ai_runner

*ESOI records kept beside the code in `quest_ai_runner`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote, and every answer and action rests on the person's own goals, material and preferences, and never on another person's.

These are the solutions of [Grounded AI that advances people's goals](../esoi/README.md).

## How the solutions fit together

quest-ai-runner turns a person's request or queued task into grounded work in one flow, and the Grounded goal agent is that flow. It takes the request in through Request intake, says what the person is working toward through Higher mind and chooses the context the request needs through Context choice. It reasons step by step through Mind, acts on the account through Body and Quest account access, and answers questions beyond the person's own material through Science. It has every run watched through Nervous System and holds the work to the metric that serves it through Critical Thinking. It asks the model through the Tiered model provider layer and stays comparable by metric through Integrity. It recalls what a request needs through Subconscious. Subconscious recalls the cards that match a request through Hub card store. Tiered model provider layer accounts for every call through Cost estimation, daily budget and host load guard. Mind starts each step on the model tier it picks through Cost-ordered model tier selector with outage and allowance recovery. Nervous System has the model oversee a run through Tiered model provider layer. Queued-task runner claims, runs and reports a queued task, asking the model through Tiered model provider layer and working in the account through Quest account access. It writes each prompt as a registered spec with an owner and a metric through Justified model calls. Body reads and changes the quest through Context card persistence backend, Quest account access and Queued-task runner. Higher mind saves what it learned through Context card persistence backend. Request intake hears from a live channel through Live messaging channel transport. Body changes the quest as the person asks through Quest change executor and writes files inside one folder through Bounded reversible file writer. Body reads from the sources the person's work lives in through Multi-source evidence retriever and hands a new goal to the right owner through Goal assignee and doability judge. Body does heavy work through Bounded autonomous work runner, makes one named change on an outside tool server through Allowlisted operation writer and claims only the reach that World grants. Quest change executor reaches the account through Quest account access and decides through Tiered model provider layer. Goal assignee and doability judge asks the model whether a goal is doable through Tiered model provider layer. World reaches a person through Live messaging channel transport and judges through Tiered model provider layer.

## Overview diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Tiered model provider layer<br/><i>vendor-model-caller</i>"]
    s1["Live messaging channel transport<br/><i>talk-over-a-channel</i>"]
    s2["Nervous System<br/><i>nervous-system</i>"]
    s3["Cost estimation, daily budget and host load guard<br/><i>track-usage-and-cost</i>"]
    s4["Integrity<br/><i>system-integrity</i>"]
    s5["Science<br/><i>the-science</i>"]
    s6["Queued-task runner<br/><i>run-queued-tasks</i>"]
    s7["Critical Thinking<br/><i>critical-thinking</i>"]
    s8["Multi-source evidence retriever<br/><i>gather-evidence</i>"]
    s9["Quest change executor<br/><i>operate-a-quest</i>"]
    s10["Bounded autonomous work runner<br/><i>run-deep-work</i>"]
    s11["Bounded reversible file writer<br/><i>write-files</i>"]
    s12["Allowlisted operation writer<br/><i>write-through-mcp</i>"]
    s13["World<br/><i>claim-reach</i>"]
    s14["Goal assignee and doability judge<br/><i>hand-off-a-goal</i>"]
    s15["Body<br/><i>the-body</i>"]
    s16["Quest account access<br/><i>access-quest-operations</i>"]
    s17["Mind<br/><i>the-mind</i>"]
    s18["Higher mind<br/><i>hold-the-intents</i>"]
    s19["Request intake<br/><i>take-in-the-world</i>"]
    s20["Subconscious<br/><i>the-subconscious</i>"]
    s21["Grounded goal agent<br/><i>do-grounded-work</i>"]
    s22["Justified model calls<br/><i>justify-model-calls</i>"]
    s23["Cost-ordered model tier selector with outage and allowance recovery<br/><i>model-tier-chooser</i>"]
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s2
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s6
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s9
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s13
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s14
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s17
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s18
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s21
    s1 -->|"An inbound batch (messages with ids, chat references, text and attachments, a healthy flag and any error) and a send result with success and message id"| s13
    s1 -->|"An inbound batch (messages with ids, chat references, text and attachments, a healthy flag and any error) and a send result with success and message id"| s19
    s2 -->|"For a flagged case: the risk category, the response applied and its signal (proceed, redirect with a hint, answer now, escalate to deep execution, escalate to a person), with a reason and a degraded flag"| s21
    s3 -->|"A price per million input tokens, a bootstrap cost estimate with the provider and model used, a running total with an over-limit flag and status line, and a paused or not verdict"| s0
    s4 -->|"Checked records, recorded metric results and the selected implementation for each solution"| s21
    s5 -->|"Findings, each with its source, an evidence-strength label and the passage that supports it"| s21
    s6 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s7
    s6 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s15
    s7 -->|"Per request: the metric applied with its reading before and after, or the reason none applies, or a proposed metric; and for a regression or a missing metric, the response taken"| s21
    s8 -->|"Observation values carrying text, a path or source label, and a kind, with error observations in place of exceptions"| s5
    s8 -->|"Observation values carrying text, a path or source label, and a kind, with error observations in place of exceptions"| s15
    s9 -->|"The resulting quest state and a reply that reports what was done"| s7
    s9 -->|"The resulting quest state and a reply that reports what was done"| s15
    s10 -->|"A result stating whether the goal was met, the output text, and error or pause details such as a decision id"| s13
    s10 -->|"A result stating whether the goal was met, the output text, and error or pause details such as a decision id"| s15
    s11 -->|"A write result stating success, whether the file was created, bytes written, where the backup went and any error; the current text on read"| s13
    s11 -->|"A write result stating success, whether the file was created, bytes written, where the backup went and any error; the current text on read"| s15
    s12 -->|"A write result with success, error and an audit detail of the tool, arguments and whether it executed; a list of writable operations with schemas"| s13
    s12 -->|"A write result with success, error and an audit detail of the tool, arguments and whether it executed; a list of writable operations with schemas"| s15
    s13 -->|"A reach verdict for the request (inside, outside, in the world now, or not reachable, with why), and receipts for outside acts (what ran, where, its id)"| s15
    s14 -->|"The text or verdict the call returns"| s15
    s15 -->|"A current state view for the request's scope with the time each part was read, and after a change the read-back objects"| s21
    s16 -->|"The result of one Quest operation"| s6
    s16 -->|"The result of one Quest operation"| s9
    s16 -->|"The result of one Quest operation"| s15
    s16 -->|"The result of one Quest operation"| s21
    s17 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s15
    s17 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s21
    s18 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s20
    s18 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s21
    s19 -->|"One routed turn with its context sources"| s21
    s20 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s17
    s20 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s21
    s22 -->|"A per-prompt table and the share of average model spend that is justified"| s6
    s23 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s2
    s23 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s17
```

## Queued-task runner (`run-queued-tasks`)

Takes a person's queued requests to a reported outcome, so they run on time without anyone having to start them by hand.

- Entry call: `quest_ai_runner.run("run-queued-tasks", 'team-1')`
- Selected implementation: Offline lifecycle harness (`qar_runner`)
- Metric: Task lifecycle correctness over offline cases
- Implemented by: [`quest_ai_runner/the_body/area_body.py`](the_body/area_body.py), [`quest_ai_runner/the_body/configuration/__init__.py`](the_body/configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](the_body/configuration/acp_deep_runner_config.py), [`quest_ai_runner/the_body/configuration/card_store_settings.py`](the_body/configuration/card_store_settings.py), [`quest_ai_runner/the_body/configuration/cli_config.py`](the_body/configuration/cli_config.py), [`quest_ai_runner/the_body/configuration/config.py`](the_body/configuration/config.py), [`quest_ai_runner/the_body/configuration/config_assembler.py`](the_body/configuration/config_assembler.py), [`quest_ai_runner/the_body/configuration/config_context_index.py`](the_body/configuration/config_context_index.py) and 116 more

## Critical Thinking (`critical-thinking`)

Makes sure everything the system does applies the relevant ESOI metric and leaves it improved, or at least not worse.

- Entry call: `quest_ai_runner.run("critical-thinking", ...)`
- Selected implementation: Code-enforced before and after on deep runs only (today) (`deep_runs_only`)
- Metric: Requests held to their ESOI metric by code
- Implemented by: [`evaluation/components/act_as_the_body/__init__.py`](../evaluation/components/act_as_the_body/__init__.py), [`evaluation/components/act_as_the_body/broken/__init__.py`](../evaluation/components/act_as_the_body/broken/__init__.py), [`evaluation/components/act_as_the_body/broken/blind_replay.py`](../evaluation/components/act_as_the_body/broken/blind_replay.py), [`evaluation/components/act_as_the_body/cases.json`](../evaluation/components/act_as_the_body/cases.json), [`evaluation/components/act_as_the_body/impls/__init__.py`](../evaluation/components/act_as_the_body/impls/__init__.py), [`evaluation/components/act_as_the_body/impls/card_and_reads.py`](../evaluation/components/act_as_the_body/impls/card_and_reads.py), [`evaluation/components/act_as_the_body/interface.py`](../evaluation/components/act_as_the_body/interface.py), [`evaluation/components/act_as_the_body/score.py`](../evaluation/components/act_as_the_body/score.py) and 930 more

## Multi-source evidence retriever (`gather-evidence`)

Gives a planner the material it needs to ground a decision or answer, drawn from the sources a person's work already lives in.

- Entry call: `quest_ai_runner.run("gather-evidence", 'NOTES.md')`
- Selected implementation: Parallel multi-source composite (`composite`)
- Metric: Retrieval recall of the right document over handbook questions
- Implemented by: [`quest_ai_runner/the_body/configuration/__init__.py`](the_body/configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](the_body/configuration/acp_deep_runner_config.py), [`quest_ai_runner/the_body/configuration/card_store_settings.py`](the_body/configuration/card_store_settings.py), [`quest_ai_runner/the_body/configuration/cli_config.py`](the_body/configuration/cli_config.py), [`quest_ai_runner/the_body/configuration/config.py`](the_body/configuration/config.py), [`quest_ai_runner/the_body/configuration/config_assembler.py`](the_body/configuration/config_assembler.py), [`quest_ai_runner/the_body/configuration/config_context_index.py`](the_body/configuration/config_context_index.py), [`quest_ai_runner/the_body/configuration/config_core_settings.py`](the_body/configuration/config_core_settings.py) and 57 more

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Queued-task runner<br/><i>run-queued-tasks</i>"]
    s1["Critical Thinking<br/><i>critical-thinking</i>"]
    s2["Multi-source evidence retriever<br/><i>gather-evidence</i>"]
    s3["Quest change executor<br/><i>operate-a-quest</i>"]
    s4["Bounded autonomous work runner<br/><i>run-deep-work</i>"]
    s5["Bounded reversible file writer<br/><i>write-files</i>"]
    s6["Allowlisted operation writer<br/><i>write-through-mcp</i>"]
    s7["World<br/><i>claim-reach</i>"]
    s8["Goal assignee and doability judge<br/><i>hand-off-a-goal</i>"]
    s9["Body<br/><i>the-body</i>"]
    s10["Quest account access<br/><i>access-quest-operations</i>"]
    s11["Mind<br/><i>the-mind</i>"]
    s12["Higher mind<br/><i>hold-the-intents</i>"]
    s13["Request intake<br/><i>take-in-the-world</i>"]
    s14["Subconscious<br/><i>the-subconscious</i>"]
    s15["Grounded goal agent<br/><i>do-grounded-work</i>"]
    s16["Justified model calls<br/><i>justify-model-calls</i>"]
    s17["Cost-ordered model tier selector with outage and allowance recovery<br/><i>model-tier-chooser</i>"]
    s0 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s1
    s0 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s9
    s1 -->|"Per request: the metric applied with its reading before and after, or the reason none applies, or a proposed metric; and for a regression or a missing metric, the response taken"| s15
    s2 -->|"Observation values carrying text, a path or source label, and a kind, with error observations in place of exceptions"| s9
    s3 -->|"The resulting quest state and a reply that reports what was done"| s1
    s3 -->|"The resulting quest state and a reply that reports what was done"| s9
    s4 -->|"A result stating whether the goal was met, the output text, and error or pause details such as a decision id"| s7
    s4 -->|"A result stating whether the goal was met, the output text, and error or pause details such as a decision id"| s9
    s5 -->|"A write result stating success, whether the file was created, bytes written, where the backup went and any error; the current text on read"| s7
    s5 -->|"A write result stating success, whether the file was created, bytes written, where the backup went and any error; the current text on read"| s9
    s6 -->|"A write result with success, error and an audit detail of the tool, arguments and whether it executed; a list of writable operations with schemas"| s7
    s6 -->|"A write result with success, error and an audit detail of the tool, arguments and whether it executed; a list of writable operations with schemas"| s9
    s7 -->|"A reach verdict for the request (inside, outside, in the world now, or not reachable, with why), and receipts for outside acts (what ran, where, its id)"| s9
    s8 -->|"The text or verdict the call returns"| s9
    s9 -->|"A current state view for the request's scope with the time each part was read, and after a change the read-back objects"| s15
    s10 -->|"The result of one Quest operation"| s0
    s10 -->|"The result of one Quest operation"| s3
    s10 -->|"The result of one Quest operation"| s9
    s10 -->|"The result of one Quest operation"| s15
    s11 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s9
    s11 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s15
    s12 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s14
    s12 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s15
    s13 -->|"One routed turn with its context sources"| s15
    s14 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s11
    s14 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s15
    s16 -->|"A per-prompt table and the share of average model spend that is justified"| s0
    s17 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s11
```

## Metrics

- Task lifecycle correctness over offline cases (`metric-run-queued-tasks`): leader `qar_runner`, 43 of 43 cases
- Requests held to their ESOI metric by code (`metric-think-critically`): leader `deep_runs_only`, 4 of 10 cases
- Retrieval recall of the right document over handbook questions (`metric-gather-evidence`): leader `composite`, 19 of 21 cases

## Problems

- Help That Does Not Know The Person (`QP2`): A general assistant answers from general knowledge.
- Unreliable Help With The App (`prob-app-help-unreliable`): Questions about how to use Quest, or what its AI can do for the person, are answered from guesses instead of the product's documentation and actual reach: the AI describes a capability it does not have, or says it cannot do something it can.
- Advice Without A Way To Check It (`prob-experiments-not-proposed`): Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose an experiment that would settle it, though the app has an Experiments feature built for that.
- Unrequested Or Partial Changes (`prob-unrequested-or-partial-changes`): A request to change a quest leads to changes that were not asked for, to only part of what was asked, or to the same change made twice.

## Ideals

- Personally Grounded Help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- Self-Explaining App (`ideal-self-explaining-app`): Any question about how to use the app gets a correct answer from its own documentation, and the AI knows what it can do.
- Most-Likely Path (`ideal-most-likely-path`): Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.
- Right Representative (`ideal-right-rep`): Each request is handled by the representative whose role and assigned quests fit it, and work spawned from a thread keeps that thread's representative unless the person chooses another.

## Where to go next

- Intent: [Grounded AI that advances people's goals](../esoi/README.md)
- Down: [quest_ai_runner/the_body](the_body/README.md)
- Down: [quest_ai_runner/the_consciousness](the_consciousness/README.md)
- Down: [quest_ai_runner/the_intelligence](the_intelligence/README.md)
- Down: [quest_ai_runner/the_self](the_self/README.md)
- Down: [quest_ai_runner/the_world/model_plumbing/model_tier_chooser](the_world/model_plumbing/model_tier_chooser/README.md)
- Down: [quest_ai_runner/the_world/model_plumbing/spend](the_world/model_plumbing/spend/README.md)
- Down: [quest_ai_runner/the_world/model_plumbing/vendor_model_caller](the_world/model_plumbing/vendor_model_caller/README.md)
- Down: [quest_ai_runner/the_world/tools/connected_system_operations](the_world/tools/connected_system_operations/README.md)
- Down: [quest_ai_runner/the_world/tools/delegated_agent_sessions](the_world/tools/delegated_agent_sessions/README.md)
- Down: [quest_ai_runner/the_world/tools/file_editing](the_world/tools/file_editing/README.md)
- Down: [quest_ai_runner/the_world/tools/file_reading](the_world/tools/file_reading/README.md)
- Down: [quest_ai_runner/the_world/tools/web_search](the_world/tools/web_search/README.md)
- Repository: [top README](../README.md)
