# vendor_model_caller

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Model Plumbing](../README.md) > vendor_model_caller

Part of [Model Plumbing](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Prefix-routing multi-provider | `vendor-model-caller:multi_provider` | Wraps a primary provider and a set of vendor providers, routes each call by model-name prefix, steps down a tier when a quota error occurs, and applies shared retry for transient errors. Callers see one provider. | [`quest_ai_runner/the_world/model_plumbing/vendor_model_caller/providers.py`](providers.py) | selected |
| Keyless command-line provider | `vendor-model-caller:claude_cli` | Drives the local Claude command-line tool in print mode so planning and answering run on the operator's subscription login. Planning asks for only the decision JSON and parses leniently, and the spawned process has API credentials stripped from its environment. | [`quest_ai_runner/the_world/model_plumbing/vendor_model_caller/claude_cli_provider.py`](claude_cli_provider.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Model Plumbing](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
