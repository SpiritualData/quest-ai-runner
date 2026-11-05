# Quest Requests

Every request made in Quest is taken in once by the same AI.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `quest-requests:ways_in` of its parent intent [`the_consciousness`](../README.md). Opportunities: A runner is reached from many places and each has its own failure and cost, which the parent solution measures one by one and none together: a short chat message must not pay for a long run, an image has to be described before it can ground an answer, input that arrives while a run works must be heard at the next decision, a terminal session needs helper calls that cost a fraction of a cent, and a developer needs a package that installs and runs on an empty environment, learned from documents and examples that are kept true.

## How the solutions fit together

Quest Requests takes a request in from a chat turn, a queued task or a live channel and gives every surface the same understanding of it. Planner decides where the request goes, whether to answer it, use a tool, hand it to deep work, confirm it with the person or ask for clarity, and replays a request that worked before at a fraction of the cost.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Planner | `planner` | `quest_ai_runner.run("planner", 'Add a note that the launch moves to Friday')` | The loop planner decides the route (`planner_route`) | Routing correctness over the classification cases, Fast-path hit rate and parity with the planner-only path, Accuracy of core ML tasks over labelled cases |

## Solutions from a library

- [Planner](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/mind/README.md) (`planner`): Breaks a request into pieces, each with the sub-goal it must meet and the worker category that fits it; on a replan it plans again only what failed. Adopted from the library `hub`.

## Metrics and their leaders

- Routing correctness over the classification cases (`metric-route-a-message`): leader `single_prompt`, 52 of 54 cases
- Update-offering correctness over synthetic quest channels (`metric-feed-context-sources`): leader `quest_channels`, 20 of 20 cases
- Exactly-once mid-run delivery per run, and how fast it is seen (`metric-receive-input-mid-run`): leader `in_memory`, 13 of 13 cases
- Public surface conformance on fixture inputs (`metric-expose-the-public-api`): leader `live_surface`, 101 of 101 cases
- Example runnability on an empty environment (`metric-learn-by-example`): leader `live_examples`, 29 of 37 cases
- Configuration precedence and validation correctness (`metric-configure-a-lane`): leader `qar_config`, 36 of 36 cases
- Command-line parsing, refusal and output correctness (`metric-operate-from-a-terminal`): leader `cli_commands`, 42 of 42 cases
- Documentation coverage and voice conformance (`metric-explain-qar`): leader `shipped_docs`, 466 of 469 cases
- Answer pass rate gained per image described (`metric-describe-an-attachment`): leader `skip_images`, 2 of 10 cases
- Correct terminal helper verdicts within the wait a person accepts (`metric-assist-a-terminal-session`): leader `fast_model_helpers`, 30 of 32 cases

## Where to go next

