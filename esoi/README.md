# Grounded AI that advances people's goals

Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote, and every answer and action rests on the person's own goals, material and preferences, and never on another person's.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

Opportunities: Other products that need an AI which works from a person's own goals and material could reuse this engine, so the grounding, the standard for delegated work and the scope fences are built once and shared.

## How the solutions fit together

quest-ai-runner turns a person's request or queued task into grounded work in one flow, and the Grounded goal agent is that flow. It takes the request in through Request intake, says what the person is working toward through Higher mind and chooses the context the request needs through Context choice. It reasons step by step through Mind, acts on the account through Body and Quest account access, and answers questions beyond the person's own material through Science. It has every run watched through Nervous System and holds the work to the metric that serves it through Critical Thinking. It asks the model through the Tiered model provider layer and stays comparable by metric through Integrity. It recalls what a request needs through Subconscious. Subconscious recalls the cards that match a request through Hub card store. Tiered model provider layer accounts for every call through Cost estimation, daily budget and host load guard. Mind starts each step on the model tier it picks through Cost-ordered model tier selector with outage and allowance recovery. Nervous System has the model oversee a run through Tiered model provider layer. Queued-task runner claims, runs and reports a queued task, asking the model through Tiered model provider layer and working in the account through Quest account access. It writes each prompt as a registered spec with an owner and a metric through Justified model calls. Body reads and changes the quest through Context card persistence backend, Quest account access and Queued-task runner. Higher mind saves what it learned through Context card persistence backend. Request intake hears from a live channel through Live messaging channel transport. Body changes the quest as the person asks through Quest change executor and writes files inside one folder through Bounded reversible file writer. Body reads from the sources the person's work lives in through Multi-source evidence retriever and hands a new goal to the right owner through Goal assignee and doability judge. Body does heavy work through Bounded autonomous work runner, makes one named change on an outside tool server through Allowlisted operation writer and claims only the reach that World grants. Quest change executor reaches the account through Quest account access and decides through Tiered model provider layer. Goal assignee and doability judge asks the model whether a goal is doable through Tiered model provider layer. World reaches a person through Live messaging channel transport and judges through Tiered model provider layer.

## Diagram

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

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Tiered model provider layer | `vendor-model-caller` | `quest_ai_runner.run("vendor-model-caller", ...)` | Prefix-routing multi-provider (`multi_provider`) | Provider routing, fallback, retry and cost correctness |
| Live messaging channel transport | `talk-over-a-channel` | `quest_ai_runner.run("talk-over-a-channel")` | MCP gateway bridge transport (`openclaw`) | Channel transport conformance to the gateway wire contract |
| Nervous System | `nervous-system` | `quest_ai_runner.run("nervous-system", 'delete the old backup folder')` | Digest-driven overseer (`overseer`) | Overseer digest evidence and signal correctness on labelled scenarios |
| Cost estimation, daily budget and host load guard | `track-usage-and-cost` | no entry declared | Live usage evaluation arm (`qar_usage`) | Cost, budget and resource-guard correctness |
| Integrity | `system-integrity` | `quest_ai_runner.run("system-integrity", ['examplevendor'])` | Apply the evolvable-structure library (`structure-library`) | Parts selected by measurement, Correct selection decisions over the evolve-by-metric cases, Intents whose solutions one paragraph and one diagram explain as the code builds them, Hub of hubs |
| Science | `the-science` | `quest_ai_runner.run("the-science", 'meditation and attention')` | Web research with the answer's support check (today) (`web_with_support_check`) | Supported and calibrated answers to world questions |
| Queued-task runner | `run-queued-tasks` | `quest_ai_runner.run("run-queued-tasks", 'team-1')` | Offline lifecycle harness (`qar_runner`) | Task lifecycle correctness over offline cases |
| Critical Thinking | `critical-thinking` | `quest_ai_runner.run("critical-thinking", ...)` | Code-enforced before and after on deep runs only (today) (`deep_runs_only`) | Requests held to their ESOI metric by code |
| Multi-source evidence retriever | `gather-evidence` | `quest_ai_runner.run("gather-evidence", 'NOTES.md')` | Parallel multi-source composite (`composite`) | Retrieval recall of the right document over handbook questions |
| Quest change executor | `operate-a-quest` | `quest_ai_runner.run("operate-a-quest", 'Add a goal to ship the launch note by Friday')` | Planner-only orchestration (`qar_orchestrator`) | Final quest state against the request over the quest operation cases |
| Bounded autonomous work runner | `run-deep-work` | no entry declared | Subprocess goal runner (`subprocess`) | Outcome reporting correctness across deep runners, Goal loop outcomes against the done-standard, Deep worker launch settings ranked by cost per goal met |
| Bounded reversible file writer | `write-files` | `quest_ai_runner.run("write-files", 'NOTES.md')` | Contained backed-up writer (`files`) | Write boundary and reversibility over the scratch-tree cases |
| Allowlisted operation writer | `write-through-mcp` | no entry declared | MCP operation writer (`mcp`) | Allowlist enforcement and exactly-once execution |
| World | `claim-reach` | `quest_ai_runner.run("claim-reach", 'Send the launch note to the whole team')` | Coding agent, reach described in the prompt (today) (`coding_agent_prompt_reach`) | Combined world score |
| Goal assignee and doability judge | `hand-off-a-goal` | `quest_ai_runner.run("hand-off-a-goal", ...)` | Two small model verdicts when a goal is created (today) (`model_judge`) | Hand-offs a person keeps per unit of cost and time |
| Body | `the-body` | `quest_ai_runner.run("the-body", '')` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Quest account access | `access-quest-operations` | `quest_ai_runner.run("access-quest-operations", 'quest-id')` | Quest HTTP client from the environment (`quest_client`) | Parity of Quest operations across access points |
| Mind | `the-mind` | `quest_ai_runner.run("the-mind", ...)` | Rebuilt prompt per step with a referenced context (today) (`rebuilt_per_step`) | Step continuity at cost over multi-step requests |
| Higher mind | `hold-the-intents` | `quest_ai_runner.run("hold-the-intents", ...)` | Separate stores read at the start of a request (today) (`separate_stores`) | Representative long-view carry-through across assigned quests, with nothing outside its branch |
| Request intake | `take-in-the-world` | `quest_ai_runner.run("take-in-the-world", 'Save a note that the launch moves to Friday.')` | Channels, terminal and package as ways in (`ways_in`) | Ways in that pass their own metric |
| Subconscious | `the-subconscious` | `quest_ai_runner.run("the-subconscious", 'rust release')` | Start-of-run card filter and a recency window (today) (`recency_window`) | Goal-relevant admission at a precision floor with nothing out of scope |
| Grounded goal agent | `do-grounded-work` | `quest_ai_runner.run("do-grounded-work", 'What should I work on first this week?')` | quest-ai-runner library (`library`) | QAR overall score, Passing chat turns per dollar, Delegated tasks meeting their standard, Exact and consented changes, Data kept in scope, Engine reuse, Measured coverage |
| Justified model calls | `justify-model-calls` | no entry declared | Registered prompt specs with an enforcement test (today) (`spec_registry`) | Justified share of model spend |
| Cost-ordered model tier selector with outage and allowance recovery | `model-tier-chooser` | `quest_ai_runner.run("model-tier-chooser", {'available': ['anthropic']})` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |

