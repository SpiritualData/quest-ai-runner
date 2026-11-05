# Quest Self

**Vision statement:** Each Quest user meets one whole assistant.

Breadcrumb: [Top](../../esoi/README.md) > Quest Self

This sub-intent arises as the implementation `quest-self:library` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A person's day with their assistant is judged as they meet it, which no single part can show, so the whole assistant has its own measure of how capable it feels from the user's seat.

## How the solutions fit together

Quest Self keeps every part of the assistant one specified whole, and Integrity is how it does that. Integrity checks that each part is a specified solution with a measured best implementation, so the package, its front door and its configuration stay one measured whole.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- [Integrity](integrity/README.md) (sub-intent): Every part of the runner is a specified solution whose best implementation runs as the state of the art moves.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Integrity | `system-integrity` | `quest_ai_runner.run("system-integrity", ['examplevendor'])` | Apply the evolvable-structure library (`structure-library`) | Parts selected by measurement, Hub of hubs, Hub of hubs, Correct selection decisions over the evolve-by-metric cases, Intents whose solutions one paragraph and one diagram explain as the code builds them, Public surface conformance on fixture inputs, Example runnability on an empty environment, Configuration precedence and validation correctness, Command-line parsing, refusal and output correctness, Documentation coverage and voice conformance, Correct terminal helper verdicts within the wait a person accepts, Offline test collection, Secret detection and placeholder tolerance, Right solution missed without the tag |

## Solutions from a library

- [Integrity](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_self/README.md) (`system-integrity`): Keeps the hub generic: claims match evidence, records match code, and only failures that are hidden are reported. Adopted from the library `hub`.

## Metrics and their leaders

- Passing chat turns per dollar (`QME1`): leader `reference_replies`, 331 of 331 cases
- Delegated tasks meeting their standard (`QME2`)
- Exact and consented changes (`QME3`)
- Data kept in scope (`QME4`)
- Engine reuse (`QME5`)
- Measured coverage (`QME6`)
- QAR overall score (`metric-qar-overall`): leader `qar_overall.broken.latest_row_any_age:build`, 0 of 9 cases
- Justified share of model spend (`metric-justified-model-spend`): leader `spec_registry`, 15 of 15 cases
- Justified model spend (`QME8`): leader `spec_registry`, 15 of 15 cases
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): leader `qar_usage`, 38 of 39 cases

## Where to go next

