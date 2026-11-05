# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Evolvable-structure library decides (live) | `system-integrity:structure_library` | Lets the esoi library's own leaderboard decide: a rival on the same cases replaces the selected part only on a statistically significant gain, tested paired per case when per-case outcomes exist and by Fisher's exact test on the pass counts otherwise, and the repository's committed selection is read the way production reads it. | [`evaluation/components/evolve_by_metric/impls/structure_library.py`](structure_library.py) |  |
| Keep the selection as written (floor) | `system-integrity:keep_current` | Never switches to a rival and never reads the committed selection, so the selection stays whatever someone last wrote, however the metric reads. | [`evaluation/components/evolve_by_metric/impls/keep_current.py`](keep_current.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
