# Subconscious

**Vision statement:** The right part of a person's own knowledge is already in mind when they ask.

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > Subconscious

This sub-intent arises as the implementation `the-subconscious:recency_window` of its parent intent [`the_intelligence`](../README.md). Opportunities: The cards a person's work leaves behind are copies of what lives elsewhere, so the store itself raises questions the parent solution does not ask: when a card has drifted from its source file, how an assistant keeps serving the last known listing while a backend is down and replays the writes that failed, how a store is kept proportionate to its sources instead of inflating with copies from a backups folder, which folders a corpus may index at all, and how a card is found by meaning when the person asks in other words. Each has alternatives that can be measured against each other.

## How the solutions fit together

Subconscious has the right part of a person's own knowledge ready before they ask. Intuition remembers what the AI expected to happen so a surprise can be noticed. Cards From Files builds topic cards from a person's own files and history, so no card is made for every file. Choose The Context picks the cards, files and conversations the request concerns.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- [Chosen Context](context_retriever/README.md) (sub-intent): The planner reads the material the request concerns, however the request is worded.
- Intuition (solution with no page of its own, its implementations are in this intent's records)
- Cards From Files (solution with no page of its own, its implementations are in this intent's records)
- Digest Records For A Need (solution with no page of its own, its implementations are in this intent's records): Lets a step that needs something from hundreds of records read all of them without a prompt that holds them, so cost and prompt size stay bounded and every finding can be traced to a record the person can open.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Intuition | `the-intuition` | `quest_ai_runner.run("the-intuition", 'quest-id')` | Atomic file prediction store (`file`) | Prediction store contract pass rate |
| Cards From Files | `cards-from-files` | `quest_ai_runner.run("cards-from-files", {'root': '.', 'files': [], 'existing': {}})` | QAR topic bootstrap (`qar_topic_bootstrap`) | Cards per source-file group after repeated passes, for the guards that prevent growth, Copies kept out with real material kept in, by review availability, Retrieval hits and category precision per unit of cost |
| Choose The Context | `choose-the-context` | `quest_ai_runner.run("choose-the-context", 'What should I work on first this week?')` | Per-lane assembly of the context members (today) (`per_lane_assembly`) | Context handed to the planner |
| Digest Records For A Need | `digest-records-for-a-need` | `quest_ai_runner.run("digest-records-for-a-need", 'What do my notes say about why I sleep badly?')` | Digest by need (sub-analyses over source groups) (`digest_by_need`) | Cited findings from a large body of records, bounded and cached |

## Solutions from a library

- [Intuition](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/subconscious/README.md) (`the-intuition`): What comes to mind for a request without being asked for: cards and facts the Subconscious offers because they are close to the request, which the Mind may take or ignore. Adopted from the library `hub`.
- [Cards From Files](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/subconscious/README.md) (`cards-from-files`): Proposes a proportionate, current set of cards for material a consumer already keeps in files, without writing them. Adopted from the library `hub`.
- [Context Retriever](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/subconscious/README.md) (`choose-the-context`): Cuts a corpus (files, the text of cards, or the turns of a conversation) into units at natural boundaries and returns, for a request, the units that answer it best first within a token budget, or nothing when the request needs no context. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Records Too Large To Read Whole (`prob-records-too-large-to-read`): A step that needs something from hundreds of the person's records either puts them all in one prompt, which costs more with every entry and buries what matters, or reads a few and misses the rest, and nothing says which record a statement rests on.

**Ideals**

- Findings Traced To Records (`ideal-findings-traced-to-records`): What a step is told about the person's records is the part that bears on its need, and every statement names the record it rests on.

## Metrics and their leaders

- Card repository conformance across backends (`metric-persist-cards`): leader `filesystem`, 14 of 14 cases
- Staleness detection precision and recall over mutation cases (`metric-detect-stale-cards`): leader `store_sha256`, 5 of 6 cases
- Outage resilience of the card repository (`metric-cache-cards`): leader `file_cache`, 12 of 12 cases
- Cards per source-file group and duplicate share after a repair of an inflated store (`metric-repair-a-card-store`): leader `bootstrap_guards`, 12 of 12 cases
- Cards per source-file group after repeated passes, for the guards that prevent growth (`metric-bound-card-growth`): leader `bootstrap_guards`, 12 of 12 cases
- Copies kept out with real material kept in, by review availability (`metric-choose-what-to-index`): leader `risk_gate`, 16 of 16 cases
- Retrieval hits and category precision per unit of cost (`metric-label-context-cards`): leader `fast_model_labels_fast`, 10 of 11 cases

## Where to go next

- Sub-intent: [Chosen Context](context_retriever/README.md)
- Parent intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_intelligence/subconscious/cards_from_files](cards_from_files/README.md), [quest_ai_runner/the_intelligence/subconscious/intuition](intuition/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Records Too Large To Read Whole (`prob-records-too-large-to-read`): A step that needs something from hundreds of the person's records either puts them all in one prompt, which costs more with every entry and buries what matters, or reads a few and misses the rest, and nothing says which record a statement rests on.

### Ideals

- Findings Traced To Records (`ideal-findings-traced-to-records`): What a step is told about the person's records is the part that bears on its need, and every statement names the record it rests on.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Intuition | `the-intuition` | Atomic file prediction store (`file`) | Prediction store contract pass rate |
| Cards From Files | `cards-from-files` | QAR topic bootstrap (`qar_topic_bootstrap`) | Cards per source-file group after repeated passes, for the guards that prevent growth, Copies kept out with real material kept in, by review availability, Retrieval hits and category precision per unit of cost |
| Choose The Context | `choose-the-context` | Per-lane assembly of the context members (today) (`per_lane_assembly`) | Context handed to the planner |
| Digest Records For A Need | `digest-records-for-a-need` | Digest by need (sub-analyses over source groups) (`digest_by_need`) | Cited findings from a large body of records, bounded and cached |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| In-memory reference store | `the-intuition:memory_store` | [evaluation/components/persist_predictions/impls/memory_store.py](../../../evaluation/components/persist_predictions/impls/memory_store.py) |  |
| Copy mode with no guard | `cards-from-files:no_cleanup` | [evaluation/components/card_growth/impls/no_cleanup.py](../../../evaluation/components/card_growth/impls/no_cleanup.py) |  |
| Bootstrap guards against re-import | `cards-from-files:bootstrap_guards` | [evaluation/components/card_growth/impls/bootstrap_guards.py](../../../evaluation/components/card_growth/impls/bootstrap_guards.py) |  |
| Risk-gated folder review | `cards-from-files:risk_gate` | [evaluation/components/choose_what_to_index/impls/risk_gate.py](../../../evaluation/components/choose_what_to_index/impls/risk_gate.py) |  |
| Model-judged folder review before the gate | `cards-from-files:review_only` | [evaluation/components/choose_what_to_index/impls/review_only.py](../../../evaluation/components/choose_what_to_index/impls/review_only.py) |  |
| Index everything (broken baseline) | `cards-from-files:index_everything` | [evaluation/components/choose_what_to_index/broken/index_everything.py](../../../evaluation/components/choose_what_to_index/broken/index_everything.py) |  |
| Path and title rules, no model call (floor, proposed) | `cards-from-files:path_rules` | [evaluation/components/label_context_cards/impls/path_rules.py](../../../evaluation/components/label_context_cards/impls/path_rules.py) |  |
| Per-lane assembly of the context members (today) | `choose-the-context:per_lane_assembly` | [quest_ai_runner/the_intelligence/subconscious/area_context_retriever.py](area_context_retriever.py) | [context_retriever](context_retriever/README.md) |
| Digest by need (sub-analyses over source groups) | `digest-records-for-a-need:digest_by_need` | [quest_ai_runner/the_intelligence/subconscious/digest_by_need.py](digest_by_need.py) |  |
| Whole collection in one prompt (broken baseline) | `digest-records-for-a-need:whole_collection` | [evaluation/components/digest_by_need/broken/whole_collection.py](../../../evaluation/components/digest_by_need/broken/whole_collection.py) |  |
| Fast-tier model writes metadata and categories (today) | `cards-from-files:fast_model_labels` | [quest_ai_runner/the_intelligence/subconscious/cards_from_files/card_metadata_generator.py](cards_from_files/card_metadata_generator.py) |  |
| QAR topic bootstrap | `cards-from-files:qar_topic_bootstrap` | [quest_ai_runner/the_intelligence/subconscious/cards_from_files/qar_topic_bootstrap.py](cards_from_files/qar_topic_bootstrap.py) |  |
| Atomic file prediction store | `the-intuition:file` | [quest_ai_runner/the_intelligence/subconscious/intuition/anticipation_store.py](intuition/anticipation_store.py) |  |

### Metrics

- Card repository conformance across backends (`metric-persist-cards`): `esoi run persist_cards --impl quest_api`
- Staleness detection precision and recall over mutation cases (`metric-detect-stale-cards`): `esoi run staleness --impl store_sha256`
- Outage resilience of the card repository (`metric-cache-cards`): `esoi run cache_cards --impl file_cache`
- Cards per source-file group and duplicate share after a repair of an inflated store (`metric-repair-a-card-store`): `esoi run card_growth --impl repository_tidy; esoi run card_growth --impl quarantine_tool; esoi run card_growth --impl no_cleanup`
- Cards per source-file group after repeated passes, for the guards that prevent growth (`metric-bound-card-growth`): `esoi run card_growth --impl bootstrap_guards; esoi run card_store_growth --impl live_store; esoi run card_growth --impl no_cleanup`
- Copies kept out with real material kept in, by review availability (`metric-choose-what-to-index`): `esoi run choose_what_to_index --impl risk_gate; esoi run choose_what_to_index --impl review_only`
- Retrieval hits and category precision per unit of cost (`metric-label-context-cards`): `esoi run label_context_cards --impl fast_model_labels`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