- Sub-intent: [Integrity](integrity/README.md)
- Parent intent: [Quest AI Runner](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Integrity | `system-integrity` | Apply the evolvable-structure library (`structure-library`) | Parts selected by measurement, Hub of hubs, Hub of hubs, Correct selection decisions over the evolve-by-metric cases, Intents whose solutions one paragraph and one diagram explain as the code builds them, Public surface conformance on fixture inputs, Example runnability on an empty environment, Configuration precedence and validation correctness, Command-line parsing, refusal and output correctness, Documentation coverage and voice conformance, Correct terminal helper verdicts within the wait a person accepts, Offline test collection, Secret detection and placeholder tolerance, Right solution missed without the tag |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Package root with front doors | `system-integrity:__init__` | [quest_ai_runner/__init__.py](../__init__.py) |  |
| Long-lived background loop bridge | `system-integrity:async_bridge` | [quest_ai_runner/the_consciousness/request_intake/async_bridge.py](../the_consciousness/request_intake/async_bridge.py) |  |
| Live package-surface driver | `system-integrity:live_surface` | [evaluation/components/expose_the_public_api/impls/live_surface.py](../../evaluation/components/expose_the_public_api/impls/live_surface.py) |  |
| Minimal lane example | `system-integrity:minimal_lane` | [examples/minimal_lane.py](../../examples/minimal_lane.py) |  |
| Custom consumer configuration example | `system-integrity:custom_consumer` | [examples/custom_consumer.py](../../examples/custom_consumer.py) |  |
| Example check harness | `system-integrity:live_examples` | [evaluation/components/learn_by_example/impls/live_examples.py](../../evaluation/components/learn_by_example/impls/live_examples.py) |  |
| Configuration dataclass and file entry point | `system-integrity:config` | [quest_ai_runner/the_body/configuration/config.py](../the_body/configuration/config.py) |  |
| Strict TOML file loader | `system-integrity:config_file` | [quest_ai_runner/the_body/configuration/config_file.py](../the_body/configuration/config_file.py) |  |
| Environment and env-file layering | `system-integrity:config_environment` | [quest_ai_runner/the_body/configuration/config_environment.py](../the_body/configuration/config_environment.py) |  |
| Environment-backed tuning settings | `system-integrity:config_core_settings` | [quest_ai_runner/the_body/configuration/config_core_settings.py](../the_body/configuration/config_core_settings.py) |  |
| Injected core settings holder | `system-integrity:runtime_settings` | [quest_ai_runner/the_body/configuration/runtime_settings.py](../the_body/configuration/runtime_settings.py) |  |
| Layered configuration evaluation arm | `system-integrity:qar_config` | [evaluation/components/configure_a_lane/impls/qar_config.py](../../evaluation/components/configure_a_lane/impls/qar_config.py) |  |
| Command dispatcher | `system-integrity:cli` | [quest_ai_runner/the_consciousness/terminal/cli.py](../the_consciousness/terminal/cli.py) |  |
| Declarative argument tree | `system-integrity:cli_parser` | [quest_ai_runner/the_consciousness/terminal/cli_parser.py](../the_consciousness/terminal/cli_parser.py) |  |
| Quest API subcommands | `system-integrity:cli_quest` | [quest_ai_runner/the_consciousness/terminal/cli_quest.py](../the_consciousness/terminal/cli_quest.py) |  |
| Card-store maintenance commands | `system-integrity:store_cli` | [quest_ai_runner/the_intelligence/subconscious/cards_from_files/store_cli.py](../the_intelligence/subconscious/cards_from_files/store_cli.py) |  |
| Tool catalog shell entry | `system-integrity:__main__` | [quest_ai_runner/the_consciousness/terminal/__main__.py](../the_consciousness/terminal/__main__.py) |  |
| Real-entry-point evaluation arm | `system-integrity:qar_main` | [evaluation/components/operate_from_a_terminal/impls/qar_main.py](../../evaluation/components/operate_from_a_terminal/impls/qar_main.py) |  |
| Documentation index | `system-integrity:README.md` | [docs/README.md](../../docs/README.md) |  |
| Shipped-documents evaluation arm | `system-integrity:shipped_docs` | [evaluation/components/explain_qar/impls/shipped_docs.py](../../evaluation/components/explain_qar/impls/shipped_docs.py) |  |
| Rules only, no model call (floor, proposed) | `system-integrity:rules_only` |  |  |
| Apply the evolvable-structure library | `system-integrity:structure-library` | [quest_ai_runner/the_self/area_integrity.py](area_integrity.py) | [integrity](integrity/README.md) |
| Offline suite configuration | `system-integrity:conftest` | [tests/conftest.py](../../tests/conftest.py) |  |
| Collection check by test file | `system-integrity:collect_by_file` | [evaluation/components/verify_offline/impls/collect_by_file.py](../../evaluation/components/verify_offline/impls/collect_by_file.py) |  |
| Planted-secret evaluation arm | `system-integrity:live_scanner` | [evaluation/components/keep_secrets_out/impls/live_scanner.py](../../evaluation/components/keep_secrets_out/impls/live_scanner.py) |  |
| Problem-linked tag list with a check rule and a lookup | `system-integrity:tag_list_with_check` | [scripts/esoi_tags.py](../../scripts/esoi_tags.py) |  |

### Metrics

- Passing chat turns per dollar (`QME1`): `esoi run chat_quality --impl dev_quest_chat --sample 10 --max-calls 300`
- Delegated tasks meeting their standard (`QME2`): `none`
- Exact and consented changes (`QME3`): `none`
- Data kept in scope (`QME4`): `none`
- Engine reuse (`QME5`): `none`
- Measured coverage (`QME6`): `none`
- QAR overall score (`metric-qar-overall`): `esoi run qar_overall --impl recorded_rows`
- Justified share of model spend (`metric-justified-model-spend`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Justified model spend (`QME8`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): `esoi run track_usage_and_cost --impl qar_usage`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
