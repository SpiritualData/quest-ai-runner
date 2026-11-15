# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Model Plumbing

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Registered prompt specs with an enforcement test (today) (`spec_registry`) | 100% (15 of 15 cases) | free |  |
| 2 | Registry plus per-call ablation against the owner metric (proposed) (`spec_registry_ablation`) | 0% (0 of 15 cases) | n/a |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Justified share of model spend. share of AVERAGE model spend on prompts that are registered, whose owner metric has a current row on production-shaped cases, and whose call beats its ablation on average while its MAXIMUM cost, tokens and latency stay within that prompt's own budget (bar 0.8).
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Live usage evaluation arm | `model-plumbing:qar_usage` | Drives the live price, estimate, tracker and guard code, with a real usage file in a scratch folder and scripted host samples as the only fake. Backed at run time by the arms pricing, usage and resources; this arm measures them and stays selected as the measured one. | [`evaluation/components/track_usage_and_cost/impls/qar_usage.py`](qar_usage.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
