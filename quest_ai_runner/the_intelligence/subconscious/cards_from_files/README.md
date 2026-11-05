# cards_from_files

*ESOI records kept beside the code in `quest_ai_runner/the_intelligence/subconscious/cards_from_files`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Fast-tier model writes metadata and categories (today) | `cards-from-files:fast_model_labels` | [`quest_ai_runner/the_intelligence/subconscious/cards_from_files/card_metadata_generator.py`](card_metadata_generator.py) |  |
| QAR topic bootstrap | `cards-from-files:qar_topic_bootstrap` | [`quest_ai_runner/the_intelligence/subconscious/cards_from_files/qar_topic_bootstrap.py`](qar_topic_bootstrap.py) |  |

## Metrics

- Cards per source-file group after repeated passes, for the guards that prevent growth (`metric-bound-card-growth`): leader `bootstrap_guards`, 12 of 12 cases
- Copies kept out with real material kept in, by review availability (`metric-choose-what-to-index`): leader `risk_gate`, 16 of 16 cases
- Retrieval hits and category precision per unit of cost (`metric-label-context-cards`): leader `fast_model_labels_fast`, 10 of 11 cases

## Where to go next

- Up: [quest_ai_runner/the_intelligence](../../README.md)
- Intent: [Subconscious](../README.md)
- Repository: [top README](../../../../README.md)
