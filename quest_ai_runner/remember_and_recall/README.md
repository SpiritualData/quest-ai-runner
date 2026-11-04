# Remembering and recalling the person's own knowledge

A request recalls the part of the person's own knowledge its goal needs, from cards that are current with their sources, found by meaning and proportionate in size, and never from anyone else's.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `remember-and-recall:recency_window` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: The cards a person's work leaves behind are copies of what lives elsewhere, so the store itself raises questions the parent solution does not ask: when a card has drifted from its source file, how an assistant keeps serving the last known listing while a backend is down and replays the writes that failed, how a store is kept proportionate to its sources instead of inflating with copies from a backups folder, which folders a corpus may index at all, and how a card is found by meaning when the person asks in other words. Each has alternatives that can be measured against each other.

## How the solutions fit together

remember-and-recall recalls the cards that match a request through card-store. card-store keeps the cards through persist-cards, reports stale ones through detect-stale-cards and holds their growth in check through bound-card-growth. The area serves the last known listing while a backend is down with cache-cards. Cards are found by meaning with index-by-meaning. Each card gets its labels from label-context-cards. An inflated store is shrunk with repair-a-card-store. repair-a-card-store decides which sources deserve a card by asking choose-what-to-index before it rewrites anything.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Context card persistence backend<br/>persist-cards"]
    s1["Source-change staleness detector<br/>detect-stale-cards"]
    s2["Card store growth prevention<br/>bound-card-growth"]
    s3["Card store repair<br/>repair-a-card-store"]
    s4["Index scope decision: which folders and...<br/>choose-what-to-index"]
    s5["QAR card repository as the hub card...<br/>card-store"]
    s0 -->|"Load-all returns a mapping of id to card, read returns a..."| s5
    s1 -->|"Whether the file is stale, and the stale card list for the..."| s5
    s2 -->|"Cards written only under an id made of their origin; a..."| s5
    s4 -->|"A FolderGate: the set of corpus-relative folders no card..."| s3
