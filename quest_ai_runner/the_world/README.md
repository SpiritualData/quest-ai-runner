# World

**Vision statement:** What lies beyond a person's Quest reaches the AI through one measured path.

Breadcrumb: [Top](../../esoi/README.md) > World

This sub-intent arises as the implementation `the-world:composition` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: Models, search backends and tools change faster than the assistant does, so reaching them through one measured path lets the best one run as they move.

## How the solutions fit together

World is how the assistant learns what lies outside it and acts on it. Model Plumbing asks a model through one measured path. Science answers from public research with its source shown. Tools lets the assistant use the tool of each category the person's authority allows.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- [Model Plumbing](model_plumbing/README.md) (sub-intent): Each question to a model goes to the tier that earns its cost.
- [Science](science/README.md) (sub-intent): What a request needs from beyond the person's own material comes from the world, with its source.
- [Tools](tools/README.md) (sub-intent): The AI acts only through tools the person's authority allows.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Model Plumbing | `model-plumbing` | no entry declared | Via vendor (`via_vendor`) | Cost, budget and resource-guard correctness, Justified share of model spend, Justified model spend |
| Science | `the-science` | `quest_ai_runner.run("the-science", 'meditation and attention')` | Web research with the answer's support check (today) (`web_with_support_check`) | Supported and calibrated answers to world questions |
| Tools | `the-tools` | `quest_ai_runner.run("the-tools", 'Send the launch note to the whole team')` | Coding agent, reach described in the prompt (today) (`coding_agent_prompt_reach`) | Combined world score |

## Solutions from a library

- [Model Plumbing](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/README.md) (`model-plumbing`): The one measured path every part uses to ask a model: the Model Tier Chooser picks the model for a tier and the Vendor Model Caller reaches the vendor and steps to the next provider when one fails. Adopted from the library `hub`.
- [Science](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/README.md) (`the-science`): Looks up what public research says and returns observations a reader can cite. Adopted from the library `hub`.
- [Tools](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/README.md) (`the-tools`): What the AI can use or do outside the system, grouped by category. Adopted from the library `hub`.

## Where to go next

- Sub-intent: [Model Plumbing](model_plumbing/README.md)
- Sub-intent: [Science](science/README.md)
- Sub-intent: [Tools](tools/README.md)
- Parent intent: [Quest AI Runner](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Model Plumbing | `model-plumbing` | Via vendor (`via_vendor`) | Cost, budget and resource-guard correctness, Justified share of model spend, Justified model spend |
| Science | `the-science` | Web research with the answer's support check (today) (`web_with_support_check`) | Supported and calibrated answers to world questions |
| Tools | `the-tools` | Coding agent, reach described in the prompt (today) (`coding_agent_prompt_reach`) | Combined world score |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Host research sources first, then the web (proposed) | `the-science:research_sources_first` |  |  |
| Findings weighed by a host's evidence-estimation service (proposed) | `the-science:evidence_weighted` |  |  |
| Registry plus per-call ablation against the owner metric (proposed) | `model-plumbing:spec_registry_ablation` |  |  |
| Hysteresis resource guard | `model-plumbing:resources` | [quest_ai_runner/the_body/host/resources.py](../the_body/host/resources.py) |  |
| Coding agent with the host reach summary and reach judge (built, off by default) | `the-tools:coding_agent_reach_judge` | [quest_ai_runner/the_intelligence/mind/planner/reach_judge.py](../the_intelligence/mind/planner/reach_judge.py) | [tools](tools/README.md) |
| Coding agent, reach described in the prompt (today) | `the-tools:coding_agent_prompt_reach` | [quest_ai_runner/the_body/area_body.py](../the_body/area_body.py) |  |
| Registered prompt specs with an enforcement test (today) | `model-plumbing:spec_registry` | [quest_ai_runner/the_self/integrity/prompts/prompt_specs.py](../the_self/integrity/prompts/prompt_specs.py) |  |
| Via vendor | `model-plumbing:via_vendor` |  | [model_plumbing](model_plumbing/README.md) |
| Static price table lookup | `model-plumbing:pricing` | [quest_ai_runner/the_world/model_plumbing/spend/pricing.py](model_plumbing/spend/pricing.py) |  |
| Persisted daily token tracker | `model-plumbing:usage` | [quest_ai_runner/the_world/model_plumbing/spend/usage.py](model_plumbing/spend/usage.py) |  |
| Snippet-first research with page extraction | `the-science:web_research` | [quest_ai_runner/the_world/tools/web_search/web_research.py](tools/web_search/web_research.py) |  |
| Web research with the answer's support check (today) | `the-science:web_with_support_check` | [quest_ai_runner/the_world/tools/web_search/area.py](tools/web_search/area.py) | [science](science/README.md) |


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
