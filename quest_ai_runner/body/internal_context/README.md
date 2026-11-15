# Quest Context

**Vision statement:** The AI knows each person's own Quest, nobody else's.

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Body](../README.md) > Quest Context

This sub-intent arises as the implementation `quest-context:quest` of its parent intent [`body`](../README.md). Opportunities: A person's Quest holds more than any one request can read, and each piece belongs to one person, so how it is fed, stored and kept apart is a question the parent solution leaves open.

## How the solutions fit together

Quest Context knows each person's own Quest and nobody else's. Card Store keeps the person's context cards where they must live. Persist Cards writes them to a folder, a vector database or Quest itself. Cache Cards serves the last listing and holds failed writes while a backend is down. Feed Context Sources tells the assistant what is new in a quest since it last looked.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- Card Store (solution with no page of its own, its implementations are in this intent's records)
- Feed Context Sources (solution with no page of its own, its implementations are in this intent's records): Help an assistant catch up on a work item where people left off, so it answers what is new and open and not what it has already seen.
- Cache Cards (solution with no page of its own, its implementations are in this intent's records): Keep an assistant working through a card backend outage by serving the last known listing and holding failed writes for replay.
- Persist Cards (solution with no page of its own, its implementations are in this intent's records): Keeps context cards safe and retrievable wherever a person needs them to live, from a local folder to the Quest backend.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Card Store | `card-store` | `quest_ai_runner.run("card-store", 'rust release')` | Quest backend card API from the environment (`quest-api-cards`) | Staleness detection precision and recall over mutation cases, Cards per source-file group and duplicate share after a repair of an inflated store |
| Feed Context Sources | `feed-context-sources` | `quest_ai_runner.run("feed-context-sources", {'quest_id': 'quest-id'})` | The update engine over its built-in channels (`quest_channels`) | Update-offering correctness over synthetic quest channels |
| Cache Cards | `cache-cards` | `quest_ai_runner.run("cache-cards")` | Durable file cache (`file`) | Outage resilience of the card repository |
| Persist Cards | `persist-cards` | `quest_ai_runner.run("persist-cards", 'card-1')` | Hosted card API repository (`quest_api`) | Card repository conformance across backends |

### Top implementations of Card Store

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Content-hash fingerprint check (`store_sha256`) | 83% (5 of 6 cases) | free |  |
| 2 | Timestamp-and-size baseline (`mtime_size`) | 67% (4 of 6 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Staleness detection precision and recall over mutation cases. precision and recall over synthetic mutation cases, zero model calls An edit that restores the mtime and size is trusted as unchanged, a deliberate speed trade for the mtime fast path.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Feed Context Sources

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | The update engine over its built-in channels (`quest_channels`) | 100% (20 of 20 cases) | free | selected |
| 2 | Live engine over fake quest channels (`update_engine`) | 100% (20 of 20 cases) | free |  |

**How it is scored:** Update-offering correctness over synthetic quest channels. synthetic quests across the quest channels (notes, goal updates, decisions): the exact set of items offered and of open asks (own words offered, assistant echoes and unattributed notes left out, answered and already-seen items not repeated, age and count bounds) Read in this direction: raise the share of new human-authored items offered exactly once; lower the count of repeated, already-answered, self-echoed or unattributed items offered.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Cache Cards

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Durable file cache under outage scenarios (`file_cache`) | 100% (12 of 12 cases) | free |  |
| 2 | In-process memory cache (`memory_cache`) | 100% (12 of 12 cases) | free |  |
| 3 | No-op cache (shipped default of the seam) (`null_cache`) | 50% (6 of 12 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Outage resilience of the card repository. Offline cases on the live QuestApiCardRepository against an in-process card API that can go down: caller-visible behaviour, stored listing, failed-write outbox, cold start during an outage The shipped null cache and an in-process double lose the listing and the outbox, and the file cache is single-host, so a card backend shared by several hosts has no shared cache.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Persist Cards

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Atomic JSON file repository (`filesystem`) | 100% (14 of 14 cases) | free |  |
| 2 | Vector database repository (`qdrant`) | 100% (14 of 14 cases) | free |  |
| 3 | Hosted card API repository (`quest_api`) | 100% (14 of 14 cases) | free | selected |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Card repository conformance across backends. One conformance case set through the CardRepository port on the filesystem, embedded Qdrant and Quest API (fake card API) backends: roundtrip, overwrite, no loss at volume and across pages, no duplicate, delete (also after restart), concurrent writers, restart, change stamp, tolerance.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Solutions from a library

- [Card Store](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/body/README.md) (`card-store`): A small store that keeps cards, the hub's own memory unit, and finds them again by words or a tag. Adopted from the library `hub`.
- [Internal Context](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/body/README.md) (`feed-context-sources`): Declares where the system's data lives and which World tool reads it. Specializes the library `hub`.
- [Card Store](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/body/README.md) (`cache-cards`): A small store that keeps cards, the hub's own memory unit, and finds them again by words or a tag. Specializes the library `hub`.
- [Card Store](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/body/README.md) (`persist-cards`): A small store that keeps cards, the hub's own memory unit, and finds them again by words or a tag. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Private Data Crossing Scope (`prob-private-data-crosses-scope`): Data from one user, team or quest reaches another one's prompt or store, through retrieval, caches, conversation recall, shared stores or the sources fed to the AI.

**Ideals**

- Contained Scope (`ideal-contained-scope`): Data stays inside the user, team and quest it belongs to, on every read and write path.

## Where to go next

- Parent intent: [Quest Body](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/body/internal_context/card_store](card_store/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv)

---

## The full record

### Problems

- Private Data Crossing Scope (`prob-private-data-crosses-scope`): Data from one user, team or quest reaches another one's prompt or store, through retrieval, caches, conversation recall, shared stores or the sources fed to the AI.

### Ideals

- Contained Scope (`ideal-contained-scope`): Data stays inside the user, team and quest it belongs to, on every read and write path.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Card Store | `card-store` | Quest backend card API from the environment (`quest-api-cards`) | Staleness detection precision and recall over mutation cases, Cards per source-file group and duplicate share after a repair of an inflated store |
| Feed Context Sources | `feed-context-sources` | The update engine over its built-in channels (`quest_channels`) | Update-offering correctness over synthetic quest channels |
| Cache Cards | `cache-cards` | Durable file cache (`file`) | Outage resilience of the card repository |
| Persist Cards | `persist-cards` | Hosted card API repository (`quest_api`) | Card repository conformance across backends |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Source interface with shared channel flags | `feed-context-sources:base` | [quest_ai_runner/body/intake_sources/context_sources_base.py](../intake_sources/context_sources_base.py) |  |
| Live engine over fake quest channels | `feed-context-sources:update_engine` | [evaluation/components/feed_context_sources/impls/update_engine.py](../../../evaluation/components/feed_context_sources/impls/update_engine.py) |  |
| The update engine over its built-in channels | `feed-context-sources:quest_channels` | [quest_ai_runner/body/intake_sources/context_updates.py](../intake_sources/context_updates.py) |  |
| Atomic JSON file repository | `persist-cards:filesystem` | [quest_ai_runner/body/configuration/config_assembler.py](../configuration/config_assembler.py) |  |
| Content-hash fingerprint check | `card-store:store_sha256` | [evaluation/components/staleness/impls/store_sha256.py](../../../evaluation/components/staleness/impls/store_sha256.py) |  |
| Timestamp-and-size baseline | `card-store:mtime_size` | [evaluation/components/staleness/impls/mtime_size.py](../../../evaluation/components/staleness/impls/mtime_size.py) |  |
| No-op cache (shipped default of the seam) | `cache-cards:null_cache` | [evaluation/components/cache_cards/impls/null_cache.py](../../../evaluation/components/cache_cards/impls/null_cache.py) |  |
| In-process memory cache | `cache-cards:memory_cache` | [evaluation/components/cache_cards/impls/memory_cache.py](../../../evaluation/components/cache_cards/impls/memory_cache.py) |  |
| Durable file cache under outage scenarios | `cache-cards:file_cache` | [evaluation/components/cache_cards/impls/file_cache.py](../../../evaluation/components/cache_cards/impls/file_cache.py) |  |
| Card quarantine tool | `card-store:quarantine_tool` | [evaluation/components/card_growth/impls/quarantine_tool.py](../../../evaluation/components/card_growth/impls/quarantine_tool.py) |  |
| Store as copy mode leaves it | `card-store:no_cleanup` | [evaluation/components/card_growth/impls/no_cleanup.py](../../../evaluation/components/card_growth/impls/no_cleanup.py) |  |
| Vector database repository | `persist-cards:qdrant` | [quest_ai_runner/body/internal_context/card_store/qdrant_card_repository.py](card_store/qdrant_card_repository.py) |  |
| Hosted card API repository | `persist-cards:quest_api` | [quest_ai_runner/body/internal_context/card_store/quest_api_card_repository.py](card_store/quest_api_card_repository.py) |  |
| Card repository with a cache seam and a null cache | `cache-cards:null` | [quest_ai_runner/body/internal_context/card_store/quest_api_card_repository.py](card_store/quest_api_card_repository.py) |  |
| Durable file cache | `cache-cards:file` | [quest_ai_runner/body/internal_context/card_store/card_cache_file.py](card_store/card_cache_file.py) |  |
| Repository tidy | `card-store:repository_tidy` | [quest_ai_runner/body/internal_context/card_store/card_store_tidy.py](card_store/card_store_tidy.py) |  |
| Tidy at open of a local folder | `card-store:local_folder_tidy` | [quest_ai_runner/body/internal_context/card_store/context_store_heal.py](card_store/context_store_heal.py) |  |
| QAR card repository | `card-store:qar-card-repository` | [quest_ai_runner/body/internal_context/card_store/hub_card_store.py](card_store/hub_card_store.py) |  |
| Quest backend card API from the environment | `card-store:quest-api-cards` | [quest_ai_runner/body/internal_context/card_store/hub_card_store.py](card_store/hub_card_store.py) |  |


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
