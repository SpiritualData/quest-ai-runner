# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Live chat on a disposable world | `quest-self:dev_quest_chat` | Drives the real in-app chat end to end on a disposable development account, running each case in a fresh conversation and restoring the world afterwards. Hard deterministic pre-checks run first, then a model judge scores the transcript against the case rubric and named pivot facts. It counts model calls and tokens when run in process. | [`evaluation/components/chat_quality/impls/dev_quest_chat.py`](dev_quest_chat.py) |  |
| Offline metric validity check | `quest-self:reference_replies` | No Quest and no model: for each case it validates the case against the dataset schema, checks that the deterministic pre-checks accept a reference reply built from the case's own expected phrases, and checks that they reject an empty reply. It proves the scoring is able to tell a good reply from none. | [`evaluation/components/chat_quality/impls/reference_replies.py`](reference_replies.py) |  |
| Planner-only route check on the chat dataset | `quest-self:planner_route` | Runs QAR's own planning call on every user turn of the chat cases, with the pinned quest or the five world quests as the quest view, and compares the decision with the case's expected routing, forbid_writes and expected step tools. No Quest, no world, no writes, no reply. | [`evaluation/components/chat_quality/impls/planner_route.py`](planner_route.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
