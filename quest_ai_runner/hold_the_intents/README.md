# Holding the intents across quests

Each AI representative starts every request from the standing view of the quests it is assigned and the intents above them, so a correction is made once and a thread is never restated.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `hold-the-intents:separate_stores` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A representative works across many quests, so the standing view it needs is wider than any one request: past conversations, recent context, the guidance learned from corrections, the ledger of what was asked, the predictions made and the person's long-term goal. That opens questions the parent solution does not ask: how a correction is kept in the person's own words and tracked until it is followed, how a view is held per quest, per intent and per representative without crossing scopes, how a reply is aimed at the person's end without hijacking a simple question, and which representative a request runs as, the same way in chat, tasks and background work.

## How the solutions fit together

Higher mind keeps the long view across turns. It carries what was said and found through Cross-conversation recall store and Scoped recent-context warm start. It keeps and applies a person's corrections through Verbatim feedback ledger and Feedback-to-guidance loop. It saves what was learned through Scoped prediction store and Context card persistence backend. It keeps work aimed at the person's end through Guidance toward the long-term goal, and picks the representative a request runs as through Representative selection. Representative selection sends the queued work of that representative through Queued-task runner.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Feedback-to-guidance loop<br/><i>guide-by-feedback</i>"]
    s1["Representative selection<br/><i>select-a-rep</i>"]
    s0 -->|"A list of guidance cards with bodies for the applicable-guidance block; a catalog and single card reads on demand"| s1
