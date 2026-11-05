# cards_from_files

Breadcrumb: [Top](../../../../esoi/README.md) > [Intelligence](../../README.md) > [Subconscious](../README.md) > cards_from_files

Part of [Subconscious](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Fast-tier model writes metadata and categories (today) | `cards-from-files:fast_model_labels` | On card creation or refresh a fast-tier model writes the metadata, and after a run it places modified files into cards. | [`quest_ai_runner/the_intelligence/subconscious/cards_from_files/card_metadata_generator.py`](card_metadata_generator.py) |  |
| QAR topic bootstrap | `cards-from-files:qar_topic_bootstrap` | The topic cards QAR's bootstrap proposes for files no card covers: areas discovered, topics written per area, duplicates folded into the cards ``existing`` holds. Returns ``{id, name, keywords, summary, files}`` (files as paths); the bootstrap stamps, limits and writes them. With no model nothing can be identified and the answer is an empty list. | [`quest_ai_runner/the_intelligence/subconscious/cards_from_files/qar_topic_bootstrap.py`](qar_topic_bootstrap.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Subconscious](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
