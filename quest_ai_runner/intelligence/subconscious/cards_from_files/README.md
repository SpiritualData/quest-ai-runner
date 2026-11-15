# cards_from_files

Breadcrumb: [Top](../../../../esoi/README.md) > [Intelligence](../../README.md) > [Subconscious](../README.md) > cards_from_files

Part of [Subconscious](../README.md)

### Top implementations of Cards From Files

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Bootstrap guards against re-import (`bootstrap_guards`) | 100% (12 of 12 cases) | free |  |
| 2 | `repository_tidy` | 100% (12 of 12 cases) | free |  |
| 3 | `quarantine_tool` | 83% (10 of 12 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

2 more compared; the full list is in the results log.

**How it is scored:** Cards per source-file group after repeated passes, for the guards that prevent growth. Measures, offline, whether a card store stays at one card per distinct topic of its source files as the runner builds and opens it.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Fast-tier model writes metadata and categories (today) | `cards-from-files:fast_model_labels` | On card creation or refresh a fast-tier model writes the metadata, and after a run it places modified files into cards. | [`quest_ai_runner/intelligence/subconscious/cards_from_files/card_metadata_generator.py`](card_metadata_generator.py) |  |
| QAR topic bootstrap | `cards-from-files:qar_topic_bootstrap` | The topic cards QAR's bootstrap proposes for files no card covers: areas discovered, topics written per area, duplicates folded into the cards ``existing`` holds. Returns ``{id, name, keywords, summary, files}`` (files as paths); the bootstrap stamps, limits and writes them. With no model nothing can be identified and the answer is an empty list. | [`quest_ai_runner/intelligence/subconscious/cards_from_files/qar_topic_bootstrap.py`](qar_topic_bootstrap.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Subconscious](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