## Problems and ideals

**Problems**

- Different Science Evidence In Two Chats (`prob-science-search-duplicates-research-chat`): One science question can get different evidence in Quest chat than in Quest's research chat.
- Help That Does Not Know The Person (`QP2`): A general assistant answers from general knowledge.
- Unreliable Help With The App (`prob-app-help-unreliable`): Questions about how to use Quest, or what its AI can do for the person, are answered from guesses instead of the product's documentation and actual reach: the AI describes a capability it does not have, or says it cannot do something it can.
- Advice Without A Way To Check It (`prob-experiments-not-proposed`): Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose an experiment that would settle it, though the app has an Experiments feature built for that.
- Unrequested Or Partial Changes (`prob-unrequested-or-partial-changes`): A request to change a quest leads to changes that were not asked for, to only part of what was asked, or to the same change made twice.
- Contradictory Account State (`prob-account-state-contradicts`): What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it can report the same state two ways, repeat a change it already made, or propose again what it said was done.
- Lost Or Unreported Tasks (`QP1`): Quest tasks attached to goals can be lost, run twice, or end without an outcome the person sees.
- Decisions Not Reaching The Person (`QP3`): A step only a person on the quest may decide can be taken without them, reach the wrong teammate, or be dropped before they answer.
- Different Answers On Different Surfaces (`QP4`): The same request can get a different answer depending on the Quest surface it came from.

**Ideals**

- Exact Change (`ideal-exact-change`): The final state matches the request: everything asked, nothing else, once.
- Dependable Delivery (`ideal-dependable-delivery`): What is handed to the system arrives once, in order, and is accounted for.
- Consistent Science Evidence (`ideal-science-through-one-search`): A science question gets the same evidence, weighed the same way, in Quest chat and in the research chat.
- Personally Grounded Help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- Self-Explaining App (`ideal-self-explaining-app`): Any question about how to use the app gets a correct answer from its own documentation, and the AI knows what it can do.
- Most-Likely Path (`ideal-most-likely-path`): Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.
- Right Representative (`ideal-right-rep`): Each request is handled by the representative whose role and assigned quests fit it, and work spawned from a thread keeps that thread's representative unless the person chooses another.
- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Trusted Autonomy (`QI3`): The AI acts first on what it may decide and brings the person exactly the decisions that are theirs.
- Same Assistant Everywhere (`QI4`): A person gets the same assistant, with the same answer to the same request, on every Quest surface.

## Metrics and their leaders

