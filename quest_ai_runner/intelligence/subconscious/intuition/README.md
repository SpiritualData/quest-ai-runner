# intuition

Breadcrumb: [Top](../../../../esoi/README.md) > [Intelligence](../../README.md) > [Subconscious](../README.md) > intuition

Part of [Subconscious](../README.md)

### Top implementations of Intuition

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | `file_store` | 100% (12 of 12 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Prediction store contract pass rate. share of persistence contract scenarios (roundtrip, isolation, replace, expiry, view, clear, log, restart, tolerance) the store passes; a share held out Read in this direction: maximise exact round trip of saved data; maximise scope isolation; maximise correct expiry; maximise survival across a restart; ensure no store call raises.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Atomic file prediction store | `intuition:file` | Keeps one JSON file per scope, written atomically, plus an append-only outcome log, drops expired predictions on load and swallows storage errors so a failure never reaches a turn. | [`quest_ai_runner/intelligence/subconscious/intuition/anticipation_store.py`](anticipation_store.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Subconscious](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
