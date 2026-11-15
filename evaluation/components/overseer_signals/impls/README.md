# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Nervous System

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | No-judgement digest probe (`digest_probe`) | 100% (16 of 16 cases) | free |  |
| 2 | Live overseer model arm (`live_model`) | 88% (14 of 16 cases) | $0.0010 |  |
| 3 | No overseer (configuration off) (`no_overseer`) | 38% (6 of 16 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Overseer digest evidence and signal correctness on labelled scenarios. digest evidence on hand-labelled scenarios (contrast pairs), offline; signal match in a live arm; signal parsing and degrade-to-proceed in judges The signal match itself needs a model and is not seen offline.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| No-judgement digest probe | `nervous-system:digest_probe` | A floor arm that always returns no signal, so the scorer measures only whether the digest shows the evidence each label turns on. | [`evaluation/components/overseer_signals/impls/digest_probe.py`](digest_probe.py) |  |
| Live overseer model arm | `nervous-system:live_model` | Calls the production overseer function against the real provider, one small call per scenario, and fails if the overseer degraded. | [`evaluation/components/overseer_signals/impls/live_model.py`](live_model.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
