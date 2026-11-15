# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

### Top implementations of File Editing

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Scratch-tree boundary check (`bounded_writer`) | 100% (20 of 20 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Write boundary and reversibility over the scratch-tree cases. offline cases on a scratch tree: ordinary create and overwrite, traversal, absolute and sibling-prefix paths, symlink escape, protected names, size and create limits, reversible overwrite (outside untouched, backup kept) The binary-extension and secret-name lists are fixed in code, and no case covers concurrent writers.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Scratch-tree boundary check | `file-editing:bounded_writer` | Drives the production writer on temporary trees with an outside file as a sentinel, and observes whether the write succeeded, whether the outside stayed intact, the final content and whether a backup was kept. | [`evaluation/components/write_files/impls/bounded_writer.py`](bounded_writer.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