- Provider routing, fallback, retry and cost correctness (`metric-call-a-model`): leader `multi_provider`, 56 of 56 cases
- Channel transport conformance to the gateway wire contract (`metric-talk-over-a-channel`): leader `openclaw_bridge`, 20 of 20 cases
- Overseer digest evidence and signal correctness on labelled scenarios (`metric-watch-a-run`): leader `digest_probe`, 16 of 16 cases
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): leader `qar_usage`, 38 of 39 cases
- Parts selected by measurement (`QME7`): leader `structure_library`, 13 of 17 cases
- Supported and calibrated answers to world questions (`metric-know-the-world`): leader `web_with_support_check`, 9 of 9 cases
- Hub of hubs (`metric-hub-of-hubs`): leader `registry_scan`, 244 of 271 cases
- Hub of hubs (`QME9`): leader `registry_scan`, 244 of 271 cases
- Correct selection decisions over the evolve-by-metric cases (`metric-metric-earns-its-run`): leader `structure_library`, 13 of 17 cases
- Intents whose solutions one paragraph and one diagram explain as the code builds them (`metric-solution-coherence`)
- and 28 more: all 38 are in [metrics](#metrics) of the full record below

## Where to go next

- Sub-intent: [Body](../quest_ai_runner/the_body/internal_context/README.md) (What the AI says about a person's account is the account as it is now.)
- Sub-intent: [Taking in requests from anywhere](../quest_ai_runner/the_consciousness/request_intake/README.md) (A person or program reaches the runner from wherever they work and is understood the same way.)
- Sub-intent: [Critical Thinking](../quest_ai_runner/the_intelligence/critical_thinking/README.md) (Everything the AI does and says is continuously held to the metric that serves the person's goals.)
- Sub-intent: [Mind](../quest_ai_runner/the_intelligence/mind/README.md) (Every request is answered from everything its work has established so far, in the best way possible.)
- Sub-intent: [Nervous System](../quest_ai_runner/the_intelligence/nervous_system/README.md) (The AI system watches every run and brings in a person only where only a person can decide.)
- Sub-intent: [Subconscious](../quest_ai_runner/the_intelligence/subconscious/README.md) (The AI optimally recalls the part of the person's own knowledge that each request needs.)
- Sub-intent: [Integrity](../quest_ai_runner/the_self/integrity/README.md) (Every part of QAR is a specified solution whose best implementation runs as the state of the art moves.)
- Sub-intent: [Science](../quest_ai_runner/the_world/science/README.md) (What a request needs from beyond the person's own material comes from the world, with its source.)
- Sub-intent: [Holding the intents across quests](../quest_ai_runner/the_intelligence/higher_mind/objectives/README.md) (Each AI representative continuously acts from the standing view of the person's quests and intents.)
- Repository: [top README](../README.md)
- Records beside the code: [quest_ai_runner](../quest_ai_runner/README.md), [quest_ai_runner/the_body](../quest_ai_runner/the_body/README.md), [quest_ai_runner/the_consciousness](../quest_ai_runner/the_consciousness/README.md), [quest_ai_runner/the_intelligence](../quest_ai_runner/the_intelligence/README.md), [quest_ai_runner/the_self](../quest_ai_runner/the_self/README.md), [quest_ai_runner/the_body/configuration](../quest_ai_runner/the_body/configuration/README.md), [quest_ai_runner/the_body/host](../quest_ai_runner/the_body/host/README.md), [quest_ai_runner/the_consciousness/representatives](../quest_ai_runner/the_consciousness/representatives/README.md), [quest_ai_runner/the_world/model_plumbing/model_tier_chooser](../quest_ai_runner/the_world/model_plumbing/model_tier_chooser/README.md), [quest_ai_runner/the_world/model_plumbing/spend](../quest_ai_runner/the_world/model_plumbing/spend/README.md), [quest_ai_runner/the_world/model_plumbing/vendor_model_caller](../quest_ai_runner/the_world/model_plumbing/vendor_model_caller/README.md), [quest_ai_runner/the_world/tools/connected_system_operations](../quest_ai_runner/the_world/tools/connected_system_operations/README.md), [quest_ai_runner/the_world/tools/delegated_agent_sessions](../quest_ai_runner/the_world/tools/delegated_agent_sessions/README.md), [quest_ai_runner/the_world/tools/file_editing](../quest_ai_runner/the_world/tools/file_editing/README.md), [quest_ai_runner/the_world/tools/file_reading](../quest_ai_runner/the_world/tools/file_reading/README.md), [quest_ai_runner/the_world/tools/web_search](../quest_ai_runner/the_world/tools/web_search/README.md), [evaluation/components/choose_a_model_tier/impls](../evaluation/components/choose_a_model_tier/impls/README.md), [evaluation/components/claim_reach/impls](../evaluation/components/claim_reach/impls/README.md), [evaluation/components/deep_run/impls](../evaluation/components/deep_run/impls/README.md), [evaluation/components/deep_runner/impls](../evaluation/components/deep_runner/impls/README.md), [evaluation/components/evolve_by_metric/impls](../evaluation/components/evolve_by_metric/impls/README.md), [evaluation/components/hand_off_a_goal/impls](../evaluation/components/hand_off_a_goal/impls/README.md), [evaluation/components/hub_of_hubs/impls](../evaluation/components/hub_of_hubs/impls/README.md), [evaluation/components/know_the_world/impls](../evaluation/components/know_the_world/impls/README.md), [evaluation/components/model_providers/impls](../evaluation/components/model_providers/impls/README.md), [evaluation/components/overseer_signals/impls](../evaluation/components/overseer_signals/impls/README.md), [evaluation/components/quest_ops/impls](../evaluation/components/quest_ops/impls/README.md), [evaluation/components/talk_over_a_channel/impls](../evaluation/components/talk_over_a_channel/impls/README.md), [evaluation/components/track_usage_and_cost/impls](../evaluation/components/track_usage_and_cost/impls/README.md), [evaluation/components/write_files/impls](../evaluation/components/write_files/impls/README.md), [evaluation/components/write_through_mcp/impls](../evaluation/components/write_through_mcp/impls/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- Different Science Evidence In Two Chats (`prob-science-search-duplicates-research-chat`): One science question can get different evidence in Quest chat than in Quest's research chat.
- Help That Does Not Know The Person (`QP2`): A general assistant answers from general knowledge.
- Unreliable Help With The App (`prob-app-help-unreliable`): Questions about how to use Quest, or what its AI can do for the person, are answered from guesses instead of the product's documentation and actual reach: the AI describes a capability it does not have, or says it cannot do something it can.
- Advice Without A Way To Check It (`prob-experiments-not-proposed`): Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose an experiment that would settle it, though the app has an Experiments feature built for that.
- Unrequested Or Partial Changes (`prob-unrequested-or-partial-changes`): A request to change a quest leads to changes that were not asked for, to only part of what was asked, or to the same change made twice.
- Contradictory Account State (`prob-account-state-contradicts`): What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it can report the same state two ways, repeat a change it already made, or propose again what it said was done.
- Lost Or Unreported Tasks (`QP1`): Quest tasks attached to goals can be lost, run twice, or end without an outcome the person sees.
- Decisions Not Reaching The Person (`QP3`): A step only a person on the quest may decide can be taken without them, reach the wrong teammate, or be dropped before they answer.
- Different Answers On Different Surfaces (`QP4`): The same request can get a different answer depending on the Quest surface it came from.

### Ideals

- Exact Change (`ideal-exact-change`): The final state matches the request: everything asked, nothing else, once.
- Dependable Delivery (`ideal-dependable-delivery`): What is handed to the system arrives once, in order, and is accounted for.
- Consistent Science Evidence (`ideal-science-through-one-search`): A science question gets the same evidence, weighed the same way, in Quest chat and in the research chat.
- Personally Grounded Help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- Self-Explaining App (`ideal-self-explaining-app`): Any question about how to use the app gets a correct answer from its own documentation, and the AI knows what it can do.
- Most-Likely Path (`ideal-most-likely-path`): Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.
- Right Representative (`ideal-right-rep`): Each request is handled by the representative whose role and assigned quests fit it, and work spawned from a thread keeps that thread's representative unless the person chooses another.
- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Trusted Autonomy (`QI3`): The AI acts first on what it may decide and brings the person exactly the decisions that are theirs.
- Same Assistant Everywhere (`QI4`): A person gets the same assistant, with the same answer to the same request, on every Quest surface.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Tiered model provider layer | `vendor-model-caller` | Prefix-routing multi-provider (`multi_provider`) | Provider routing, fallback, retry and cost correctness |
| Live messaging channel transport | `talk-over-a-channel` | MCP gateway bridge transport (`openclaw`) | Channel transport conformance to the gateway wire contract |
| Nervous System | `nervous-system` | Digest-driven overseer (`overseer`) | Overseer digest evidence and signal correctness on labelled scenarios |
| Cost estimation, daily budget and host load guard | `track-usage-and-cost` | Live usage evaluation arm (`qar_usage`) | Cost, budget and resource-guard correctness |
| Integrity | `system-integrity` | Apply the evolvable-structure library (`structure-library`) | Parts selected by measurement, Correct selection decisions over the evolve-by-metric cases, Intents whose solutions one paragraph and one diagram explain as the code builds them, Hub of hubs |
| Science | `the-science` | Web research with the answer's support check (today) (`web_with_support_check`) | Supported and calibrated answers to world questions |
| Queued-task runner | `run-queued-tasks` | Offline lifecycle harness (`qar_runner`) | Task lifecycle correctness over offline cases |
| Critical Thinking | `critical-thinking` | Code-enforced before and after on deep runs only (today) (`deep_runs_only`) | Requests held to their ESOI metric by code |
| Multi-source evidence retriever | `gather-evidence` | Parallel multi-source composite (`composite`) | Retrieval recall of the right document over handbook questions |
| Quest change executor | `operate-a-quest` | Planner-only orchestration (`qar_orchestrator`) | Final quest state against the request over the quest operation cases |
| Bounded autonomous work runner | `run-deep-work` | Subprocess goal runner (`subprocess`) | Outcome reporting correctness across deep runners, Goal loop outcomes against the done-standard, Deep worker launch settings ranked by cost per goal met |
| Bounded reversible file writer | `write-files` | Contained backed-up writer (`files`) | Write boundary and reversibility over the scratch-tree cases |
| Allowlisted operation writer | `write-through-mcp` | MCP operation writer (`mcp`) | Allowlist enforcement and exactly-once execution |
| World | `claim-reach` | Coding agent, reach described in the prompt (today) (`coding_agent_prompt_reach`) | Combined world score |
| Goal assignee and doability judge | `hand-off-a-goal` | Two small model verdicts when a goal is created (today) (`model_judge`) | Hand-offs a person keeps per unit of cost and time |
| Body | `the-body` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Quest account access | `access-quest-operations` | Quest HTTP client from the environment (`quest_client`) | Parity of Quest operations across access points |
| Mind | `the-mind` | Rebuilt prompt per step with a referenced context (today) (`rebuilt_per_step`) | Step continuity at cost over multi-step requests |
| Higher mind | `hold-the-intents` | Separate stores read at the start of a request (today) (`separate_stores`) | Representative long-view carry-through across assigned quests, with nothing outside its branch |
| Request intake | `take-in-the-world` | Channels, terminal and package as ways in (`ways_in`) | Ways in that pass their own metric |
| Subconscious | `the-subconscious` | Start-of-run card filter and a recency window (today) (`recency_window`) | Goal-relevant admission at a precision floor with nothing out of scope |
| Grounded goal agent | `do-grounded-work` | quest-ai-runner library (`library`) | QAR overall score, Passing chat turns per dollar, Delegated tasks meeting their standard, Exact and consented changes, Data kept in scope, Engine reuse, Measured coverage |
| Justified model calls | `justify-model-calls` | Registered prompt specs with an enforcement test (today) (`spec_registry`) | Justified share of model spend |
| Cost-ordered model tier selector with outage and allowance recovery | `model-tier-chooser` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Per-consumer worker | `do-grounded-work:per-consumer-worker` |  |  |
| Agent framework with Quest glue | `do-grounded-work:agent-framework-with-quest-glue` |  |  |
| Deep runner only | `do-grounded-work:deep-runner-only` |  |  |
| Carried message stream per request (proposed) | `the-mind:append_only_stream` |  |  |
| Carried stream with compaction (proposed) | `the-mind:stream_with_compaction` |  |  |
| Small-model judge of every candidate (proposed) | `the-subconscious:goal_judge` |  |  |
| Score thresholds per source (proposed) | `the-subconscious:score_thresholds` |  |  |
| Thresholds with a judge on the borderline (proposed) | `the-subconscious:thresholds_then_judge` |  |  |
| Host research sources first, then the web (proposed) | `the-science:research_sources_first` |  |  |
| Findings weighed by a host's evidence-estimation service (proposed) | `the-science:evidence_weighted` |  |  |
| Read back what a change touched (proposed) | `the-body:read_after_write` |  |  |
| A live read behind every state claim (proposed) | `the-body:live_read_only` |  |  |
| Maintained view per quest only (proposed) | `hold-the-intents:per_quest_note` |  |  |
| Periodic reflection on the best tier (proposed) | `hold-the-intents:periodic_reflection` |  |  |
| Outcome precedents surfaced at admission (proposed) | `the-subconscious:outcome_precedents` |  |  |
| Overseer that asks questions of fact (proposed) | `nervous-system:fact_questions` |  |  |
| Every request measured by code (proposed) | `critical-thinking:every_request_measured` |  |  |
| Measured, proposed and answered (proposed) | `critical-thinking:measured_proposed_and_answered` |  |  |
| Per-risk response chosen by metric (proposed) | `nervous-system:per_risk_response` |  |  |
| One view per intent, cascading down (proposed) | `hold-the-intents:per_intent_cascade` |  |  |
| One view per representative across its assigned quests, placed in the hierarchy (proposed, recommended) | `hold-the-intents:per_rep_assigned_view` |  |  |
| Registry plus per-call ablation against the owner metric (proposed) | `justify-model-calls:spec_registry_ablation` |  |  |
| Subprocess goal runner | `run-deep-work:subprocess` | [quest_ai_runner/the_intelligence/mind/delegation/goal_runner.py](../quest_ai_runner/the_intelligence/mind/delegation/goal_runner.py) |  |
| Coding agent with the host reach summary and reach judge (built, off by default) | `claim-reach:coding_agent_reach_judge` | [quest_ai_runner/the_intelligence/mind/planner/reach_judge.py](../quest_ai_runner/the_intelligence/mind/planner/reach_judge.py) |  |
| Coding agent with one declared inventory and receipts (proposed) | `claim-reach:coding_agent_declared_inventory` |  |  |
| Best open-source agent harness by the combined metric, with declared inventory (proposed) | `claim-reach:best_open_source_harness` |  |  |
| Offline lifecycle harness | `run-queued-tasks:qar_runner` | [quest_ai_runner/the_body/area_body.py](../quest_ai_runner/the_body/area_body.py) |  |
| Managed quest card plus optional live reads (today) | `the-body:card_and_reads` | [quest_ai_runner/the_body/area_body.py](../quest_ai_runner/the_body/area_body.py) | [internal_context](internal_context/README.md) |
| Quest HTTP client from the environment | `access-quest-operations:quest_client` | [quest_ai_runner/the_body/area_body.py](../quest_ai_runner/the_body/area_body.py) |  |
| Planner-only orchestration | `operate-a-quest:qar_orchestrator` | [quest_ai_runner/the_body/area_body.py](../quest_ai_runner/the_body/area_body.py) |  |
| Coding agent, reach described in the prompt (today) | `claim-reach:coding_agent_prompt_reach` | [quest_ai_runner/the_body/area_body.py](../quest_ai_runner/the_body/area_body.py) |  |
| Two small model verdicts when a goal is created (today) | `hand-off-a-goal:model_judge` | [quest_ai_runner/the_body/area_body.py](../quest_ai_runner/the_body/area_body.py) |  |
| Task poller and claimer | `run-queued-tasks:poller` | [quest_ai_runner/the_consciousness/request_intake/poller.py](../quest_ai_runner/the_consciousness/request_intake/poller.py) |  |
| Single-task executor | `run-queued-tasks:executor` | [quest_ai_runner/the_consciousness/request_intake/executor.py](../quest_ai_runner/the_consciousness/request_intake/executor.py) |  |
| Shared lane entry point | `run-queued-tasks:lane` | [quest_ai_runner/the_consciousness/request_intake/lane.py](../quest_ai_runner/the_consciousness/request_intake/lane.py) |  |
| Digest-driven overseer | `nervous-system:overseer` | [quest_ai_runner/the_intelligence/nervous_system/area.py](../quest_ai_runner/the_intelligence/nervous_system/area.py) | [nervous_system](nervous_system/README.md) |
| Apply the evolvable-structure library | `system-integrity:structure-library` | [quest_ai_runner/the_intelligence/critical_thinking/response_quality/area.py](../quest_ai_runner/the_intelligence/critical_thinking/response_quality/area.py) | [integrity](integrity/README.md) |
| Rebuilt prompt per step with a referenced context (today) | `the-mind:rebuilt_per_step` | [quest_ai_runner/the_intelligence/mind/area_mind.py](../quest_ai_runner/the_intelligence/mind/area_mind.py) | [mind](mind/README.md) |
| Separate stores read at the start of a request (today) | `hold-the-intents:separate_stores` | [quest_ai_runner/the_intelligence/higher_mind/objectives/area.py](../quest_ai_runner/the_intelligence/higher_mind/objectives/area.py) | [objectives](objectives/README.md) |
| Code-enforced before and after on deep runs only (today) | `critical-thinking:deep_runs_only` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](../quest_ai_runner/the_intelligence/critical_thinking/area.py) | [critical_thinking](critical_thinking/README.md) |
| Channels, terminal and package as ways in | `take-in-the-world:ways_in` | [quest_ai_runner/the_intelligence/mind/planner/area_take_in_the_world.py](../quest_ai_runner/the_intelligence/mind/planner/area_take_in_the_world.py) | [request_intake](request_intake/README.md) |
| Rebuilt prompt per step with the context re-sent from cache | `the-mind:rebuilt_full_context` | [quest_ai_runner/the_intelligence/mind/planner/orchestrator_planning.py](../quest_ai_runner/the_intelligence/mind/planner/orchestrator_planning.py) |  |
| Start-of-run card filter and a recency window (today) | `the-subconscious:recency_window` | [quest_ai_runner/the_intelligence/mind/context_intake/planner_context.py](../quest_ai_runner/the_intelligence/mind/context_intake/planner_context.py) | [subconscious](subconscious/README.md) |
| No overseer (configuration off) | `nervous-system:no_overseer` | [quest_ai_runner/the_intelligence/mind/orchestrator_types.py](../quest_ai_runner/the_intelligence/mind/orchestrator_types.py) |  |
| Work-folder sync | `run-queued-tasks:quest_folder_sync` | [quest_ai_runner/the_intelligence/higher_mind/objectives/quest_folder_sync.py](../quest_ai_runner/the_intelligence/higher_mind/objectives/quest_folder_sync.py) |  |
| quest-ai-runner library | `do-grounded-work:library` | [quest_ai_runner/the_self/respond.py](../quest_ai_runner/the_self/respond.py) |  |
| Standing instruction alone (floor) | `critical-thinking:instruction_only` | [quest_ai_runner/the_self/integrity/prompts/standing_instructions.py](../quest_ai_runner/the_self/integrity/prompts/standing_instructions.py) |  |
| Registered prompt specs with an enforcement test (today) | `justify-model-calls:spec_registry` | [quest_ai_runner/the_self/integrity/prompts/prompt_specs.py](../quest_ai_runner/the_self/integrity/prompts/prompt_specs.py) |  |
| One-call fast file editor | `run-deep-work:fast_edit` | [quest_ai_runner/the_body/configuration/config_resolvers.py](../quest_ai_runner/the_body/configuration/config_resolvers.py) |  |
| One-call MCP operation runner | `run-deep-work:mcp_write` | [quest_ai_runner/the_body/configuration/config_resolvers.py](../quest_ai_runner/the_body/configuration/config_resolvers.py) |  |
| Lean guarded launch of the deep worker | `run-deep-work:lean_guarded` | [quest_ai_runner/the_body/configuration/deep_launch.py](../quest_ai_runner/the_body/configuration/deep_launch.py) |  |
| Hysteresis resource guard | `track-usage-and-cost:resources` | [quest_ai_runner/the_body/host/resources.py](../quest_ai_runner/the_body/host/resources.py) |  |
| Configured persona resolver | `run-queued-tasks:personas` | [quest_ai_runner/the_consciousness/representatives/personas.py](../quest_ai_runner/the_consciousness/representatives/personas.py) |  |
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py](../quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py](../quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py) |  |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py](../quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py) |  |
| Static price table lookup | `track-usage-and-cost:pricing` | [quest_ai_runner/the_world/model_plumbing/spend/pricing.py](../quest_ai_runner/the_world/model_plumbing/spend/pricing.py) |  |
| Persisted daily token tracker | `track-usage-and-cost:usage` | [quest_ai_runner/the_world/model_plumbing/spend/usage.py](../quest_ai_runner/the_world/model_plumbing/spend/usage.py) |  |
| Prefix-routing multi-provider | `vendor-model-caller:multi_provider` | [quest_ai_runner/the_world/model_plumbing/vendor_model_caller/providers.py](../quest_ai_runner/the_world/model_plumbing/vendor_model_caller/providers.py) |  |
| Keyless command-line provider | `vendor-model-caller:claude_cli` | [quest_ai_runner/the_world/model_plumbing/vendor_model_caller/claude_cli_provider.py](../quest_ai_runner/the_world/model_plumbing/vendor_model_caller/claude_cli_provider.py) |  |
| MCP gateway bridge transport | `talk-over-a-channel:openclaw` | [quest_ai_runner/the_world/tools/connected_system_operations/openclaw_channel.py](../quest_ai_runner/the_world/tools/connected_system_operations/openclaw_channel.py) |  |
| Parallel multi-source composite | `gather-evidence:composite` | [quest_ai_runner/the_world/tools/connected_system_operations/composite_retrieval_adapter.py](../quest_ai_runner/the_world/tools/connected_system_operations/composite_retrieval_adapter.py) |  |
| MCP server reader | `gather-evidence:mcp` | [quest_ai_runner/the_world/tools/connected_system_operations/mcp_retrieval_adapter.py](../quest_ai_runner/the_world/tools/connected_system_operations/mcp_retrieval_adapter.py) |  |
| Notion database reader | `gather-evidence:notion` | [quest_ai_runner/the_world/tools/connected_system_operations/notion_adapter.py](../quest_ai_runner/the_world/tools/connected_system_operations/notion_adapter.py) |  |
| Drive document reader | `gather-evidence:google_drive` | [quest_ai_runner/the_world/tools/connected_system_operations/google_drive_adapter.py](../quest_ai_runner/the_world/tools/connected_system_operations/google_drive_adapter.py) |  |
| Chat conversation reader | `gather-evidence:google_chat` | [quest_ai_runner/the_world/tools/connected_system_operations/google_chat_adapter.py](../quest_ai_runner/the_world/tools/connected_system_operations/google_chat_adapter.py) |  |
| MCP operation writer | `write-through-mcp:mcp` | [quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py](../quest_ai_runner/the_world/tools/connected_system_operations/mcp_write_adapter.py) |  |
| Agent-protocol deep runner | `run-deep-work:acp` | [quest_ai_runner/the_world/tools/delegated_agent_sessions/acp_deep_runner.py](../quest_ai_runner/the_world/tools/delegated_agent_sessions/acp_deep_runner.py) |  |
| Persistent stream-json runner | `run-deep-work:stream_json` | [quest_ai_runner/the_world/tools/delegated_agent_sessions/goal_runner_stream.py](../quest_ai_runner/the_world/tools/delegated_agent_sessions/goal_runner_stream.py) |  |
| Contained backed-up writer | `write-files:files` | [quest_ai_runner/the_world/tools/file_editing/files_writer.py](../quest_ai_runner/the_world/tools/file_editing/files_writer.py) |  |
| Local file tree reader | `gather-evidence:files` | [quest_ai_runner/the_world/tools/file_reading/files_adapter.py](../quest_ai_runner/the_world/tools/file_reading/files_adapter.py) |  |
| Remote file server reader | `gather-evidence:remote_files_adapter` | [quest_ai_runner/the_world/tools/file_reading/remote_files_adapter.py](../quest_ai_runner/the_world/tools/file_reading/remote_files_adapter.py) |  |
| Web research with the answer's support check (today) | `the-science:web_with_support_check` | [quest_ai_runner/the_world/tools/web_search/area.py](../quest_ai_runner/the_world/tools/web_search/area.py) | [science](science/README.md) |
| Live selection evaluation arm | `model-tier-chooser:qar_selection` | [evaluation/components/choose_a_model_tier/impls/qar_selection.py](../evaluation/components/choose_a_model_tier/impls/qar_selection.py) |  |
| Scripted ideal decisions, no model call (offline) | `claim-reach:scripted_ideal` | [evaluation/components/claim_reach/impls/scripted_ideal.py](../evaluation/components/claim_reach/impls/scripted_ideal.py) |  |
| Rules only, no model (floor) | `claim-reach:rules_only` | [evaluation/components/claim_reach/impls/rules_only.py](../evaluation/components/claim_reach/impls/rules_only.py) |  |
| Goal contract rules check | `run-deep-work:goal_contract` | [evaluation/components/deep_run/impls/goal_contract.py](../evaluation/components/deep_run/impls/goal_contract.py) |  |
| Subprocess runner scenarios | `run-deep-work:subprocess_runner` | [evaluation/components/deep_runner/impls/subprocess_runner.py](../evaluation/components/deep_runner/impls/subprocess_runner.py) |  |
| Agent-protocol runner scenarios | `run-deep-work:acp_runner` | [evaluation/components/deep_runner/impls/acp_runner.py](../evaluation/components/deep_runner/impls/acp_runner.py) |  |
| Evolvable-structure library decides (live) | `system-integrity:structure_library` | [evaluation/components/evolve_by_metric/impls/structure_library.py](../evaluation/components/evolve_by_metric/impls/structure_library.py) |  |
| Keep the selection as written (floor) | `system-integrity:keep_current` | [evaluation/components/evolve_by_metric/impls/keep_current.py](../evaluation/components/evolve_by_metric/impls/keep_current.py) |  |
| Creator keeps the goal, no model call (floor, proposed) | `hand-off-a-goal:creator_keeps_it` | [evaluation/components/hand_off_a_goal/impls/creator_keeps_it.py](../evaluation/components/hand_off_a_goal/impls/creator_keeps_it.py) |  |
| Registry read together with the code (today) | `system-integrity:registry_scan` | [evaluation/components/hub_of_hubs/impls/registry_scan.py](../evaluation/components/hub_of_hubs/impls/registry_scan.py) |  |
| Memory only, no research (floor) | `the-science:memory_only` | [evaluation/components/know_the_world/impls/memory_only.py](../evaluation/components/know_the_world/impls/memory_only.py) |  |
| Single flat provider floor | `vendor-model-caller:single_provider` | [evaluation/components/model_providers/impls/single_provider.py](../evaluation/components/model_providers/impls/single_provider.py) |  |
| No-judgement digest probe | `nervous-system:digest_probe` | [evaluation/components/overseer_signals/impls/digest_probe.py](../evaluation/components/overseer_signals/impls/digest_probe.py) |  |
| Live overseer model arm | `nervous-system:live_model` | [evaluation/components/overseer_signals/impls/live_model.py](../evaluation/components/overseer_signals/impls/live_model.py) |  |
| Direct tool-call ablation | `operate-a-quest:direct_tool_call` | [evaluation/components/quest_ops/impls/direct_tool_call.py](../evaluation/components/quest_ops/impls/direct_tool_call.py) |  |
| Fake-gateway conformance arm | `talk-over-a-channel:openclaw_bridge` | [evaluation/components/talk_over_a_channel/impls/openclaw_bridge.py](../evaluation/components/talk_over_a_channel/impls/openclaw_bridge.py) |  |
| Live usage evaluation arm | `track-usage-and-cost:qar_usage` | [evaluation/components/track_usage_and_cost/impls/qar_usage.py](../evaluation/components/track_usage_and_cost/impls/qar_usage.py) |  |
| Scratch-tree boundary check | `write-files:bounded_writer` | [evaluation/components/write_files/impls/bounded_writer.py](../evaluation/components/write_files/impls/bounded_writer.py) |  |
| Fake-client allowlist check | `write-through-mcp:allowlist_writer` | [evaluation/components/write_through_mcp/impls/allowlist_writer.py](../evaluation/components/write_through_mcp/impls/allowlist_writer.py) |  |

### Metrics

- Provider routing, fallback, retry and cost correctness (`metric-call-a-model`): `esoi run model_providers --impl multi_provider`
- Channel transport conformance to the gateway wire contract (`metric-talk-over-a-channel`): `esoi run talk_over_a_channel --impl openclaw_bridge`
- Overseer digest evidence and signal correctness on labelled scenarios (`metric-watch-a-run`): `esoi run overseer_signals --impl live_model; esoi run overseer_signals --impl digest_probe`
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): `esoi run track_usage_and_cost --impl qar_usage`
- Parts selected by measurement (`QME7`): `esoi run evolve_by_metric --impl structure_library`
- Supported and calibrated answers to world questions (`metric-know-the-world`): `esoi run know_the_world --impl web_with_support_check`
- Hub of hubs (`metric-hub-of-hubs`): `esoi run hub_of_hubs --impl registry_scan`
- Hub of hubs (`QME9`): `esoi run hub_of_hubs --impl registry_scan`
- Correct selection decisions over the evolve-by-metric cases (`metric-metric-earns-its-run`): `esoi run evolve_by_metric --impl structure_library`
- Intents whose solutions one paragraph and one diagram explain as the code builds them (`metric-solution-coherence`): `python3 -m esoi.structure.structure_check.coherence --gate`
- Task lifecycle correctness over offline cases (`metric-run-queued-tasks`): `esoi run run_queued_tasks --impl qar_runner`
- Requests held to their ESOI metric by code (`metric-think-critically`): `esoi run think_critically --impl deep_runs_only`
- Retrieval recall of the right document over handbook questions (`metric-gather-evidence`): `esoi run retrieval --impl composite; esoi run retrieval_sources --impl files_adapter`
- Final quest state against the request over the quest operation cases (`metric-operate-a-quest`): `esoi run quest_ops --impl qar_orchestrator`
- Outcome reporting correctness across deep runners (`metric-run-deep-work`): `esoi run deep_runner --impl subprocess_runner; esoi run deep_run --impl goal_contract`
- Write boundary and reversibility over the scratch-tree cases (`metric-write-files`): `esoi run write_files --impl bounded_writer`
- Allowlist enforcement and exactly-once execution (`metric-write-through-mcp`): `esoi run write_through_mcp --impl allowlist_writer`
- Combined world score (`metric-claim-reach`): `esoi run claim_reach --impl scripted_ideal`
- Hand-offs a person keeps per unit of cost and time (`metric-hand-off-a-goal`): `esoi run hand_off_a_goal --impl model_judge`
- Goal loop outcomes against the done-standard (`metric-goal-met-by-evidence-and-coverage`): `esoi run deep_run --impl goal_contract`
- Deep worker launch settings ranked by cost per goal met (`metric-harness-context-subagents-recursion-ranked`): `esoi run deep_runner --impl subprocess_runner`
- Account-state consistency across scripted sessions (`metric-act-as-the-body`): `esoi run act_as_the_body --impl card_and_reads`
- Parity of Quest operations across access points (`metric-quest-operations-parity`): `none`
- Step continuity at cost over multi-step requests (`metric-keep-a-working-stream`): `esoi run keep_a_working_stream --impl rebuilt_per_step --sample 3 --max-calls 40`
- Representative long-view carry-through across assigned quests, with nothing outside its branch (`metric-hold-the-long-view`): `esoi run hold_the_long_view --impl separate_stores`
- Ways in that pass their own metric (`metric-take-in-the-world`): `none`
- Context handed to the planner (`metric-choose-the-context`): `esoi run context_choice --impl per_lane_assembly`
- Goal-relevant admission at a precision floor with nothing out of scope (`metric-admit-to-the-mind`): `esoi run admit_to_the_mind --impl recency_window`
- Passing chat turns per dollar (`QME1`): `esoi run chat_quality --impl dev_quest_chat --sample 10 --max-calls 300`
- Delegated tasks meeting their standard (`QME2`): `none`
- Exact and consented changes (`QME3`): `none`
- Data kept in scope (`QME4`): `none`
- Engine reuse (`QME5`): `none`
- Measured coverage (`QME6`): `none`
- QAR overall score (`metric-qar-overall`): `esoi run qar_overall --impl recorded_rows`
- Justified share of model spend (`metric-justified-model-spend`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Justified model spend (`QME8`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): `esoi run choose_a_model_tier --impl qar_selection`
