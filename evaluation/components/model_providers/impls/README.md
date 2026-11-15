# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

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
| Single flat provider floor | `vendor-model-caller:single_provider` | A baseline in which every model name goes to one provider, a quota error is final, transient errors are not retried, the dry run counts input only and every model has the same price. It is the floor the multi-provider stack must beat. | [`evaluation/components/model_providers/impls/single_provider.py`](single_provider.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
