# Cards From Files

*ESOI records kept beside the code in `quest_ai_runner/the_intelligence/subconscious/cards_from_files`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

The AI optimally recalls the part of the person's own knowledge that each request needs.

These are the solutions of [Subconscious](../README.md).

## How the solutions fit together

Subconscious finds and prepares what a request needs before the Mind asks. Cards From Files builds topic cards from a person's own files and history, and Card Store keeps those cards in Quest, so that no card is made for every file.

## Overview diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Card Store<br/><i>card-store</i>"]
    s1["Cards From Files<br/><i>cards-from-files</i>"]
    s1 --> s0
```

## Cards From Files (`cards-from-files`)

- Entry call: `quest_ai_runner.run("cards-from-files", {'root': '.', 'files': [], 'existing': {}})`
- Selected implementation: QAR topic bootstrap (`qar_topic_bootstrap`)
- Metric: Cards per source-file group after repeated passes, for the guards that prevent growth, Copies kept out with real material kept in, by review availability, Retrieval hits and category precision per unit of cost
- Implemented by: [`evaluation/components/card_growth/impls/bootstrap_guards.py`](../../../../evaluation/components/card_growth/impls/bootstrap_guards.py), [`evaluation/components/card_growth/impls/no_cleanup.py`](../../../../evaluation/components/card_growth/impls/no_cleanup.py), [`evaluation/components/choose_what_to_index/broken/index_everything.py`](../../../../evaluation/components/choose_what_to_index/broken/index_everything.py), [`evaluation/components/choose_what_to_index/impls/review_only.py`](../../../../evaluation/components/choose_what_to_index/impls/review_only.py), [`evaluation/components/choose_what_to_index/impls/risk_gate.py`](../../../../evaluation/components/choose_what_to_index/impls/risk_gate.py), [`evaluation/components/label_context_cards/impls/path_rules.py`](../../../../evaluation/components/label_context_cards/impls/path_rules.py), [`quest_ai_runner/the_body/configuration/__init__.py`](../../../the_body/configuration/__init__.py), [`quest_ai_runner/the_body/configuration/acp_deep_runner_config.py`](../../../the_body/configuration/acp_deep_runner_config.py) and 78 more

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Fast-tier model writes metadata and categories (today) | `cards-from-files:fast_model_labels` | [`quest_ai_runner/the_intelligence/subconscious/cards_from_files/card_metadata_generator.py`](card_metadata_generator.py) |  |
| QAR topic bootstrap | `cards-from-files:qar_topic_bootstrap` | [`quest_ai_runner/the_intelligence/subconscious/cards_from_files/qar_topic_bootstrap.py`](qar_topic_bootstrap.py) | selected |

## Metrics

- Cards per source-file group after repeated passes, for the guards that prevent growth (`metric-bound-card-growth`): leader `bootstrap_guards`, 12 of 12 cases
- Copies kept out with real material kept in, by review availability (`metric-choose-what-to-index`): leader `risk_gate`, 16 of 16 cases
- Retrieval hits and category precision per unit of cost (`metric-label-context-cards`): leader `fast_model_labels_fast`, 10 of 11 cases

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Subconscious](../README.md)
- Repository: [top README](../../../../README.md)
