# Subconscious

The AI optimally recalls the part of the person's own knowledge that each request needs.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `the-subconscious:recency_window` of its parent intent [`the_intelligence`](../README.md). Opportunities: The cards a person's work leaves behind are copies of what lives elsewhere, so the store itself raises questions the parent solution does not ask: when a card has drifted from its source file, how an assistant keeps serving the last known listing while a backend is down and replays the writes that failed, how a store is kept proportionate to its sources instead of inflating with copies from a backups folder, which folders a corpus may index at all, and how a card is found by meaning when the person asks in other words. Each has alternatives that can be measured against each other.

## How the solutions fit together

Subconscious finds and prepares what a request needs before the Mind asks. Cards From Files builds topic cards from a person's own files and history, and Card Store keeps those cards in Quest, so that no card is made for every file.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Card Store<br/><i>card-store</i>"]
    s1["Cards From Files<br/><i>cards-from-files</i>"]
    s1 --> s0
```

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Card Store | `card-store` | `quest_ai_runner.run("card-store", 'rust release')` | Quest backend card API from the environment (`quest-api-cards`) | Staleness detection precision and recall over mutation cases, Cards per source-file group and duplicate share after a repair of an inflated store |
| Cards From Files | `cards-from-files` | `quest_ai_runner.run("cards-from-files", {'root': '.', 'files': [], 'existing': {}})` | QAR topic bootstrap (`qar_topic_bootstrap`) | Cards per source-file group after repeated passes, for the guards that prevent growth, Copies kept out with real material kept in, by review availability, Retrieval hits and category precision per unit of cost |

## Solutions from a library

- [Card Store](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_body/README.md) (`card-store`): A small store that keeps cards, the hub's own memory unit, and finds them again by words or a tag. Adopted from the library `hub`.
- [Cards From Files](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/subconscious/README.md) (`cards-from-files`): Proposes a proportionate, current set of cards for material a consumer already keeps in files, without writing them. Adopted from the library `hub`.

## Metrics and their leaders

- Card repository conformance across backends (`metric-persist-cards`): leader `filesystem`, 14 of 14 cases
- Staleness detection precision and recall over mutation cases (`metric-detect-stale-cards`): leader `store_sha256`, 5 of 6 cases
- Outage resilience of the card repository (`metric-cache-cards`): leader `file_cache`, 12 of 12 cases
- Cards per source-file group and duplicate share after a repair of an inflated store (`metric-repair-a-card-store`): leader `bootstrap_guards`, 12 of 12 cases
- Cards per source-file group after repeated passes, for the guards that prevent growth (`metric-bound-card-growth`): leader `bootstrap_guards`, 12 of 12 cases
- Copies kept out with real material kept in, by review availability (`metric-choose-what-to-index`): leader `risk_gate`, 16 of 16 cases
- Retrieval hits and category precision per unit of cost (`metric-label-context-cards`): leader `fast_model_labels_fast`, 10 of 11 cases

## Where to go next

- Sub-intent: [Selecting the context a planner reads](context_retriever/README.md) (The planner reads the material the request concerns, however the request is worded.)
- Parent intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_intelligence/subconscious/cards_from_files](cards_from_files/README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Card Store | `card-store` | Quest backend card API from the environment (`quest-api-cards`) | Staleness detection precision and recall over mutation cases, Cards per source-file group and duplicate share after a repair of an inflated store |
| Cards From Files | `cards-from-files` | QAR topic bootstrap (`qar_topic_bootstrap`) | Cards per source-file group after repeated passes, for the guards that prevent growth, Copies kept out with real material kept in, by review availability, Retrieval hits and category precision per unit of cost |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Atomic JSON file repository | `persist-cards:filesystem` | [quest_ai_runner/the_body/configuration/config_assembler.py](../../the_body/configuration/config_assembler.py) |  |
| Vector database repository | `persist-cards:qdrant` | [quest_ai_runner/the_body/internal_context/card_store/qdrant_card_repository.py](../../the_body/internal_context/card_store/qdrant_card_repository.py) |  |
| Hosted card API repository | `persist-cards:quest_api` | [quest_ai_runner/the_body/internal_context/card_store/quest_api_card_repository.py](../../the_body/internal_context/card_store/quest_api_card_repository.py) |  |
| Content-hash fingerprint check | `card-store:store_sha256` | [evaluation/components/staleness/impls/store_sha256.py](../../../evaluation/components/staleness/impls/store_sha256.py) |  |
| Timestamp-and-size baseline | `card-store:mtime_size` | [evaluation/components/staleness/impls/mtime_size.py](../../../evaluation/components/staleness/impls/mtime_size.py) |  |
| Card repository with a cache seam and a null cache | `cache-cards:null` | [quest_ai_runner/the_body/internal_context/card_store/quest_api_card_repository.py](../../the_body/internal_context/card_store/quest_api_card_repository.py) |  |
| Durable file cache | `cache-cards:file` | [quest_ai_runner/the_body/internal_context/card_store/card_cache_file.py](../../the_body/internal_context/card_store/card_cache_file.py) |  |
| No-op cache (shipped default of the seam) | `cache-cards:null_cache` | [evaluation/components/cache_cards/impls/null_cache.py](../../../evaluation/components/cache_cards/impls/null_cache.py) |  |
| In-process memory cache | `cache-cards:memory_cache` | [evaluation/components/cache_cards/impls/memory_cache.py](../../../evaluation/components/cache_cards/impls/memory_cache.py) |  |
| Durable file cache under outage scenarios | `cache-cards:file_cache` | [evaluation/components/cache_cards/impls/file_cache.py](../../../evaluation/components/cache_cards/impls/file_cache.py) |  |
| Card quarantine tool | `card-store:quarantine_tool` | [evaluation/components/card_growth/impls/quarantine_tool.py](../../../evaluation/components/card_growth/impls/quarantine_tool.py) |  |
| Copy mode with no guard | `cards-from-files:no_cleanup` | [evaluation/components/card_growth/impls/no_cleanup.py](../../../evaluation/components/card_growth/impls/no_cleanup.py) |  |
| Bootstrap guards against re-import | `cards-from-files:bootstrap_guards` | [evaluation/components/card_growth/impls/bootstrap_guards.py](../../../evaluation/components/card_growth/impls/bootstrap_guards.py) |  |
| Risk-gated folder review | `cards-from-files:risk_gate` | [evaluation/components/choose_what_to_index/impls/risk_gate.py](../../../evaluation/components/choose_what_to_index/impls/risk_gate.py) |  |
| Model-judged folder review before the gate | `cards-from-files:review_only` | [evaluation/components/choose_what_to_index/impls/review_only.py](../../../evaluation/components/choose_what_to_index/impls/review_only.py) |  |
| Index everything (broken baseline) | `cards-from-files:index_everything` | [evaluation/components/choose_what_to_index/broken/index_everything.py](../../../evaluation/components/choose_what_to_index/broken/index_everything.py) |  |
| Repository tidy | `card-store:repository_tidy` | [quest_ai_runner/the_body/internal_context/card_store/card_store_tidy.py](../../the_body/internal_context/card_store/card_store_tidy.py) |  |
| Tidy at open of a local folder | `card-store:local_folder_tidy` | [quest_ai_runner/the_body/internal_context/card_store/context_store_heal.py](../../the_body/internal_context/card_store/context_store_heal.py) |  |
| Store as copy mode leaves it | `card-store:no_cleanup` | [evaluation/components/card_growth/impls/no_cleanup.py](../../../evaluation/components/card_growth/impls/no_cleanup.py) |  |
| Path and title rules, no model call (floor, proposed) | `cards-from-files:path_rules` | [evaluation/components/label_context_cards/impls/path_rules.py](../../../evaluation/components/label_context_cards/impls/path_rules.py) |  |
| QAR card repository | `card-store:qar-card-repository` | [quest_ai_runner/the_body/internal_context/card_store/hub_card_store.py](../../the_body/internal_context/card_store/hub_card_store.py) |  |
| Quest backend card API from the environment | `card-store:quest-api-cards` | [quest_ai_runner/the_body/internal_context/card_store/hub_card_store.py](../../the_body/internal_context/card_store/hub_card_store.py) |  |
| Per-lane assembly of the context members (today) | `choose-the-context:per_lane_assembly` | [quest_ai_runner/the_intelligence/subconscious/area_context_retriever.py](area_context_retriever.py) | [context_retriever](context_retriever/README.md) |
| Fast-tier model writes metadata and categories (today) | `cards-from-files:fast_model_labels` | [quest_ai_runner/the_intelligence/subconscious/cards_from_files/card_metadata_generator.py](cards_from_files/card_metadata_generator.py) |  |
| QAR topic bootstrap | `cards-from-files:qar_topic_bootstrap` | [quest_ai_runner/the_intelligence/subconscious/cards_from_files/qar_topic_bootstrap.py](cards_from_files/qar_topic_bootstrap.py) |  |

### Metrics

- Card repository conformance across backends (`metric-persist-cards`): `esoi run persist_cards --impl quest_api`
- Staleness detection precision and recall over mutation cases (`metric-detect-stale-cards`): `esoi run staleness --impl store_sha256`
- Outage resilience of the card repository (`metric-cache-cards`): `esoi run cache_cards --impl file_cache`
- Cards per source-file group and duplicate share after a repair of an inflated store (`metric-repair-a-card-store`): `esoi run card_growth --impl repository_tidy; esoi run card_growth --impl quarantine_tool; esoi run card_growth --impl no_cleanup`
- Cards per source-file group after repeated passes, for the guards that prevent growth (`metric-bound-card-growth`): `esoi run card_growth --impl bootstrap_guards; esoi run card_store_growth --impl live_store; esoi run card_growth --impl no_cleanup`
- Copies kept out with real material kept in, by review availability (`metric-choose-what-to-index`): `esoi run choose_what_to_index --impl risk_gate; esoi run choose_what_to_index --impl review_only`
- Retrieval hits and category precision per unit of cost (`metric-label-context-cards`): `esoi run label_context_cards --impl fast_model_labels`
