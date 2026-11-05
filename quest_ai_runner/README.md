# quest_ai_runner

*ESOI records kept beside the code in `quest_ai_runner`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote, and every answer and action rests on the person's own goals, material and preferences, and never on another person's.

These are the solutions of [Grounded AI that advances people's goals](../esoi/README.md).

## How the solutions fit together

quest-ai-runner turns a person's request or queued task into grounded work in one loop, and the Grounded goal agent is that loop. It asks the model through the Tiered model provider layer, works in the person's Quest account through Quest account access and takes in requests through Request intake. It reasons step by step through Mind, recalls what a request needs through Subconscious and acts on the account through Body. It holds its work to the metric that serves it through Critical Thinking, has every run watched through Nervous System and keeps the long view across turns through Higher mind. It stays comparable by metric through Integrity, answers questions beyond the person's own material through Science and chooses what a planner reads through Context choice. Subconscious recalls the cards that match a request through Hub card store. Tiered model provider layer accounts for every call through Cost estimation, daily budget and host load guard. Mind starts each step on the model tier it picks through Cost-ordered model tier selector with outage and allowance recovery. Nervous System has the model oversee a run through Tiered model provider layer. Queued-task runner claims, runs and reports a queued task, asking the model through Tiered model provider layer and working in the account through Quest account access. It writes each prompt as a registered spec with an owner and a metric through Justified model calls. Body reads and changes the quest through Context card persistence backend, Quest account access and Queued-task runner. Higher mind saves what it learned through Context card persistence backend. Request intake hears from a live channel through Live messaging channel transport.

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
    s20["Context choice<br/><i>choose-the-context</i>"]
    s21["Subconscious<br/><i>the-subconscious</i>"]
    s22["Grounded goal agent<br/><i>do-grounded-work</i>"]
    s23["Justified model calls<br/><i>justify-model-calls</i>"]
    s24["Cost-ordered model tier selector with outage and allowance recovery<br/><i>model-tier-chooser</i>"]
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s2
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s6
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s9
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s13
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s14
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s17
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s18
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s22
    s1 -->|"An inbound batch (messages with ids, chat references, text and attachments, a healthy flag and any error) and a send result with success and message id"| s13
    s1 -->|"An inbound batch (messages with ids, chat references, text and attachments, a healthy flag and any error) and a send result with success and message id"| s19
    s2 -->|"For a flagged case: the risk category, the response applied and its signal (proceed, redirect with a hint, answer now, escalate to deep execution, escalate to a person), with a reason and a degraded flag"| s22
    s3 -->|"A price per million input tokens, a bootstrap cost estimate with the provider and model used, a running total with an over-limit flag and status line, and a paused or not verdict"| s0
    s4 -->|"Checked records, recorded metric results and the selected implementation for each solution"| s22
    s5 -->|"Findings, each with its source, an evidence-strength label and the passage that supports it"| s22
    s6 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s7
    s6 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s15
    s7 -->|"Per request: the metric applied with its reading before and after, or the reason none applies, or a proposed metric; and for a regression or a missing metric, the response taken"| s22
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
    s15 -->|"A current state view for the request's scope with the time each part was read, and after a change the read-back objects"| s22
    s16 -->|"The result of one Quest operation"| s6
    s16 -->|"The result of one Quest operation"| s9
    s16 -->|"The result of one Quest operation"| s15
    s16 -->|"The result of one Quest operation"| s22
    s17 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s15
    s17 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s22
    s18 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s21
    s18 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s22
    s19 -->|"One routed turn with its context sources"| s22
    s20 -->|"The assembled context, the ids of the cards it drew on, a resolved reference, a ranked list of cards and records, or the cards nearest a query by meaning"| s22
    s21 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s17
    s21 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s22
    s23 -->|"A per-prompt table and the share of average model spend that is justified"| s6
    s24 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s2
    s24 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s17
```

## Queued-task runner (`run-queued-tasks`)

Takes a person's queued requests to a reported outcome, so they run on time without anyone having to start them by hand.

- Entry call: `quest_ai_runner.run("run-queued-tasks", 'team-1')`
- Selected implementation: Offline lifecycle harness (`qar_runner`)
- Metric: Task lifecycle correctness over offline cases
- Implemented by: [`quest_ai_runner/the_body/area_body.py`](the_body/area_body.py), [`quest_ai_runner/the_body/configuration/__init__.py`](the_body/configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](the_body/configuration/acp_deep_runner_config.py), [`quest_ai_runner/the_body/configuration/card_store_settings.py`](the_body/configuration/card_store_settings.py), [`quest_ai_runner/the_body/configuration/cli_config.py`](the_body/configuration/cli_config.py), [`quest_ai_runner/the_body/configuration/config.py`](the_body/configuration/config.py), [`quest_ai_runner/the_body/configuration/config_assembler.py`](the_body/configuration/config_assembler.py), [`quest_ai_runner/the_body/configuration/config_context_index.py`](the_body/configuration/config_context_index.py) and 110 more

## Critical Thinking (`critical-thinking`)

Makes sure everything the system does applies the relevant ESOI metric and leaves it improved, or at least not worse.

- Entry call: `quest_ai_runner.run("critical-thinking", ...)`
- Selected implementation: Code-enforced before and after on deep runs only (today) (`deep_runs_only`)
- Metric: Requests held to their ESOI metric by code
- Implemented by: [`evaluation/components/act_as_the_body/__init__.py`](../evaluation/components/act_as_the_body/__init__.py), [`evaluation/components/act_as_the_body/broken/__init__.py`](../evaluation/components/act_as_the_body/broken/__init__.py), [`evaluation/components/act_as_the_body/broken/blind_replay.py`](../evaluation/components/act_as_the_body/broken/blind_replay.py), [`evaluation/components/act_as_the_body/cases.json`](../evaluation/components/act_as_the_body/cases.json), [`evaluation/components/act_as_the_body/impls/__init__.py`](../evaluation/components/act_as_the_body/impls/__init__.py), [`evaluation/components/act_as_the_body/impls/card_and_reads.py`](../evaluation/components/act_as_the_body/impls/card_and_reads.py), [`evaluation/components/act_as_the_body/interface.py`](../evaluation/components/act_as_the_body/interface.py), [`evaluation/components/act_as_the_body/score.py`](../evaluation/components/act_as_the_body/score.py) and 929 more

## Multi-source evidence retriever (`gather-evidence`)

Gives a planner the material it needs to ground a decision or answer, drawn from the sources a person's work already lives in.

- Entry call: `quest_ai_runner.run("gather-evidence", 'NOTES.md')`
- Selected implementation: Parallel multi-source composite (`composite`)
- Metric: Retrieval recall of the right document over handbook questions
- Implemented by: [`quest_ai_runner/the_body/configuration/__init__.py`](the_body/configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](the_body/configuration/acp_deep_runner_config.py), [`quest_ai_runner/the_body/configuration/card_store_settings.py`](the_body/configuration/card_store_settings.py), [`quest_ai_runner/the_body/configuration/cli_config.py`](the_body/configuration/cli_config.py), [`quest_ai_runner/the_body/configuration/config.py`](the_body/configuration/config.py), [`quest_ai_runner/the_body/configuration/config_assembler.py`](the_body/configuration/config_assembler.py), [`quest_ai_runner/the_body/configuration/config_context_index.py`](the_body/configuration/config_context_index.py), [`quest_ai_runner/the_body/configuration/config_core_settings.py`](the_body/configuration/config_core_settings.py) and 55 more

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
    s14["Context choice<br/><i>choose-the-context</i>"]
    s15["Subconscious<br/><i>the-subconscious</i>"]
    s16["Grounded goal agent<br/><i>do-grounded-work</i>"]
    s17["Justified model calls<br/><i>justify-model-calls</i>"]
    s18["Cost-ordered model tier selector with outage and allowance recovery<br/><i>model-tier-chooser</i>"]
    s0 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s1
    s0 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s9
    s1 -->|"Per request: the metric applied with its reading before and after, or the reason none applies, or a proposed metric; and for a regression or a missing metric, the response taken"| s16
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
    s9 -->|"A current state view for the request's scope with the time each part was read, and after a change the read-back objects"| s16
    s10 -->|"The result of one Quest operation"| s0
    s10 -->|"The result of one Quest operation"| s3
    s10 -->|"The result of one Quest operation"| s9
    s10 -->|"The result of one Quest operation"| s16
    s11 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s9
    s11 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s16
    s12 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s15
    s12 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s16
    s13 -->|"One routed turn with its context sources"| s16
    s14 -->|"The assembled context, the ids of the cards it drew on, a resolved reference, a ranked list of cards and records, or the cards nearest a query by meaning"| s16
    s15 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s11
    s15 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s16
    s17 -->|"A per-prompt table and the share of average model spend that is justified"| s0
    s18 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s11
```

## Metrics

- Task lifecycle correctness over offline cases (`metric-run-queued-tasks`): leader `qar_runner`, 43 of 43 cases
- Requests held to their ESOI metric by code (`metric-think-critically`): leader `deep_runs_only`, 4 of 10 cases
- Retrieval recall of the right document over handbook questions (`metric-gather-evidence`): leader `composite`, 19 of 21 cases

## Problems

- A general assistant answers from general knowledge. (`QP2`)
- Questions about how to use the app itself (how to do something, what a feature is for) should be answered from the product's own documentation, but that path is untested, the AI sometimes says it cannot do what it can, and many team members do not know what the AI can do for them. (`prob-app-help-unreliable`)
- Most people have no external environment connected, so their delegated tasks can use only the app's own operations, its documentation, a review of their own data and web search, and that path has not been tested end to end. (`prob-no-environment-tasks-untested`)
- Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose or find an experiment that would settle it, though the app has an Experiments feature built for that. (`prob-experiments-not-proposed`)
- The before-and-after reading of the relevant ESOI metric is enforced by code only on deep runs. (`prob-metric-enforced-on-deep-runs-only`)
- People ask Quest AI why a Quest feature works as it does (why creating a quest asks many questions, what a daily reflection is for) and get a description of the feature, or a generic answer, instead of the reason it exists and how it helps their own goal. (`prob-quest-feature-why-unexplained`)
- A run can be judged against its goal condition by a reply that restates the condition as if it were met, so the done-standard is satisfied in words and no evidence shows the work was done. (`prob-goal-condition-restated-not-evidence`)
- A goal that covers a set (every entry, all records) can be reported as done after only part of the set was processed, because the reply reads well and does not admit the gap. (`prob-partial-coverage-reported-as-complete`)
- A long run shows nothing of itself until it ends, so the person cannot tell whether it is working, stuck or finished, and cannot steer it. (`prob-long-run-silent-until-the-end`)
- Running a coding-agent harness on the Claude Code subscription for each step of a request is costly in tokens and slow to start, while a cheap fast model alone lacks the context discipline and reliability of a long coding-agent session. (`prob-harness-dear-and-slow-on-the-subscription`)
- A request to change a quest leads to changes nobody asked for, to only part of what was asked, or to the same change made twice. (`prob-unrequested-or-partial-changes`)
- A planner decides or answers without the material that sits in the sources a person's work lives in, because each source is read its own way and one that fails stops the rest. (`prob-evidence-spread-across-sources`)

## Ideals

- Personally grounded help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- Grounded Reply (`ideal-grounded-reply`): A reply says only what the person's own account and its sources support, and says what it found.
- Reliable Verdict (`ideal-reliable-verdict`): A narrow judgment changes a turn only when it is usable.
- Checked Improvement per Run (`ideal-checked-improvement`): Every run leaves, on the record it served, what it measured, what it moved and what it changed in the record and why, so improvement is checked per run instead of hoped for.
- Continuous Mind (`ideal-continuous-mind`): Every step of a request acts with everything the request has established so far: what the person said, refused and corrected, what the request was resolved to mean, and what earlier steps found.
- Known Reach (`ideal-known-reach`): The AI says it can do only what it can, says it cannot only after checking, and claims an outside act only with the receipt of the act.
- Self-Explaining App (`ideal-self-explaining-app`): Any question about how to use the app gets a correct answer from its own documentation, and the AI knows what it can do.
- Most-Likely Path (`ideal-most-likely-path`): Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.
- Right Representative (`ideal-right-rep`): Each request is handled by the representative whose role and assigned quests fit it, and work spawned from a thread keeps that thread's representative unless the person chooses another.
- Explained Feature Purpose (`ideal-quest-feature-why-explained`): When a person asks why a Quest feature works as it does, the answer gives the reason the feature exists and how it serves their goal, not only what it is.
- Evidenced Completion (`ideal-done-standard-evidenced`): Work is reported as done only when evidence shows its written done-standard was met, and the evidence is not a restatement of the standard.
- Stated Coverage (`ideal-coverage-met-or-gap-stated`): When a goal covers a set (every entry, all records), either the whole set was processed or the report states exactly how much was and what remains.
- Visible Progress (`ideal-incremental-output-while-working`): While work is running, the person can see that it is still working and what it has done so far, at a level they choose.
- Lean Context Harness (`ideal-cheap-fast-harness-with-ideal-context`): The harness that runs Quest AI's steps is as capable as a coding agent but costs and takes about what the fast tier does, because every step sees exactly the context its goal needs.

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