```

## Solutions at a glance

| Solution | Entry call | Selected implementation | Metric |
|---|---|---|---|
| `persist-cards` | `quest_ai_runner.run("persist-cards", 'card-1')` | `quest_api` | Card repository conformance across backends |
| `index-by-meaning` | no entry declared | `qdrant` | Semantic recall and query latency on the synthetic handbook |
| `detect-stale-cards` | no entry declared | `store_sha256` | Staleness detection precision and recall over mutation cases |
| `cache-cards` | no entry declared | `file` | Outage resilience of the card repository |
| `bound-card-growth` | no entry declared | `bootstrap_guards` | Cards per source-file group after repeated passes, for the guards that prevent growth |
| `repair-a-card-store` | no entry declared | `repository_tidy` | Cards per source-file group and duplicate share after a repair of an inflated store |
| `choose-what-to-index` | no entry declared | `risk_gate` | Copies kept out with real material kept in, by review availability |
| `label-context-cards` | no entry declared | `fast_model_labels` | Retrieval hits and category precision per unit of cost |
| `card-store` | `quest_ai_runner.run("card-store", 'rust release')` | `quest-api-cards` | none yet |

## Problems and ideals

**Problems**

- `prob-context-drifts-from-its-source`: Context cards drift from the sources they describe: a file changes or disappears and the card still says the old thing, and when a card backend is unreachable the AI loses its cards or silently drops...
- `prob-relevant-card-not-found`: The card or passage that would answer a question exists, but it is worded differently from the question, so keyword search does not surface it and the answer is built without it.
- `prob-card-store-inflates`: A card store grows far past the distinct content of its corpus.
- `prob-index-walks-into-copies`: A corpus's walk and its card sources read folders that are not its knowledge, and the worst of them look harmless by file count.

**Ideals**

- `ideal-current-knowledge`, Current Knowledge: What the AI reads still matches the source it came from, and outages are loud but harmless.
- `ideal-findable-knowledge`, Findable Knowledge: The knowledge that bears on a question is found whatever words it uses.
- `ideal-proportionate-store`, Proportionate Card Store: A store holds one card per distinct topic of its corpus, so its size follows the corpus's distinct content and never grows by re-importing what it already has.
- `ideal-proportionate-index-scope`, Proportionate Index Scope: The system itself decides, proportionately and without a person, whether a large or high-risk folder is worth looking into: a cheap call per folder, never per file, and it fails closed only where the...

## Metrics and their leaders

- `metric-persist-cards`, Card repository conformance across backends: leader `filesystem`, 14 of 14 cases
- `metric-index-by-meaning`, Semantic recall and query latency on the synthetic handbook: leader `memory_cosine`, 16 of 17 cases
- `metric-detect-stale-cards`, Staleness detection precision and recall over mutation cases: leader `store_sha256`, 5 of 6 cases
- `metric-cache-cards`, Outage resilience of the card repository: leader `file_cache`, 12 of 12 cases
- `metric-bound-card-growth`, Cards per source-file group after repeated passes, for the guards that prevent growth: leader `bootstrap_guards`, 12 of 12 cases
- `metric-repair-a-card-store`, Cards per source-file group and duplicate share after a repair of an inflated store: leader `bootstrap_guards`, 12 of 12 cases
- `metric-choose-what-to-index`, Copies kept out with real material kept in, by review availability: leader `risk_gate`, 16 of 16 cases
- `metric-label-context-cards`, Retrieval hits and category precision per unit of cost: leader `fast_model_labels_fast`, 10 of 11 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- `prob-context-drifts-from-its-source`: Context cards drift from the sources they describe: a file changes or disappears and the card still says the old thing, and when a card backend is unreachable the AI loses its cards or silently drops writes.
- `prob-relevant-card-not-found`: The card or passage that would answer a question exists, but it is worded differently from the question, so keyword search does not surface it and the answer is built without it.
- `prob-card-store-inflates`: A card store grows far past the distinct content of its corpus. In copy mode a store nested in another store's corpus copied that store's cards under a namespaced id on every bootstrap pass, copies of copies were kept, and each pass stacked another namespace, so two real stores reached 8,557 and 32,070 cards on 2026-10-05 for corpora that need hundreds, and a search over them took minutes. A terminal chat log of 2026-10-21 reported a vector seed of 142,781 items, a figure not yet explained: the same evening both live stores held about 4.4k cards (the chat log's line 'Card store has 4380 cards').
- `prob-index-walks-into-copies`: A corpus's walk and its card sources read folders that are not its knowledge, and the worst of them look harmless by file count. A machine's daily backup snapshots, one dated folder per day and each a copy of the whole tree, each carried the card store the tree had that day: one backups folder held 155 stores with 760,608 cards, and any corpus rooted above it would have walked, imported and mounted every one of them. Earlier, on one real corpus of 81,465 indexable files, most of the files were a single vendored tree present twice and generated data. Before the risk gate, the folder review that decides this ran only above a 2,000-file floor and failed open: with no model, the review switched off or a failed call, every folder was read.

### Ideals

- `ideal-current-knowledge`, Current Knowledge: What the AI reads still matches the source it came from, and outages are loud but harmless.
- `ideal-findable-knowledge`, Findable Knowledge: The knowledge that bears on a question is found whatever words it uses.
- `ideal-proportionate-store`, Proportionate Card Store: A store holds one card per distinct topic of its corpus, so its size follows the corpus's distinct content and never grows by re-importing what it already has.
- `ideal-proportionate-index-scope`, Proportionate Index Scope: The system itself decides, proportionately and without a person, whether a large or high-risk folder is worth looking into: a cheap call per folder, never per file, and it fails closed only where the risk is real.

### Solutions

| Solution | Name | Selected | Metric |
|---|---|---|---|
| `persist-cards` | Context card persistence backend | quest_api | Card repository conformance across backends |
| `index-by-meaning` | Semantic vector index | qdrant | Semantic recall and query latency on the synthetic handbook |
| `detect-stale-cards` | Source-change staleness detector | store_sha256 | Staleness detection precision and recall over mutation cases |
| `cache-cards` | Offline card cache and write outbox | file | Outage resilience of the card repository |
| `bound-card-growth` | Card store growth prevention | bootstrap_guards | Cards per source-file group after repeated passes, for the guards that prevent growth |
| `repair-a-card-store` | Card store repair | repository_tidy | Cards per source-file group and duplicate share after a repair of an inflated store |
| `choose-what-to-index` | Index scope decision: which folders and card stores a corpus may read | risk_gate | Copies kept out with real material kept in, by review availability |
| `label-context-cards` | Card categories and metadata | fast_model_labels | Retrieval hits and category precision per unit of cost |
| `card-store` | QAR card repository as the hub card store | quest-api-cards | none yet |

### Implementations

| Implementation | Name | Where | Sub-intent |
|---|---|---|---|
| `persist-cards:filesystem` | Atomic JSON file repository | [quest_ai_runner/config_assembler.py](../config_assembler.py) |  |
| `persist-cards:qdrant` | Vector database repository | [quest_ai_runner/remember_and_recall/qdrant_card_repository.py](qdrant_card_repository.py) |  |
| `persist-cards:quest_api` | Hosted card API repository | [quest_ai_runner/adapters/quest_api_card_repository.py](../adapters/quest_api_card_repository.py) |  |
| `index-by-meaning:qdrant` | Qdrant-backed vector store | [quest_ai_runner/remember_and_recall/qdrant_vector_store.py](qdrant_vector_store.py) |  |
| `detect-stale-cards:store_sha256` | Content-hash fingerprint check | [evaluation/components/staleness/impls/store_sha256.py](../../evaluation/components/staleness/impls/store_sha256.py) |  |
| `detect-stale-cards:mtime_size` | Timestamp-and-size baseline | [evaluation/components/staleness/impls/mtime_size.py](../../evaluation/components/staleness/impls/mtime_size.py) |  |
| `cache-cards:null` | Card repository with a cache seam and a null cache | [quest_ai_runner/adapters/quest_api_card_repository.py](../adapters/quest_api_card_repository.py) |  |
| `cache-cards:file` | Durable file cache | [quest_ai_runner/remember_and_recall/card_cache_file.py](card_cache_file.py) |  |
| `cache-cards:null_cache` | No-op cache (shipped default of the seam) | [evaluation/components/cache_cards/impls/null_cache.py](../../evaluation/components/cache_cards/impls/null_cache.py) |  |
| `cache-cards:memory_cache` | In-process memory cache | [evaluation/components/cache_cards/impls/memory_cache.py](../../evaluation/components/cache_cards/impls/memory_cache.py) |  |
| `cache-cards:file_cache` | Durable file cache under outage scenarios | [evaluation/components/cache_cards/impls/file_cache.py](../../evaluation/components/cache_cards/impls/file_cache.py) |  |
| `repair-a-card-store:quarantine_tool` | Card quarantine tool | [evaluation/components/card_growth/impls/quarantine_tool.py](../../evaluation/components/card_growth/impls/quarantine_tool.py) |  |
| `bound-card-growth:no_cleanup` | Copy mode with no guard | [evaluation/components/card_growth/impls/no_cleanup.py](../../evaluation/components/card_growth/impls/no_cleanup.py) |  |
| `bound-card-growth:bootstrap_guards` | Bootstrap guards against re-import | [evaluation/components/card_growth/impls/bootstrap_guards.py](../../evaluation/components/card_growth/impls/bootstrap_guards.py) |  |
| `choose-what-to-index:risk_gate` | Risk-gated folder review | [evaluation/components/choose_what_to_index/impls/risk_gate.py](../../evaluation/components/choose_what_to_index/impls/risk_gate.py) |  |
| `choose-what-to-index:review_only` | Model-judged folder review before the gate | [evaluation/components/choose_what_to_index/impls/review_only.py](../../evaluation/components/choose_what_to_index/impls/review_only.py) |  |
| `choose-what-to-index:index_everything` | Index everything (broken baseline) | [evaluation/components/choose_what_to_index/broken/index_everything.py](../../evaluation/components/choose_what_to_index/broken/index_everything.py) |  |
| `repair-a-card-store:repository_tidy` | Repository tidy | [quest_ai_runner/adapters/card_store_tidy.py](../adapters/card_store_tidy.py) |  |
| `repair-a-card-store:local_folder_tidy` | Tidy at open of a local folder | [quest_ai_runner/adapters/context_store_heal.py](../adapters/context_store_heal.py) |  |
| `repair-a-card-store:no_cleanup` | Store as copy mode leaves it | [evaluation/components/card_growth/impls/no_cleanup.py](../../evaluation/components/card_growth/impls/no_cleanup.py) |  |
| `label-context-cards:fast_model_labels` | Fast-tier model writes metadata and categories (today) | [quest_ai_runner/remember_and_recall/card_metadata_generator.py](card_metadata_generator.py) |  |
| `label-context-cards:path_rules` | Path and title rules, no model call (floor, proposed) | [evaluation/components/label_context_cards/impls/path_rules.py](../../evaluation/components/label_context_cards/impls/path_rules.py) |  |
| `card-store:qar-card-repository` | qar-card-repository | [quest_ai_runner/remember_and_recall/hub_card_store.py](hub_card_store.py) |  |
| `card-store:quest-api-cards` | Quest backend card API from the environment | [quest_ai_runner/remember_and_recall/hub_card_store.py](hub_card_store.py) |  |

### Metrics

- `metric-persist-cards`, Card repository conformance across backends: `esoi run persist_cards --impl quest_api`
- `metric-index-by-meaning`, Semantic recall and query latency on the synthetic handbook: `esoi run vector_recall --impl qdrant_embedded`
- `metric-detect-stale-cards`, Staleness detection precision and recall over mutation cases: `esoi run staleness --impl store_sha256`
- `metric-cache-cards`, Outage resilience of the card repository: `esoi run cache_cards --impl file_cache`
- `metric-bound-card-growth`, Cards per source-file group after repeated passes, for the guards that prevent growth: `esoi run card_growth --impl bootstrap_guards; esoi run card_store_growth --impl live_store; esoi run card_growth --impl no_cleanup`
- `metric-repair-a-card-store`, Cards per source-file group and duplicate share after a repair of an inflated store: `esoi run card_growth --impl repository_tidy; esoi run card_growth --impl quarantine_tool; esoi run card_growth --impl no_cleanup`
- `metric-choose-what-to-index`, Copies kept out with real material kept in, by review availability: `esoi run choose_what_to_index --impl risk_gate; esoi run choose_what_to_index --impl review_only`
- `metric-label-context-cards`, Retrieval hits and category precision per unit of cost: `esoi run label_context_cards --impl fast_model_labels`
