# Quest Self

Each Quest user meets one whole assistant.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `quest-self:library` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A person's day with their assistant is judged as they meet it, which no single part can show, so the whole assistant has its own measure of how capable it feels from the user's seat.

## How the solutions fit together

Quest Self keeps the parts of the assistant working as one whole for each Quest user. To answer a request it takes the request in through Quest Requests, learns what the person is working toward through Quest Intents, chooses the context the request needs through Context Retriever, reasons through Mind, acts on the account through Quest Body and answers beyond the person's own material through Science. Nervous System watches the run before anything irreversible is handed on and Critical Thinking holds the work to its measure. Model Plumbing is how every part asks a model, so each call is justified and its cost is known.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Model Plumbing | `model-plumbing` | no entry declared | Via vendor (`via_vendor`) | Cost, budget and resource-guard correctness, Justified share of model spend, Justified model spend |

## Solutions from a library

- [Model Plumbing](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/README.md) (`model-plumbing`): The one measured path every part uses to ask a model: the Model Tier Chooser picks the model for a tier and the Vendor Model Caller reaches the vendor and steps to the next provider when one fails. Adopted from the library `hub`.

## Metrics and their leaders

- Passing chat turns per dollar (`QME1`): leader `reference_replies`, 331 of 331 cases
- Delegated tasks meeting their standard (`QME2`)
- Exact and consented changes (`QME3`)
- Data kept in scope (`QME4`)
- Engine reuse (`QME5`)
- Measured coverage (`QME6`)
- QAR overall score (`metric-qar-overall`): leader `qar_overall.broken.latest_row_any_age:build`, 0 of 9 cases
- Justified share of model spend (`metric-justified-model-spend`): leader `spec_registry`, 15 of 15 cases
- Justified model spend (`QME8`): leader `spec_registry`, 15 of 15 cases
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): leader `qar_usage`, 38 of 39 cases

## Where to go next

- Sub-intent: [Integrity](integrity/README.md) (Every part of QAR is a specified solution whose best implementation runs as the state of the art moves.)
- Parent intent: [Quest AI Runner](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Model Plumbing | `model-plumbing` | Via vendor (`via_vendor`) | Cost, budget and resource-guard correctness, Justified share of model spend, Justified model spend |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Standing instruction alone (floor) | `critical-thinking:instruction_only` | [quest_ai_runner/the_self/integrity/prompts/standing_instructions.py](integrity/prompts/standing_instructions.py) |  |
| Registered prompt specs with an enforcement test (today) | `model-plumbing:spec_registry` | [quest_ai_runner/the_self/integrity/prompts/prompt_specs.py](integrity/prompts/prompt_specs.py) |  |
| Via vendor | `model-plumbing:via_vendor` |  |  |
| Apply the evolvable-structure library | `system-integrity:structure-library` | [quest_ai_runner/the_self/area_integrity.py](area_integrity.py) | [integrity](integrity/README.md) |

### Metrics

- Passing chat turns per dollar (`QME1`): `esoi run chat_quality --impl dev_quest_chat --sample 10 --max-calls 300`
- Delegated tasks meeting their standard (`QME2`): `none`
- Exact and consented changes (`QME3`): `none`
- Data kept in scope (`QME4`): `none`
- Engine reuse (`QME5`): `none`
- Measured coverage (`QME6`): `none`
- QAR overall score (`metric-qar-overall`): `esoi run qar_overall --impl recorded_rows`
- Justified share of model spend (`metric-justified-model-spend`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Justified model spend (`QME8`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): `esoi run track_usage_and_cost --impl qar_usage`
