# Model Plumbing

**Vision statement:** Each question to a model goes to the tier that earns its cost.

Breadcrumb: [Top](../../../esoi/README.md) > [World](../README.md) > Model Plumbing

This sub-intent arises as the implementation `model-plumbing:via_vendor` of its parent intent [`the_world`](../README.md). Opportunities: Models change faster than the assistant does, so choosing the tier by measurement and keeping the vendor behind one caller lets a better model run as it appears.

## How the solutions fit together

Model Plumbing asks a model through one path whose model can change unseen. Model Tier Chooser picks the model that earns its cost for each tier. Vendor Model Caller asks the chosen model of its vendor.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- Model Tier Chooser (solution with no page of its own, its implementations are in this intent's records)
- Vendor Model Caller (solution with no page of its own, its implementations are in this intent's records)

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Model Tier Chooser | `model-tier-chooser` | `quest_ai_runner.run("model-tier-chooser", {'available': ['anthropic']})` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |
| Vendor Model Caller | `vendor-model-caller` | `quest_ai_runner.run("vendor-model-caller", ...)` | Prefix-routing multi-provider (`multi_provider`) | Provider routing, fallback, retry and cost correctness |

## Solutions from a library

- [Model Tier Chooser](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/model_plumbing/README.md) (`model-tier-chooser`): Picks, for each of the four model tiers (fast, balanced, quality, best), the provider, model and reasoning effort that is the cheapest and fastest of the arms at least as good as the tier's held default on public evaluations (capability only for best); the hub's own measurements inform which tier a call role uses, not which model a tier maps to. Adopted from the library `hub`.
- [Vendor Model Caller](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_world/model_plumbing/README.md) (`vendor-model-caller`): Plans, answers and lists models for one vendor, or for several through a router that steps to the next model when a provider fails. Adopted from the library `hub`.

## Metrics and their leaders

- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): leader `qar_selection`, 42 of 42 cases

## Where to go next

- Parent intent: [World](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_world/model_plumbing/model_tier_chooser](model_tier_chooser/README.md), [quest_ai_runner/the_world/model_plumbing/vendor_model_caller](vendor_model_caller/README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [metrics.csv](metrics.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Model Tier Chooser | `model-tier-chooser` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |
| Vendor Model Caller | `vendor-model-caller` | Prefix-routing multi-provider (`multi_provider`) | Provider routing, fallback, retry and cost correctness |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/deep_model_selection.py](model_tier_chooser/deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/model_registry.py](model_tier_chooser/model_registry.py) |  |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/usage_limit.py](model_tier_chooser/usage_limit.py) |  |
| Call roles over the hub's tier table | `model-tier-chooser:call_roles` | [quest_ai_runner/the_world/model_plumbing/model_tier_chooser/call_roles.py](model_tier_chooser/call_roles.py) |  |
| Prefix-routing multi-provider | `vendor-model-caller:multi_provider` | [quest_ai_runner/the_world/model_plumbing/vendor_model_caller/providers.py](vendor_model_caller/providers.py) |  |
| Keyless command-line provider | `vendor-model-caller:claude_cli` | [quest_ai_runner/the_world/model_plumbing/vendor_model_caller/claude_cli_provider.py](vendor_model_caller/claude_cli_provider.py) |  |

### Metrics

- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): `esoi run choose_a_model_tier --impl qar_selection`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