- Parent intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Planner | `planner` | The loop planner decides the route (`planner_route`) | Routing correctness over the classification cases, Fast-path hit rate and parity with the planner-only path, Accuracy of core ML tasks over labelled cases |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Full planner decision | `planner:qar_planner` | [evaluation/components/classification/impls/qar_planner.py](../../../evaluation/components/classification/impls/qar_planner.py) |  |
| Keyword rules floor | `planner:keyword_rules` | [evaluation/components/classification/impls/keyword_rules.py](../../../evaluation/components/classification/impls/keyword_rules.py) |  |
| Source interface with shared channel flags | `quest-context:base` | [quest_ai_runner/the_body/intake_sources/context_sources_base.py](../../the_body/intake_sources/context_sources_base.py) |  |
| Live engine over fake quest channels | `quest-context:update_engine` | [evaluation/components/feed_context_sources/impls/update_engine.py](../../../evaluation/components/feed_context_sources/impls/update_engine.py) |  |
| In-memory thread-safe inbox | `quest-requests:in_memory` | [quest_ai_runner/the_intelligence/mind/delegation/inbox.py](../../the_intelligence/mind/delegation/inbox.py) |  |
| Package root with front doors | `system-integrity:__init__` | [quest_ai_runner/__init__.py](../../__init__.py) |  |
| Long-lived background loop bridge | `system-integrity:async_bridge` | [quest_ai_runner/the_consciousness/request_intake/async_bridge.py](async_bridge.py) |  |
| Live package-surface driver | `system-integrity:live_surface` | [evaluation/components/expose_the_public_api/impls/live_surface.py](../../../evaluation/components/expose_the_public_api/impls/live_surface.py) |  |
| Minimal lane example | `system-integrity:minimal_lane` | [examples/minimal_lane.py](../../../examples/minimal_lane.py) |  |
| Custom consumer configuration example | `system-integrity:custom_consumer` | [examples/custom_consumer.py](../../../examples/custom_consumer.py) |  |
| Example check harness | `system-integrity:live_examples` | [evaluation/components/learn_by_example/impls/live_examples.py](../../../evaluation/components/learn_by_example/impls/live_examples.py) |  |
| Configuration dataclass and file entry point | `system-integrity:config` | [quest_ai_runner/the_body/configuration/config.py](../../the_body/configuration/config.py) |  |
| Strict TOML file loader | `system-integrity:config_file` | [quest_ai_runner/the_body/configuration/config_file.py](../../the_body/configuration/config_file.py) |  |
| Environment and env-file layering | `system-integrity:config_environment` | [quest_ai_runner/the_body/configuration/config_environment.py](../../the_body/configuration/config_environment.py) |  |
| Environment-backed tuning settings | `system-integrity:config_core_settings` | [quest_ai_runner/the_body/configuration/config_core_settings.py](../../the_body/configuration/config_core_settings.py) |  |
| Injected core settings holder | `system-integrity:runtime_settings` | [quest_ai_runner/the_body/configuration/runtime_settings.py](../../the_body/configuration/runtime_settings.py) |  |
| Layered configuration evaluation arm | `system-integrity:qar_config` | [evaluation/components/configure_a_lane/impls/qar_config.py](../../../evaluation/components/configure_a_lane/impls/qar_config.py) |  |
| Command dispatcher | `system-integrity:cli` | [quest_ai_runner/the_consciousness/terminal/cli.py](../terminal/cli.py) |  |
| Declarative argument tree | `system-integrity:cli_parser` | [quest_ai_runner/the_consciousness/terminal/cli_parser.py](../terminal/cli_parser.py) |  |
| Quest API subcommands | `system-integrity:cli_quest` | [quest_ai_runner/the_consciousness/terminal/cli_quest.py](../terminal/cli_quest.py) |  |
| Card-store maintenance commands | `system-integrity:store_cli` | [quest_ai_runner/the_intelligence/subconscious/cards_from_files/store_cli.py](../../the_intelligence/subconscious/cards_from_files/store_cli.py) |  |
| Tool catalog shell entry | `system-integrity:__main__` | [quest_ai_runner/the_consciousness/terminal/__main__.py](../terminal/__main__.py) |  |
| Real-entry-point evaluation arm | `system-integrity:qar_main` | [evaluation/components/operate_from_a_terminal/impls/qar_main.py](../../../evaluation/components/operate_from_a_terminal/impls/qar_main.py) |  |
| Documentation index | `system-integrity:README.md` | [docs/README.md](../../../docs/README.md) |  |
| Shipped-documents evaluation arm | `system-integrity:shipped_docs` | [evaluation/components/explain_qar/impls/shipped_docs.py](../../../evaluation/components/explain_qar/impls/shipped_docs.py) |  |
| One vision-model call per image (today) | `quest-requests:vision_description` | [quest_ai_runner/the_intelligence/mind/context_intake/attachments.py](../../the_intelligence/mind/context_intake/attachments.py) |  |
| No description, the image is named only (proposed) | `quest-requests:skip_images` |  |  |
| Fast-tier model calls in the terminal (today) | `system-integrity:fast_model_helpers` | [quest_ai_runner/the_body/configuration/cli_config.py](../../the_body/configuration/cli_config.py) |  |
| Rules only, no model call (floor, proposed) | `system-integrity:rules_only` |  |  |
| The loop planner decides the route | `planner:planner_route` | [quest_ai_runner/the_intelligence/mind/planner/message_routing.py](../../the_intelligence/mind/planner/message_routing.py) |  |
| The update engine over its built-in channels | `quest-context:quest_channels` | [quest_ai_runner/the_body/intake_sources/context_updates.py](../../the_body/intake_sources/context_updates.py) |  |
| The command dispatcher run in process | `system-integrity:cli_commands` | [quest_ai_runner/the_intelligence/mind/planner/area_take_in_the_world.py](../../the_intelligence/mind/planner/area_take_in_the_world.py) |  |
| The names in the package __all__ | `system-integrity:package_front_doors` | [quest_ai_runner/the_intelligence/mind/planner/area_take_in_the_world.py](../../the_intelligence/mind/planner/area_take_in_the_world.py) |  |

### Metrics

- Routing correctness over the classification cases (`metric-route-a-message`): `esoi run classification --impl qar_planner`
- Update-offering correctness over synthetic quest channels (`metric-feed-context-sources`): `esoi run feed_context_sources --impl quest_channels`
- Exactly-once mid-run delivery per run, and how fast it is seen (`metric-receive-input-mid-run`): `esoi run receive_input_mid_run --impl in_memory`
- Public surface conformance on fixture inputs (`metric-expose-the-public-api`): `esoi run expose_the_public_api --impl live_surface`
- Example runnability on an empty environment (`metric-learn-by-example`): `esoi run learn_by_example --impl live_examples`
- Configuration precedence and validation correctness (`metric-configure-a-lane`): `esoi run configure_a_lane --impl qar_config`
- Command-line parsing, refusal and output correctness (`metric-operate-from-a-terminal`): `esoi run operate_from_a_terminal --impl cli_commands`
- Documentation coverage and voice conformance (`metric-explain-qar`): `esoi run explain_qar --impl shipped_docs`
- Answer pass rate gained per image described (`metric-describe-an-attachment`): `esoi run describe_an_attachment --impl vision_description`
- Correct terminal helper verdicts within the wait a person accepts (`metric-assist-a-terminal-session`): `esoi run assist_a_terminal_session --impl fast_model_helpers_live; esoi run assist_a_terminal_session --impl fast_model_helpers`
