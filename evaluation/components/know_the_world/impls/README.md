# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Science

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Web research with the answer's support check (today) (`web_with_support_check`) | 100% (9 of 9 cases) | free | selected |
| 2 | Memory only, no research (floor) (`memory_only`) | 0% (0 of 9 cases) | n/a |  |

**How it is scored:** Supported and calibrated answers to world questions. Measures whether claims about the world are supported by a cited source and stated with a confidence that matches the evidence.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Memory only, no research (floor) | `the-science:memory_only` | States the claim the case labels from what the model remembers, with no source and no retrieved page, whether or not any page supports it. | [`evaluation/components/know_the_world/impls/memory_only.py`](memory_only.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
