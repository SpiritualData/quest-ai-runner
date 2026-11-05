# model_tier_chooser

*ESOI records kept beside the code in `quest_ai_runner/the_world/model_plumbing/model_tier_chooser`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py`](deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py`](model_registry.py) |  |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py`](usage_limit.py) |  |

## Metrics

- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): leader `qar_selection`, 42 of 42 cases

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Grounded AI that advances people's goals](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)
