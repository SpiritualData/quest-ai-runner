# impls

Breadcrumb: [Top](../../../../esoi/README.md) > impls

Part of [Quest AI Runner](../../../../esoi/README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Live chat on a disposable world | `quest-self:dev_quest_chat` | Drives the real in-app chat end to end on a disposable development account, running each case in a fresh conversation and restoring the world afterwards. Hard deterministic pre-checks run first, then a model judge scores the transcript against the case rubric and named pivot facts. It counts model calls and tokens when run in process. | [`evaluation/components/chat_quality/impls/dev_quest_chat.py`](dev_quest_chat.py) |  |
| Offline metric validity check | `quest-self:reference_replies` | No Quest and no model: for each case it validates the case against the dataset schema, checks that the deterministic pre-checks accept a reference reply built from the case's own expected phrases, and checks that they reject an empty reply. It proves the scoring is able to tell a good reply from none. | [`evaluation/components/chat_quality/impls/reference_replies.py`](reference_replies.py) |  |
| Planner-only route check on the chat dataset | `quest-self:planner_route` | Runs QAR's own planning call on every user turn of the chat cases, with the pinned quest or the five world quests as the quest view, and compares the decision with the case's expected routing, forbid_writes and expected step tools. No Quest, no world, no writes, no reply. | [`evaluation/components/chat_quality/impls/planner_route.py`](planner_route.py) |  |
| Production brain, writes blocked | `quest-self:prod_qar_readonly` | One fresh chat turn per case through the orchestrator a lane builds, in this process, with the lane's own configuration, context sources, preamble, personas, tools and Quest account, and the same pre-checks and model judge as the dev arm. QAR_DRY_RUN_WRITES=1 turns every state change (Quest writes, mutating tools, file writes, outward requests, the deep worker) into a recorded 'would have done' line that is shown to the judge and kept in the output log with the reads, model calls and environment. | [`evaluation/components/chat_quality/impls/prod_qar_readonly.py`](prod_qar_readonly.py) |  |
| Production brain, bounded writes | `quest-self:prod_qar` | The production arm of prod_qar_readonly with one difference: the writes a case's own effect field allows (one named tool with given arguments, or one Quest method and path) are really made, and every other state change stays blocked and recorded as 'would have done'. The case's state_check is read from Quest before and after the turn and reported; a case fails when a repeat made a duplicate or the expected item is missing, and on a repeat the judge also requires the reply to say it is already there. A change to a quest-derived spec record (computed by the backend from the quest's own fields) counts against the turn only when the turn executed a write that could reach those fields; otherwise it is reported as a concurrent change and left alone. | [`evaluation/components/chat_quality/impls/prod_qar.py`](prod_qar.py) |  |
| Production Quest AI chat over HTTP | `quest-self:prod_quest_chat` | Drives the real Quest AI chat on production as the AI account: one fresh conversation per case pinned to the lane's quest, one streamed message, every assistant message of the turn kept in order with timestamps, and the stored conversation read back. The chat's own routing, tools, scope guards and approvals run; nothing is stubbed. Only the approved private case ids and exact texts are sent, only the production API and the AI account are accepted, and the whole quest (notes, goals, workflows, spec records, tasks, decision-requests, chat conversations) is fingerprinted before and after: a change the case's effect allow-list does not explain is an UNEXPECTED WRITE and fails the case. Same pre-checks, probes and judge standards as the other production arms. | [`evaluation/components/chat_quality/impls/prod_quest_chat.py`](prod_quest_chat.py) |  |

## Where to go next

- Intent: [Quest AI Runner](../../../../esoi/README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
