# core

*ESOI records kept beside the code in `quest_ai_runner/core`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Subconscious (`remember-and-recall`)

Keeps each request's working stream holding the part of the person's own knowledge its goal needs, so a fast model is neither misled by near misses nor buried in noise.

- Entry call: `quest_ai_runner.run("remember-and-recall", 'rust release')`
- Selected implementation: Start-of-run card filter and a recency window (today) (`recency_window`)
- Metric: Goal-relevant admission at a precision floor with nothing out of scope
- Implemented by: [`quest_ai_runner/core/auxiliary_prompts.py`](auxiliary_prompts.py), [`quest_ai_runner/core/context_doctrine.py`](context_doctrine.py), [`quest_ai_runner/core/planner_cascade.py`](planner_cascade.py), [`quest_ai_runner/core/planner_context.py`](planner_context.py), [`quest_ai_runner/core/planner_doctrine.py`](planner_doctrine.py), [`quest_ai_runner/core/planner_prompt.py`](planner_prompt.py), [`quest_ai_runner/core/prompt_layers.py`](prompt_layers.py), [`quest_ai_runner/core/prompt_specs.py`](prompt_specs.py) and 4 more

## Justified model calls (`justify-model-calls`)

Gives an operator a known value, cost and time for every model call, so spend and latency go only to prompts that earn them and a cheaper or faster option is not missed.

- Entry call: no entry declared
- Selected implementation: Registered prompt specs with an enforcement test (today) (`spec_registry`)
- Metric: Justified share of model spend
- Implemented by: [`quest_ai_runner/core/auxiliary_prompts.py`](auxiliary_prompts.py), [`quest_ai_runner/core/context_doctrine.py`](context_doctrine.py), [`quest_ai_runner/core/planner_cascade.py`](planner_cascade.py), [`quest_ai_runner/core/planner_context.py`](planner_context.py), [`quest_ai_runner/core/planner_doctrine.py`](planner_doctrine.py), [`quest_ai_runner/core/planner_prompt.py`](planner_prompt.py), [`quest_ai_runner/core/prompt_layers.py`](prompt_layers.py), [`quest_ai_runner/core/prompt_specs.py`](prompt_specs.py) and 7 more

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Rebuilt prompt per step with the context re-sent from cache | `reason-and-answer:rebuilt_full_context` | [`quest_ai_runner/core/orchestrator_planning.py`](orchestrator_planning.py) |  |
| Start-of-run card filter and a recency window (today) | `remember-and-recall:recency_window` | [`quest_ai_runner/core/planner_context.py`](planner_context.py) | selected |
| No overseer (configuration off) | `watch-for-risk:no_overseer` | [`quest_ai_runner/core/orchestrator_types.py`](orchestrator_types.py) |  |
| Standing instruction alone (floor) | `think-critically:instruction_only` | [`quest_ai_runner/core/standing_instructions.py`](standing_instructions.py) |  |
| Registered prompt specs with an enforcement test (today) | `justify-model-calls:spec_registry` | [`quest_ai_runner/core/prompt_specs.py`](prompt_specs.py) | selected |

## Metrics

- Goal-relevant admission at a precision floor with nothing out of scope (`metric-admit-to-the-mind`): leader `recency_window`, 4 of 9 cases
- Justified share of model spend (`metric-justified-model-spend`): leader `spec_registry`, 15 of 15 cases
- Justified model spend (`QME8`): leader `spec_registry`, 15 of 15 cases

## Problems

- What of the person's own material enters a step's view is decided by position and size, not by what the request needs: cards are chosen once from the request text at the start, the newest few... (`prob-admission-by-recency-not-relevance`)

## Ideals

- Goal-Relevant Attention (`ideal-goal-relevant-attention`): Only what bears on the request's goal enters the working stream, and nothing from outside the request's scope ever does.

## Where to go next

- Up: [quest_ai_runner](../README.md)
- Intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
