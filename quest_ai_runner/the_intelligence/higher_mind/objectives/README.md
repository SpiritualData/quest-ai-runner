# Quest Intents

**Vision statement:** The AI works from the person's quests.

Breadcrumb: [Top](../../../../esoi/README.md) > [Intelligence](../../README.md) > Quest Intents

This sub-intent arises as the implementation `quest-intents:separate_stores` of its parent intent [`the_intelligence`](../../README.md). Opportunities: A representative works across many quests, so the standing view it needs is wider than any one request: past conversations, recent context, the guidance learned from corrections, the ledger of what was asked, the predictions made and the person's long-term goal. That opens questions the parent solution does not ask: how a correction is kept in the person's own words and tracked until it is followed, how a view is held per quest, per intent and per representative without crossing scopes, how a reply is aimed at the person's end without hijacking a simple question, and which representative a request runs as, the same way in chat, tasks and background work.

## How the solutions fit together

Place On A Quest says which of the person's quests a message, goal, note or task belongs to, or none, by what each quest holds and means and a model's pick among the best candidates, through the one function the host supplies, so every surface picks a quest the same way and a pinned quest is left alone. Quest Intents keeps the person's quests as the standing view every request is read against, and its view hands the person's goals to Prioritization as the milestones of their solutions so they come in the order that matters. Long-Term Memory keeps what was said across conversations and says what bears on a request. Recall Recent Context hands the next turn the cards its last turns already found. Guidance ties the work to its purpose by framing the intent a quest sits under. Guide Toward The Long-Term Goal points a reply at the quest's next milestone without nagging.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- Long-Term Memory (solution with no page of its own, its implementations are in this intent's records)
- Guidance (solution with no page of its own, its implementations are in this intent's records)
- Recall Recent Context (solution with no page of its own, its implementations are in this intent's records): Hands the next turn of a conversation the cards its last turns already found, so a short follow-up needs no restating.
- Guide Toward The Long-Term Goal (solution with no page of its own, its implementations are in this intent's records): Keeps the person moving toward the end they are working for, without hijacking a simple question or nagging.
- [Place On A Quest](place_on_a_quest/README.md) (solution with implementations): Say which of the person's quests a message, goal, note or task belongs to, or none, the same way on every surface that picks one.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Long-Term Memory | `long-term-memory` | `quest_ai_runner.run("long-term-memory", ...)` | Local session-file conversation store (`session_file`) | Past-conversation recall and isolation over synthetic cases |
| Guidance | `guidance` | `quest_ai_runner.run("guidance", 'quest-id')` | Computed frame in the prompt (`frame-line`) | Intent-grounded pass rate |
| Recall Recent Context | `recall-recent-context` | `quest_ai_runner.run("recall-recent-context", 'quest-id')` | Lexical-gated scoped file store (`file`) | Warm-start accuracy with no cross-work-item leakage |
| Guide Toward The Long-Term Goal | `guide-toward-the-long-term-goal` | `quest_ai_runner.run("guide-toward-the-long-term-goal", 'What should I work on first this week?')` | Long-term guidance section in the reply voice (`guided_v2`) | Two-sided long-term guidance |
| Place On A Quest | `place-on-a-quest` | `quest_ai_runner.run("place-on-a-quest", ...)` | Words and meaning candidates, the model picks (`hybrid-candidate-ranker`) | Quest placement accuracy over labelled messages, balanced with cost and time |

## Solutions from a library

- [Long-Term Memory](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/higher_mind/README.md) (`long-term-memory`): The retention policy and the recall over the Body's store: what the system keeps across requests, for how long, and how it is found again. Adopted from the library `hub`.
- [Guidance](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/higher_mind/README.md) (`guidance`): An infrequent, longer-term nudge to the Planner, drawn from the objectives, and the context-specific criteria for what a good response is in this situation. Adopted from the library `hub`.
- [Long-Term Memory](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/higher_mind/README.md) (`recall-recent-context`): The retention policy and the recall over the Body's store: what the system keeps across requests, for how long, and how it is found again. Specializes the library `hub`.
- [Guidance](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/higher_mind/README.md) (`guide-toward-the-long-term-goal`): An infrequent, longer-term nudge to the Planner, drawn from the objectives, and the context-specific criteria for what a good response is in this situation. Specializes the library `hub`.
- [Objectives](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/higher_mind/README.md) (`place-on-a-quest`): The live hierarchy of intents and goals across requests. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Messages Not Placed On The Quest They Belong To (`prob-message-lands-on-no-quest`): A message, a goal, a note or a task is attached to a quest by whichever surface received it, each with its own way of picking (the first quest by priority, a word match, an embedding search, a model asked about the quests it was shown), so the same sentence lands on different quests in different places, a quest past the model's window is never offered, and a message no quest fits is given one anyway or none is asked about.

**Ideals**

- Message Served By Its Quest (`ideal-message-served-by-its-quest`): Every message, goal, note and task is served by the quest it is about, or by none when no quest fits.

## Metrics and their leaders

- Past-conversation recall and isolation over synthetic cases (`metric-remember-conversations`): leader `session_files`, 9 of 10 cases
- Repeat-correction recall and precision over feedback sequences (`metric-guide-by-feedback`): leader `ledger_guidance`, 12 of 16 cases
- Correction carry-through over the feedback-loop cases (`metric-store-feedback`): leader `ledger_guidance`, 12 of 16 cases
- Warm-start accuracy with no cross-work-item leakage (`metric-recall-recent-context`): leader `file_store_filter`, 9 of 10 cases
- Prediction store contract pass rate (`metric-persist-predictions`): leader `file_store`, 12 of 12 cases
- Representative choice accuracy and thread consistency (`metric-select-a-rep`): leader `persona_resolver_precedence`, 14 of 16 cases
- Two-sided long-term guidance (`metric-guide-toward-the-long-term-goal`): leader `guided_v2`, 9 of 10 cases
- Quest placement accuracy over labelled messages, balanced with cost and time (`metric-place-on-a-quest`): leader `hybrid_candidate_ranker_model`, 48 of 50 cases

## Where to go next

- Parent intent: [Intelligence](../../README.md)
- Repository: [top README](../../../../README.md)
- Records beside the code: [quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest](place_on_a_quest/README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Messages Not Placed On The Quest They Belong To (`prob-message-lands-on-no-quest`): A message, a goal, a note or a task is attached to a quest by whichever surface received it, each with its own way of picking (the first quest by priority, a word match, an embedding search, a model asked about the quests it was shown), so the same sentence lands on different quests in different places, a quest past the model's window is never offered, and a message no quest fits is given one anyway or none is asked about.

### Ideals

- Message Served By Its Quest (`ideal-message-served-by-its-quest`): Every message, goal, note and task is served by the quest it is about, or by none when no quest fits.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Long-Term Memory | `long-term-memory` | Local session-file conversation store (`session_file`) | Past-conversation recall and isolation over synthetic cases |
| Guidance | `guidance` | Computed frame in the prompt (`frame-line`) | Intent-grounded pass rate |
| Recall Recent Context | `recall-recent-context` | Lexical-gated scoped file store (`file`) | Warm-start accuracy with no cross-work-item leakage |
| Guide Toward The Long-Term Goal | `guide-toward-the-long-term-goal` | Long-term guidance section in the reply voice (`guided_v2`) | Two-sided long-term guidance |
| Place On A Quest | `place-on-a-quest` | Words and meaning candidates, the model picks (`hybrid-candidate-ranker`) | Quest placement accuracy over labelled messages, balanced with cost and time |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Prompts without the intent frame | `guidance:none` | [quest_ai_runner/the_intelligence/proactivity/autopilot_briefs.py](../../proactivity/autopilot_briefs.py) |  |
| Computed frame in the prompt | `guidance:frame-line` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](../../critical_thinking/area.py) |  |
| Frame plus intent card and record drill-down | `guidance:frame-card-drilldown` | [quest_ai_runner/the_body/internal_context/quest/client/quest_client_intents.py](../../../the_body/internal_context/quest/client/quest_client_intents.py) |  |
| Frame plus a lazily generated quest specification | `guidance:frame-lazy-spec` | [quest_ai_runner/the_body/hands/quest_operations/tools_quest_intents.py](../../../the_body/hands/quest_operations/tools_quest_intents.py) |  |
| Local session-file conversation store | `long-term-memory:session_file` | [quest_ai_runner/the_intelligence/higher_mind/long_term_memory/session_file_conversation_store.py](../long_term_memory/session_file_conversation_store.py) |  |
| Lexical-gated scoped file store | `recall-recent-context:file` | [quest_ai_runner/the_intelligence/higher_mind/long_term_memory/recent_context.py](../long_term_memory/recent_context.py) |  |
| Reply voice before the long-term guidance section (prior) | `guide-toward-the-long-term-goal:reply_voice` |  |  |
| Reply voice plus a long-term guidance directive (proposed) | `guide-toward-the-long-term-goal:guided` |  |  |
| Plain answer only (floor) | `guide-toward-the-long-term-goal:no_nudge` |  |  |
| Long-term guidance section in the reply voice | `guide-toward-the-long-term-goal:guided_v2` | [quest_ai_runner/the_self/integrity/prompts/auxiliary_prompts.py](../../../the_self/integrity/prompts/auxiliary_prompts.py) |  |
| Embedding-led candidates, the model picks | `place-on-a-quest:shared-quest-ranker` | [evaluation/components/place_on_a_quest/impls/shared_quest_ranker.py](../../../../evaluation/components/place_on_a_quest/impls/shared_quest_ranker.py) |  |
| Words and meaning candidates, the model picks | `place-on-a-quest:hybrid-candidate-ranker` | [quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest/quest_placement.py](place_on_a_quest/quest_placement.py) |  |
| The library judge over the quests it is given | `place-on-a-quest:library-quest-judge` | [quest_ai_runner/the_intelligence/higher_mind/objectives/place_on_a_quest/quest_placement.py](place_on_a_quest/quest_placement.py) |  |

### Metrics

- Past-conversation recall and isolation over synthetic cases (`metric-remember-conversations`): `esoi run conversation_recall --impl session_files`
- Repeat-correction recall and precision over feedback sequences (`metric-guide-by-feedback`): `esoi run feedback_loop --impl ledger_guidance`
- Correction carry-through over the feedback-loop cases (`metric-store-feedback`): `esoi run feedback_loop --impl ledger_guidance`
- Warm-start accuracy with no cross-work-item leakage (`metric-recall-recent-context`): `esoi run recent_context --impl file_store_filter`
- Prediction store contract pass rate (`metric-persist-predictions`): `esoi run persist_predictions --impl file_store`
- Representative choice accuracy and thread consistency (`metric-select-a-rep`): `esoi run select_a_rep --impl persona_resolver_precedence`
- Two-sided long-term guidance (`metric-guide-toward-the-long-term-goal`): `esoi run proactive_guidance --impl guided_v2`
- Quest placement accuracy over labelled messages, balanced with cost and time (`metric-place-on-a-quest`): `esoi run place_on_a_quest --impl hybrid_candidate_ranker_model`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
