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
| Registry read together with the code (today) | `system-integrity:registry_scan` | Reads the hub registry and the code: declared implementations, solution records, a static scan of core for vendor branches, and a fake hub registered through each area configuration entry. | [`evaluation/components/hub_of_hubs/impls/registry_scan.py`](registry_scan.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