```

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Cross-conversation recall store | `remember-conversations` | `quest_ai_runner.run("remember-conversations", ...)` | Local session-file conversation store (`session_file`) | Past-conversation recall and isolation over synthetic cases |
| Feedback-to-guidance loop | `guide-by-feedback` | `quest_ai_runner.run("guide-by-feedback", 'What should I work on first this week?')` | Universal guidance provider (`universal`) | Repeat-correction recall and precision over feedback sequences |
| Verbatim feedback ledger | `store-feedback` | `quest_ai_runner.run("store-feedback", 'quest-id')` | File-backed feedback ledger (`ledger`) | Correction carry-through over the feedback-loop cases |
| Scoped recent-context warm start | `recall-recent-context` | `quest_ai_runner.run("recall-recent-context", 'quest-id')` | Lexical-gated scoped file store (`file`) | Warm-start accuracy with no cross-work-item leakage |
| Scoped prediction store | `persist-predictions` | `quest_ai_runner.run("persist-predictions", 'quest-id')` | Atomic file prediction store (`file`) | Prediction store contract pass rate |
| Representative selection | `select-a-rep` | `quest_ai_runner.run("select-a-rep", {'assignee_rep_id': 'rep-1'})` | Persona resolver precedence for queued tasks (today) (`persona_resolver_precedence`) | Representative choice accuracy and thread consistency |
| Guidance toward the long-term goal | `guide-toward-the-long-term-goal` | `quest_ai_runner.run("guide-toward-the-long-term-goal", 'What should I work on first this week?')` | Long-term guidance section in the reply voice (`guided_v2`) | Two-sided long-term guidance |

## Problems and ideals

**Problems**

- People give the same correction again and again because the AI does not keep what they said, or keeps it but does not apply it where it bears. (`prob-corrections-repeat`)
- Data from one user, team or quest reaches another one's prompt or store, through retrieval, caches, conversation recall, shared stores or the sources fed to the AI. (`prob-private-data-crosses-scope`)
- Which AI representative handles a message, a queued task or an autopilot pass is decided in three places by three rules (the host's chat classifier among a quest's assigned representatives, the task runner's precedence of assignment field, explicit ask and domain cards, and the autopilot roster by day), with no shared definition of the right representative, no labelled measure of how often it is right, and no rule that work spawned from a thread keeps that thread's representative. (`prob-wrong-or-inconsistent-rep`)
- The AI answers the request in front of it and lets the person's long-term goal go quiet, so each small task is done without the end in sight; the opposite fix, a plug for the goal in every reply, nags, hijacks a simple question or lands on a person who is in a hurry or upset. (`prob-long-term-goal-goes-quiet`)

**Ideals**

- Lasting Correction (`ideal-lasting-correction`): A correction given once is kept as said and applied wherever it bears.
- Contained Scope (`ideal-contained-scope`): Data stays inside the user, team and quest it belongs to, on every read and write path.
- Moves Me Forward Without Nagging (`ideal-moves-me-forward`): From the call: every reply answers what was asked first and, at a natural moment, keeps the person's next milestone in view, at the intent level they are working at.

## Metrics and their leaders

- Past-conversation recall and isolation over synthetic cases (`metric-remember-conversations`): leader `session_files`, 9 of 10 cases
- Repeat-correction recall and precision over feedback sequences (`metric-guide-by-feedback`): leader `ledger_guidance`, 12 of 16 cases
- Correction carry-through over the feedback-loop cases (`metric-store-feedback`): leader `ledger_guidance`, 12 of 16 cases
- Warm-start accuracy with no cross-work-item leakage (`metric-recall-recent-context`): leader `file_store_filter`, 9 of 10 cases
- Prediction store contract pass rate (`metric-persist-predictions`): leader `file_store`, 12 of 12 cases
- Representative choice accuracy and thread consistency (`metric-select-a-rep`): leader `persona_resolver_precedence`, 14 of 16 cases
- Two-sided long-term guidance (`metric-guide-toward-the-long-term-goal`): leader `guided_v2`, 9 of 10 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- People give the same correction again and again because the AI does not keep what they said, or keeps it but does not apply it where it bears. (`prob-corrections-repeat`)
- Data from one user, team or quest reaches another one's prompt or store, through retrieval, caches, conversation recall, shared stores or the sources fed to the AI. (`prob-private-data-crosses-scope`)
- Which AI representative handles a message, a queued task or an autopilot pass is decided in three places by three rules (the host's chat classifier among a quest's assigned representatives, the task runner's precedence of assignment field, explicit ask and domain cards, and the autopilot roster by day), with no shared definition of the right representative, no labelled measure of how often it is right, and no rule that work spawned from a thread keeps that thread's representative. (`prob-wrong-or-inconsistent-rep`)
- The AI answers the request in front of it and lets the person's long-term goal go quiet, so each small task is done without the end in sight; the opposite fix, a plug for the goal in every reply, nags, hijacks a simple question or lands on a person who is in a hurry or upset. (`prob-long-term-goal-goes-quiet`)

### Ideals

- Lasting Correction (`ideal-lasting-correction`): A correction given once is kept as said and applied wherever it bears.
- Contained Scope (`ideal-contained-scope`): Data stays inside the user, team and quest it belongs to, on every read and write path.
- Moves Me Forward Without Nagging (`ideal-moves-me-forward`): From the call: every reply answers what was asked first and, at a natural moment, keeps the person's next milestone in view, at the intent level they are working at.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Cross-conversation recall store | `remember-conversations` | Local session-file conversation store (`session_file`) | Past-conversation recall and isolation over synthetic cases |
| Feedback-to-guidance loop | `guide-by-feedback` | Universal guidance provider (`universal`) | Repeat-correction recall and precision over feedback sequences |
| Verbatim feedback ledger | `store-feedback` | File-backed feedback ledger (`ledger`) | Correction carry-through over the feedback-loop cases |
| Scoped recent-context warm start | `recall-recent-context` | Lexical-gated scoped file store (`file`) | Warm-start accuracy with no cross-work-item leakage |
| Scoped prediction store | `persist-predictions` | Atomic file prediction store (`file`) | Prediction store contract pass rate |
| Representative selection | `select-a-rep` | Persona resolver precedence for queued tasks (today) (`persona_resolver_precedence`) | Representative choice accuracy and thread consistency |
| Guidance toward the long-term goal | `guide-toward-the-long-term-goal` | Long-term guidance section in the reply voice (`guided_v2`) | Two-sided long-term guidance |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Local session-file conversation store | `remember-conversations:session_file` | [quest_ai_runner/adapters/session_file_conversation_store.py](../adapters/session_file_conversation_store.py) |  |
| Universal guidance provider | `guide-by-feedback:universal` | [quest_ai_runner/adapters/guidance_card_manager.py](../adapters/guidance_card_manager.py) |  |
| Live ledger and provider arm | `guide-by-feedback:ledger_guidance` | [evaluation/components/feedback_loop/impls/ledger_guidance.py](../../evaluation/components/feedback_loop/impls/ledger_guidance.py) |  |
| No-feedback floor | `guide-by-feedback:no_memory` | [evaluation/components/feedback_loop/impls/no_memory.py](../../evaluation/components/feedback_loop/impls/no_memory.py) |  |
| Show-everything baseline | `guide-by-feedback:dump_all` | [evaluation/components/feedback_loop/impls/dump_all.py](../../evaluation/components/feedback_loop/impls/dump_all.py) |  |
| File-backed feedback ledger | `store-feedback:ledger` | [quest_ai_runner/hold_the_intents/feedback_ledger.py](feedback_ledger.py) |  |
| Ledger plus guidance bridge, end to end | `store-feedback:ledger_guidance` | [evaluation/components/feedback_loop/impls/ledger_guidance.py](../../evaluation/components/feedback_loop/impls/ledger_guidance.py) |  |
| No-memory floor | `store-feedback:no_memory` | [evaluation/components/feedback_loop/impls/no_memory.py](../../evaluation/components/feedback_loop/impls/no_memory.py) |  |
| Remember-everything baseline | `store-feedback:dump_all` | [evaluation/components/feedback_loop/impls/dump_all.py](../../evaluation/components/feedback_loop/impls/dump_all.py) |  |
| Lexical-gated scoped file store | `recall-recent-context:file` | [quest_ai_runner/core/recent_context.py](../core/recent_context.py) |  |
| Atomic file prediction store | `persist-predictions:file` | [quest_ai_runner/core/anticipation_store.py](../core/anticipation_store.py) |  |
| In-memory reference store | `persist-predictions:memory_store` | [evaluation/components/persist_predictions/impls/memory_store.py](../../evaluation/components/persist_predictions/impls/memory_store.py) |  |
| Persona resolver precedence for queued tasks (today) | `select-a-rep:persona_resolver_precedence` | [quest_ai_runner/runner/personas.py](../runner/personas.py) |  |
| Host chat classifier among assigned representatives (today, in the host) | `select-a-rep:host_assigned_classifier` |  |  |
| A thread keeps its representative (proposed) | `select-a-rep:thread_holds_its_rep` |  |  |
| Fit by role, assigned quests and intent branch (proposed) | `select-a-rep:fit_by_role_and_quests` |  |  |
| Ask the person on an equal fit (proposed) | `select-a-rep:ask_the_person` |  |  |
| Reply voice before the long-term guidance section (prior) | `guide-toward-the-long-term-goal:reply_voice` |  |  |
| Reply voice plus a long-term guidance directive (proposed) | `guide-toward-the-long-term-goal:guided` |  |  |
| Plain answer only (floor) | `guide-toward-the-long-term-goal:no_nudge` |  |  |
| Long-term guidance section in the reply voice | `guide-toward-the-long-term-goal:guided_v2` | [quest_ai_runner/core/auxiliary_prompts.py](../core/auxiliary_prompts.py) |  |

### Metrics

- Past-conversation recall and isolation over synthetic cases (`metric-remember-conversations`): `esoi run conversation_recall --impl session_files`
- Repeat-correction recall and precision over feedback sequences (`metric-guide-by-feedback`): `esoi run feedback_loop --impl ledger_guidance`
- Correction carry-through over the feedback-loop cases (`metric-store-feedback`): `esoi run feedback_loop --impl ledger_guidance`
- Warm-start accuracy with no cross-work-item leakage (`metric-recall-recent-context`): `esoi run recent_context --impl file_store_filter`
- Prediction store contract pass rate (`metric-persist-predictions`): `esoi run persist_predictions --impl file_store`
- Representative choice accuracy and thread consistency (`metric-select-a-rep`): `esoi run select_a_rep --impl persona_resolver_precedence`
- Two-sided long-term guidance (`metric-guide-toward-the-long-term-goal`): `esoi run proactive_guidance --impl guided_v2`
