# model_tier_chooser

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Model Plumbing](../README.md) > model_tier_chooser

Part of [Model Plumbing](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | Turns the planner's difficulty rating into a starting rung on the deep-worker ladder, normalising synonyms and unknown ratings and respecting an operator mapping, with no additional model call. | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py`](deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | Maps a tier name to the newest live model id by bucketing a provider's latest-first list by family, caching the result, using a last-known fallback only when the live list is empty, and exposing the next lower tier and vision capability. | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py`](model_registry.py) | selected |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | Detects a subscription usage-limit message in a run's result, parses its reset time and zone, computes a resume time, and records the limit lane-wide with optional persistence so other work waits. | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py`](usage_limit.py) |  |
| Call roles over the hub's tier table | `model-tier-chooser:call_roles` | Binds each Quest AI Runner model call (a prompt spec id or a direct call) to a role of the hub's role table, which gives the tier, and applies a person's or lane's explicit tier under three guards: classification never above fast, a response never below balanced, best only by a person's explicit tier or the structure-watch role. Deep work defaults to balanced. The legacy word science reads as its role's tier. | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/call_roles.py`](call_roles.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Model Plumbing](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
