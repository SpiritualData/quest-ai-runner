# Model Tier Chooser

*ESOI records kept beside the code in `quest_ai_runner/the_world/model_plumbing/model_tier_chooser`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

The AI system optimally learns and interacts with the world outside of it.

These are the solutions of [World](../../README.md).

## How the solutions fit together

World is how the assistant learns what is outside it and acts on it. Model Plumbing asks a model through one measured path, where Model Tier Chooser picks the model for each tier and Vendor Model Caller asks that vendor. Tools lets the assistant use the tool of each category the person's authority allows, through Web Search, File Editing and Delegated Agent Sessions. Science answers from public research with its sources.

## Model Tier Chooser (`model-tier-chooser`)

- Entry call: `quest_ai_runner.run("model-tier-chooser", {'available': ['anthropic']})`
- Selected implementation: Live tier registry (`model_registry`)
- Metric: Model tier selection and limit-recognition correctness
- Implemented by: [`evaluation/components/choose_a_model_tier/impls/qar_selection.py`](../../../../evaluation/components/choose_a_model_tier/impls/qar_selection.py), [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/__init__.py`](__init__.py), [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py`](deep_model_selection.py), [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/interface.py`](interface.py), [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py`](model_registry.py), [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py`](usage_limit.py)

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py`](deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py`](model_registry.py) | selected |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py`](usage_limit.py) |  |

## Metrics

- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): leader `qar_selection`, 42 of 42 cases

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [World](../../README.md)
- Repository: [top README](../../../../README.md)
