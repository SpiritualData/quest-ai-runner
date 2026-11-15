# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Model Tier Chooser

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Live selection evaluation arm (`qar_selection`) | 100% (42 of 42 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Model tier selection and limit-recognition correctness. offline cases: deep starting rung per difficulty and ladder (synonyms, unknown rating, off-ladder start, operator mapping), family resolution to the newest live id, tier resolution with and without live models and pins, lower-tier fallback, vision capability, outage wording, usage-limit detection and reset parsing; zero model calls Read in this direction: maximise the share of work started on the cheapest adequate rung; maximise correct resolution of names to live models with operator pins always winning; maximise correct recognition of outages and allowance limits and of their reset time.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Live selection evaluation arm | `model-tier-chooser:qar_selection` | Calls the live selection, family, registry, fallback, outage and limit functions with a provider fake that only lists models. It is the measured arm for the rules above. | [`evaluation/components/choose_a_model_tier/impls/qar_selection.py`](qar_selection.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
