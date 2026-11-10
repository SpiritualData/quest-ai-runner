# Feedback Learning

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > Feedback Learning

Part of [Quest Consciousness](../README.md)

## Feedback Learning (`feedback-learning`)

- Entry call: `quest_ai_runner.run("feedback-learning", 'quest-id')`
- Selected implementation: File-backed feedback ledger (`ledger`)
- Metric: Correction carry-through over the feedback-loop cases
- Implemented by: [`quest_ai_runner/the_body/internal_context/card_store/__init__.py`](../../the_body/internal_context/card_store/__init__.py), [`quest_ai_runner/the_body/internal_context/card_store/card_cache_file.py`](../../the_body/internal_context/card_store/card_cache_file.py), [`quest_ai_runner/the_body/internal_context/card_store/card_limit_report.py`](../../the_body/internal_context/card_store/card_limit_report.py), [`quest_ai_runner/the_body/internal_context/card_store/card_store_fence.py`](../../the_body/internal_context/card_store/card_store_fence.py), [`quest_ai_runner/the_body/internal_context/card_store/card_store_merge.py`](../../the_body/internal_context/card_store/card_store_merge.py), [`quest_ai_runner/the_body/internal_context/card_store/card_store_migrate.py`](../../the_body/internal_context/card_store/card_store_migrate.py), [`quest_ai_runner/the_body/internal_context/card_store/card_store_tidy.py`](../../the_body/internal_context/card_store/card_store_tidy.py), [`quest_ai_runner/the_body/internal_context/card_store/card_store_upload.py`](../../the_body/internal_context/card_store/card_store_upload.py) and 98 more

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| File-backed feedback ledger | `feedback-learning:ledger` | Keeps every tracked item in one small JSON document beside the lane's state, locked across threads and processes and written atomically, with a state machine of open, in progress, partially applied, needs reapplying and done. It exposes a two-method store seam so another store can replace the file, and offers standing rules to a guidance writer. | [`quest_ai_runner/the_consciousness/feedback_learning/feedback_ledger.py`](feedback_ledger.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
