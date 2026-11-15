# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of Integrity

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Evolvable-structure library decides (live) (`structure_library`) | 76% (13 of 17 cases) | free |  |
| 2 | Keep the selection as written (floor) (`keep_current`) | 59% (10 of 17 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Parts selected by measurement. share of solutions whose selected implementation has a recorded result on the committed code and is not beaten by a rival on the same cases
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Evolvable-structure library decides (live) | `system-integrity:structure_library` | Lets the esoi library's own leaderboard decide: a rival on the same cases replaces the selected part only on a statistically significant gain, tested paired per case when per-case outcomes exist and by Fisher's exact test on the pass counts otherwise, and the repository's committed selection is read the way production reads it. | [`evaluation/components/evolve_by_metric/impls/structure_library.py`](structure_library.py) |  |
| Keep the selection as written (floor) | `system-integrity:keep_current` | Never switches to a rival and never reads the committed selection, so the selection stays whatever someone last wrote, however the metric reads. | [`evaluation/components/evolve_by_metric/impls/keep_current.py`](keep_current.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
