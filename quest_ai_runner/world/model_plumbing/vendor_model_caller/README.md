# vendor_model_caller

Breadcrumb: [Top](../../../../esoi/README.md) > [World](../../README.md) > [Model Plumbing](../README.md) > vendor_model_caller

Part of [Model Plumbing](../README.md)

### Top implementations of Vendor Model Caller

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Prefix-routing multi-provider (`multi_provider`) | 100% (56 of 56 cases) | free | selected |
| 2 | Single flat provider floor (`single_provider`) | 29% (16 of 56 cases) | free |  |

**How it is scored:** Provider routing, fallback, retry and cost correctness. routing, tier fallback, retry, cost and pricing arithmetic on offline cases (fake and dry-run providers) Read in this direction: minimise cost per passing task by using the cheapest tier that passes; maximise availability through retry and tier fallback, and keep vendors swappable by configuration alone.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Prefix-routing multi-provider | `vendor-model-caller:multi_provider` | Wraps a primary provider and a set of vendor providers, routes each call by model-name prefix, steps down a tier when a quota error occurs, and applies shared retry for transient errors. Callers see one provider. | [`quest_ai_runner/world/model_plumbing/vendor_model_caller/providers.py`](providers.py) | selected |
| Keyless command-line provider | `vendor-model-caller:claude_cli` | Drives the local Claude command-line tool in print mode so planning and answering run on the operator's subscription login. Planning asks for only the decision JSON and parses leniently, and the spawned process has API credentials stripped from its environment. | [`quest_ai_runner/world/model_plumbing/vendor_model_caller/claude_cli_provider.py`](claude_cli_provider.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Model Plumbing](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
