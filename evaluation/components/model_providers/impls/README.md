# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Single flat provider floor | `vendor-model-caller:single_provider` | A baseline in which every model name goes to one provider, a quota error is final, transient errors are not retried, the dry run counts input only and every model has the same price. It is the floor the multi-provider stack must beat. | [`evaluation/components/model_providers/impls/single_provider.py`](single_provider.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
