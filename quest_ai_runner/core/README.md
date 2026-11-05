# core

*ESOI records kept beside the code in `quest_ai_runner/core`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote, and every answer and action rests on the person's own goals, material and preferences, and never on another person's.

These are the solutions of [Grounded AI that advances people's goals](../../esoi/README.md).

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
    s6["Grounded goal agent<br/><i>do-grounded-work</i>"]
    s7["Queued-task runner<br/><i>run-queued-tasks</i>"]
    s8["Mind<br/><i>the-mind</i>"]
    s9["Body<br/><i>the-body</i>"]
    s10["Higher mind<br/><i>hold-the-intents</i>"]
    s11["Critical Thinking<br/><i>critical-thinking</i>"]
    s12["Quest account access<br/><i>access-quest-operations</i>"]
    s13["Request intake<br/><i>take-in-the-world</i>"]
    s14["Context choice<br/><i>choose-the-context</i>"]
    s15["Subconscious<br/><i>the-subconscious</i>"]
    s16["Justified model calls<br/><i>justify-model-calls</i>"]
    s17["Cost-ordered model tier selector with outage and allowance recovery<br/><i>model-tier-chooser</i>"]
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s2
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s6
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s7
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s8
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s10
    s1 -->|"An inbound batch (messages with ids, chat references, text and attachments, a healthy flag and any error) and a send result with success and message id"| s13
    s2 -->|"For a flagged case: the risk category, the response applied and its signal (proceed, redirect with a hint, answer now, escalate to deep execution, escalate to a person), with a reason and a degraded flag"| s6
    s3 -->|"A price per million input tokens, a bootstrap cost estimate with the provider and model used, a running total with an over-limit flag and status line, and a paused or not verdict"| s0
    s4 -->|"Checked records, recorded metric results and the selected implementation for each solution"| s6
    s5 -->|"Findings, each with its source, an evidence-strength label and the passage that supports it"| s6
    s7 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s9
    s7 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s11
    s8 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s6
    s8 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s9
    s9 -->|"A current state view for the request's scope with the time each part was read, and after a change the read-back objects"| s6
    s10 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s6
    s10 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s15
    s11 -->|"Per request: the metric applied with its reading before and after, or the reason none applies, or a proposed metric; and for a regression or a missing metric, the response taken"| s6
    s12 -->|"The result of one Quest operation"| s6
    s12 -->|"The result of one Quest operation"| s7
    s12 -->|"The result of one Quest operation"| s9
    s13 -->|"One routed turn with its context sources"| s6
    s14 -->|"The assembled context, the ids of the cards it drew on, a resolved reference, a ranked list of cards and records, or the cards nearest a query by meaning"| s6
    s15 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s6
    s15 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s8
    s16 -->|"A per-prompt table and the share of average model spend that is justified"| s7
    s17 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s2
    s17 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s8
```

## Subconscious (`the-subconscious`)

Keeps each request's working stream holding the part of the person's own knowledge its goal needs, so a fast model is neither misled by near misses nor buried in noise.

- Entry call: `quest_ai_runner.run("the-subconscious", 'rust release')`
- Selected implementation: Start-of-run card filter and a recency window (today) (`recency_window`)
- Metric: Goal-relevant admission at a precision floor with nothing out of scope
- Implemented by: [`quest_ai_runner/core/auxiliary_prompts.py`](auxiliary_prompts.py), [`quest_ai_runner/core/context_doctrine.py`](context_doctrine.py), [`quest_ai_runner/core/planner_cascade.py`](planner_cascade.py), [`quest_ai_runner/core/planner_context.py`](planner_context.py), [`quest_ai_runner/core/planner_doctrine.py`](planner_doctrine.py), [`quest_ai_runner/core/planner_prompt.py`](planner_prompt.py), [`quest_ai_runner/core/prompt_layers.py`](prompt_layers.py), [`quest_ai_runner/core/prompt_specs.py`](prompt_specs.py) and 4 more

## Justified model calls (`justify-model-calls`)

Gives an operator a known value, cost and time for every model call, so spend and latency go only to prompts that earn them and a cheaper or faster option is not missed.

- Entry call: no entry declared
- Selected implementation: Registered prompt specs with an enforcement test (today) (`spec_registry`)
- Metric: Justified share of model spend
- Implemented by: [`quest_ai_runner/core/auxiliary_prompts.py`](auxiliary_prompts.py), [`quest_ai_runner/core/context_doctrine.py`](context_doctrine.py), [`quest_ai_runner/core/planner_cascade.py`](planner_cascade.py), [`quest_ai_runner/core/planner_context.py`](planner_context.py), [`quest_ai_runner/core/planner_doctrine.py`](planner_doctrine.py), [`quest_ai_runner/core/planner_prompt.py`](planner_prompt.py), [`quest_ai_runner/core/prompt_layers.py`](prompt_layers.py), [`quest_ai_runner/core/prompt_specs.py`](prompt_specs.py) and 7 more

## Cost-ordered model tier selector with outage and allowance recovery (`model-tier-chooser`)

Choose the model rung that starts each piece of work, and resume work after a provider outage or usage limit rather than failing it.

- Entry call: `quest_ai_runner.run("model-tier-chooser", {'available': ['anthropic']})`
- Selected implementation: Live tier registry (`model_registry`)
- Metric: Model tier selection and limit-recognition correctness
- Implemented by: [`evaluation/components/choose_a_model_tier/impls/qar_selection.py`](../../evaluation/components/choose_a_model_tier/impls/qar_selection.py), [`quest_ai_runner/core/deep_model_selection.py`](deep_model_selection.py), [`quest_ai_runner/core/model_registry.py`](model_registry.py), [`quest_ai_runner/core/usage_limit.py`](usage_limit.py)

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Rebuilt prompt per step with the context re-sent from cache | `the-mind:rebuilt_full_context` | [`quest_ai_runner/core/orchestrator_planning.py`](orchestrator_planning.py) |  |
| Start-of-run card filter and a recency window (today) | `the-subconscious:recency_window` | [`quest_ai_runner/core/planner_context.py`](planner_context.py) | selected |
| No overseer (configuration off) | `nervous-system:no_overseer` | [`quest_ai_runner/core/orchestrator_types.py`](orchestrator_types.py) |  |
| Standing instruction alone (floor) | `critical-thinking:instruction_only` | [`quest_ai_runner/core/standing_instructions.py`](standing_instructions.py) |  |
| Registered prompt specs with an enforcement test (today) | `justify-model-calls:spec_registry` | [`quest_ai_runner/core/prompt_specs.py`](prompt_specs.py) | selected |
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [`quest_ai_runner/core/deep_model_selection.py`](deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [`quest_ai_runner/core/model_registry.py`](model_registry.py) | selected |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [`quest_ai_runner/core/usage_limit.py`](usage_limit.py) |  |

## Metrics

- Goal-relevant admission at a precision floor with nothing out of scope (`metric-admit-to-the-mind`): leader `recency_window`, 4 of 9 cases
- Justified share of model spend (`metric-justified-model-spend`): leader `spec_registry`, 15 of 15 cases
- Justified model spend (`QME8`): leader `spec_registry`, 15 of 15 cases
- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): leader `qar_selection`, 42 of 42 cases

## Problems

- What of the person's own material enters a step's view is decided by position and size, not by what the request needs: cards are chosen once from the request text at the start, the newest few observations are shown in full and older ones collapsed, and a lookup's result is admitted whether or not it is what was asked for. (`prob-admission-by-recency-not-relevance`)

## Ideals

- Goal-Relevant Attention (`ideal-goal-relevant-attention`): Only what bears on the request's goal enters the working stream, and nothing from outside the request's scope ever does.

## Where to go next

- Up: [quest_ai_runner](../README.md)
- Intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
