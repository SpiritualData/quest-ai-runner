# Feedback Learning

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > Feedback Learning

Part of [Quest Consciousness](../README.md)

## Feedback Learning (`feedback-learning`)

- Entry call: `quest_ai_runner.run("feedback-learning", 'quest-id')`
- Selected implementation: File-backed feedback ledger (`ledger`)
- Metric: Correction carry-through over the feedback-loop cases
- Implemented by: [`quest_ai_runner/body/internal_context/card_store/__init__.py`](../../body/internal_context/card_store/__init__.py), [`quest_ai_runner/body/internal_context/card_store/card_cache_file.py`](../../body/internal_context/card_store/card_cache_file.py), [`quest_ai_runner/body/internal_context/card_store/card_limit_report.py`](../../body/internal_context/card_store/card_limit_report.py), [`quest_ai_runner/body/internal_context/card_store/card_store_fence.py`](../../body/internal_context/card_store/card_store_fence.py), [`quest_ai_runner/body/internal_context/card_store/card_store_merge.py`](../../body/internal_context/card_store/card_store_merge.py), [`quest_ai_runner/body/internal_context/card_store/card_store_migrate.py`](../../body/internal_context/card_store/card_store_migrate.py), [`quest_ai_runner/body/internal_context/card_store/card_store_tidy.py`](../../body/internal_context/card_store/card_store_tidy.py), [`quest_ai_runner/body/internal_context/card_store/card_store_upload.py`](../../body/internal_context/card_store/card_store_upload.py) and 101 more

### Top implementations of Feedback Learning

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | `ledger_guidance` | 75% (12 of 16 cases) | free |  |
| 2 | `dump_all` | 50% (8 of 16 cases) | free |  |
| 3 | `no_memory` | 25% (4 of 16 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Correction carry-through over the feedback-loop cases. the same feedback_loop component as guide-by-feedback (many-to-one): a correction recorded in the FeedbackLedger, a standing rule turned into a guidance card, a one-time request and an uncarded half of a mixed note kept out of guidance Read in this direction: lower the rate at which a person must repeat a correction.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| File-backed feedback ledger | `feedback-learning:ledger` | Keeps every tracked item in one small JSON document beside the lane's state, locked across threads and processes and written atomically, with a state machine of open, in progress, partially applied, needs reapplying and done. It exposes a two-method store seam so another store can replace the file, and offers standing rules to a guidance writer. | [`quest_ai_runner/consciousness/feedback_learning/feedback_ledger.py`](feedback_ledger.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
