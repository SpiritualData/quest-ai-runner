# the_world

*ESOI records kept beside the code in `quest_ai_runner/the_world`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Prefix-routing multi-provider | `vendor-model-caller:multi_provider` | [`quest_ai_runner/the_world/model_plumbing/vendor_model_caller/providers.py`](model_plumbing/vendor_model_caller/providers.py) | selected |
| Static price table lookup | `track-usage-and-cost:pricing` | [`quest_ai_runner/the_world/model_plumbing/spend/pricing.py`](model_plumbing/spend/pricing.py) |  |
| Persisted daily token tracker | `track-usage-and-cost:usage` | [`quest_ai_runner/the_world/model_plumbing/spend/usage.py`](model_plumbing/spend/usage.py) |  |
| Web research with the answer's support check (today) | `the-science:web_with_support_check` | [`quest_ai_runner/the_world/tools/web_search/area.py`](tools/web_search/area.py) | selected |
| Keyless command-line provider | `vendor-model-caller:claude_cli` | [`quest_ai_runner/the_world/model_plumbing/vendor_model_caller/claude_cli_provider.py`](model_plumbing/vendor_model_caller/claude_cli_provider.py) |  |
| MCP gateway bridge transport | `talk-over-a-channel:openclaw` | [`quest_ai_runner/the_world/tools/connected_system_operations/openclaw_channel.py`](tools/connected_system_operations/openclaw_channel.py) | selected |
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py`](model_plumbing/model_tier_chooser/deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py`](model_plumbing/model_tier_chooser/model_registry.py) | selected |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [`quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py`](model_plumbing/model_tier_chooser/usage_limit.py) |  |

## Where to go next

- Up: [quest_ai_runner](../README.md)
- Intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Down: [quest_ai_runner/the_world/model_plumbing/model_tier_chooser](model_plumbing/model_tier_chooser/README.md)
- Repository: [top README](../../README.md)
