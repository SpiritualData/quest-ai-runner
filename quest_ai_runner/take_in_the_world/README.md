# Taking in requests from anywhere

A person or a program reaches the runner from wherever they work, by a message, a terminal, a live channel or the package itself, and is understood the same way.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `take-in-the-world:ways_in` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A runner is reached from many places and each has its own failure and cost, which the parent solution measures one by one and none together: a short chat message must not pay for a long run, an image has to be described before it can ground an answer, input that arrives while a run works must be heard at the next decision, a terminal session needs helper calls that cost a fraction of a cent, and a developer needs a package that installs and runs on an empty environment, learned from documents and examples that are kept true.

## How the solutions fit together

Request intake lets a person or a program reach the runner from wherever they work and be understood the same way, and ways_in is its arm. Cost-aware message router matches each message to the handling it needs, from a direct answer to a long run. Image description for grounding turns an image the person attached into text that can ground the answer. Incremental context-update feed brings what changed in a work item since the last turn. Conversation-keyed mid-run input inbox lets a person change direction or stop a run that is already working. Non-interactive command-line surface runs the tools from the command line with predictable output and exit codes. Terminal helper calls makes the small model calls a terminal session needs. Public package surface contract gives a program a stable package to import and configure. It hands over load_config, the front door that builds the lane's one configuration through Layered lane configuration loader. Documentation coverage and voice check documents the package through Public package surface contract so that a newcomer can learn it from complete documents. Runnable example set shows how to use the package through Public package surface contract in small examples that run on an empty environment. Layered lane configuration loader gives whoever runs a lane one configuration, built from its file and its environment in a fixed order, so every lane is set up the same way. It turns each selection the file or the environment names into the arm that runs through Implementation selection.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Public package surface contract<br/><i>expose-the-public-api</i>"]
    s1["Runnable example set<br/><i>learn-by-example</i>"]
    s2["Layered lane configuration loader<br/><i>configure-a-lane</i>"]
    s3["Documentation coverage and voice check<br/><i>explain-qar</i>"]
    s4["Implementation selection<br/><i>select-an-implementation</i>"]
    s0 -->|"A configured client or runner config, results of bridged..."| s1
    s0 -->|"A configured client or runner config, results of bridged..."| s3
    s2 -->|"A populated runner configuration object, or a..."| s0
    s4 -->|"The implementation that serves a solution, chosen by name..."| s2
```

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Cost-aware message router | `route-a-message` | `quest_ai_runner.run("route-a-message", 'Save a note that the launch moves to Friday.')` | The loop planner decides the route (`planner_route`) | Routing correctness over the classification cases |
| Incremental context-update feed | `feed-context-sources` | `quest_ai_runner.run("feed-context-sources", {'quest_id': 'quest-id'})` | The update engine over its built-in channels (`quest_channels`) | Update-offering correctness over synthetic quest channels |
| Conversation-keyed mid-run input inbox | `receive-input-mid-run` | `quest_ai_runner.run("receive-input-mid-run", 'conversation-1')` | In-memory thread-safe inbox (`in_memory`) | Exactly-once mid-run delivery per run, and how fast it is seen |
| Public package surface contract | `expose-the-public-api` | `quest_ai_runner.run("expose-the-public-api", 'run')` | The names in the package __all__ (`package_front_doors`) | Public surface conformance on fixture inputs |
| Runnable example set | `learn-by-example` | no entry declared | Example check harness (`live_examples`) | Example runnability on an empty environment |
| Layered lane configuration loader | `configure-a-lane` | no entry declared | Layered configuration evaluation arm (`qar_config`) | Configuration precedence and validation correctness |
| Non-interactive command-line surface | `operate-from-a-terminal` | `quest_ai_runner.run("operate-from-a-terminal", ['--help'])` | The command dispatcher run in process (`cli_commands`) | Command-line parsing, refusal and output correctness |
| Documentation coverage and voice check | `explain-qar` | no entry declared | Shipped-documents evaluation arm (`shipped_docs`) | Documentation coverage and voice conformance |
| Image description for grounding | `describe-an-attachment` | `quest_ai_runner.run("describe-an-attachment", ...)` | One vision-model call per image (today) (`vision_description`) | Answer pass rate gained per image described |
| Terminal helper calls | `assist-a-terminal-session` | `quest_ai_runner.run("assist-a-terminal-session", ...)` | Fast-tier model calls in the terminal (today) (`fast_model_helpers`) | Correct terminal helper verdicts within the wait a person accepts |
| Implementation selection | `select-an-implementation` | no entry declared | Live resolver driver (`live_resolver`) | Implementation selection |

## Problems and ideals

**Problems**

- Replies state things that are not in the person's own account, or miss facts that are, because the AI answers from general knowledge, from the wrong slice of data, or invents a link or claim that no... (`prob-reply-misses-the-account`)
- A follow-up such as "and the other one?", or a message sent while the AI is still working, is treated as a fresh start, so the person has to restate what they just said. (`prob-follow-up-loses-thread`)
- A developer embedding the library cannot tell which names are stable, how to configure a lane, whether the examples still run, or where to look, so integration is trial and error. (`prob-integration-is-trial-and-error`)
- The same request is handled less capably through the chat than as a task sent to a coding agent: the chat's own loop is a partial agent harness, so it fails, stops early or asks again where the... (`prob-harness-weaker-than-a-coding-agent`)

**Ideals**

- Unbroken Thread (`ideal-unbroken-thread`): A conversation carries what the person just said, wherever in time it appears.
- Trustworthy Interface (`ideal-trustworthy-interface`): What the documents say is true of the code, and a newcomer can start from them.

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
- and 1 more: all 11 are in [metrics](#metrics) of the full record below

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- Replies state things that are not in the person's own account, or miss facts that are, because the AI answers from general knowledge, from the wrong slice of data, or invents a link or claim that no source supports. (`prob-reply-misses-the-account`)
- A follow-up such as "and the other one?", or a message sent while the AI is still working, is treated as a fresh start, so the person has to restate what they just said. (`prob-follow-up-loses-thread`)
- A developer embedding the library cannot tell which names are stable, how to configure a lane, whether the examples still run, or where to look, so integration is trial and error. (`prob-integration-is-trial-and-error`)
- The same request is handled less capably through the chat than as a task sent to a coding agent: the chat's own loop is a partial agent harness, so it fails, stops early or asks again where the coding agent completes the work, while sending every request to the coding agent pays its slow, token-heavy context search each time. (`prob-harness-weaker-than-a-coding-agent`)

### Ideals

- Unbroken Thread (`ideal-unbroken-thread`): A conversation carries what the person just said, wherever in time it appears.
- Trustworthy Interface (`ideal-trustworthy-interface`): What the documents say is true of the code, and a newcomer can start from them.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Cost-aware message router | `route-a-message` | The loop planner decides the route (`planner_route`) | Routing correctness over the classification cases |
| Incremental context-update feed | `feed-context-sources` | The update engine over its built-in channels (`quest_channels`) | Update-offering correctness over synthetic quest channels |
| Conversation-keyed mid-run input inbox | `receive-input-mid-run` | In-memory thread-safe inbox (`in_memory`) | Exactly-once mid-run delivery per run, and how fast it is seen |
| Public package surface contract | `expose-the-public-api` | The names in the package __all__ (`package_front_doors`) | Public surface conformance on fixture inputs |
| Runnable example set | `learn-by-example` | Example check harness (`live_examples`) | Example runnability on an empty environment |
| Layered lane configuration loader | `configure-a-lane` | Layered configuration evaluation arm (`qar_config`) | Configuration precedence and validation correctness |
| Non-interactive command-line surface | `operate-from-a-terminal` | The command dispatcher run in process (`cli_commands`) | Command-line parsing, refusal and output correctness |
| Documentation coverage and voice check | `explain-qar` | Shipped-documents evaluation arm (`shipped_docs`) | Documentation coverage and voice conformance |
| Image description for grounding | `describe-an-attachment` | One vision-model call per image (today) (`vision_description`) | Answer pass rate gained per image described |
| Terminal helper calls | `assist-a-terminal-session` | Fast-tier model calls in the terminal (today) (`fast_model_helpers`) | Correct terminal helper verdicts within the wait a person accepts |
| Implementation selection | `select-an-implementation` | Live resolver driver (`live_resolver`) | Implementation selection |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Full planner decision | `route-a-message:qar_planner` | [evaluation/components/classification/impls/qar_planner.py](../../evaluation/components/classification/impls/qar_planner.py) |  |
| Keyword rules floor | `route-a-message:keyword_rules` | [evaluation/components/classification/impls/keyword_rules.py](../../evaluation/components/classification/impls/keyword_rules.py) |  |
| Source interface with shared channel flags | `feed-context-sources:base` | [quest_ai_runner/runner/context_sources_base.py](../runner/context_sources_base.py) |  |
| Live engine over fake quest channels | `feed-context-sources:update_engine` | [evaluation/components/feed_context_sources/impls/update_engine.py](../../evaluation/components/feed_context_sources/impls/update_engine.py) |  |
| In-memory thread-safe inbox | `receive-input-mid-run:in_memory` | [quest_ai_runner/core/inbox.py](../core/inbox.py) |  |
| Package root with front doors | `expose-the-public-api:__init__` | [quest_ai_runner/__init__.py](../__init__.py) |  |
| Long-lived background loop bridge | `expose-the-public-api:async_bridge` | [quest_ai_runner/take_in_the_world/async_bridge.py](async_bridge.py) |  |
| Live package-surface driver | `expose-the-public-api:live_surface` | [evaluation/components/expose_the_public_api/impls/live_surface.py](../../evaluation/components/expose_the_public_api/impls/live_surface.py) |  |
| Minimal lane example | `learn-by-example:minimal_lane` | [examples/minimal_lane.py](../../examples/minimal_lane.py) |  |
| Custom consumer configuration example | `learn-by-example:custom_consumer` | [examples/custom_consumer.py](../../examples/custom_consumer.py) |  |
| Example check harness | `learn-by-example:live_examples` | [evaluation/components/learn_by_example/impls/live_examples.py](../../evaluation/components/learn_by_example/impls/live_examples.py) |  |
| Configuration dataclass and file entry point | `configure-a-lane:config` | [quest_ai_runner/config.py](../config.py) |  |
| Strict TOML file loader | `configure-a-lane:config_file` | [quest_ai_runner/config_file.py](../config_file.py) |  |
| Environment and env-file layering | `configure-a-lane:config_environment` | [quest_ai_runner/config_environment.py](../config_environment.py) |  |
| Environment-backed tuning settings | `configure-a-lane:config_core_settings` | [quest_ai_runner/config_core_settings.py](../config_core_settings.py) |  |
| Injected core settings holder | `configure-a-lane:runtime_settings` | [quest_ai_runner/core/runtime_settings.py](../core/runtime_settings.py) |  |
| Layered configuration evaluation arm | `configure-a-lane:qar_config` | [evaluation/components/configure_a_lane/impls/qar_config.py](../../evaluation/components/configure_a_lane/impls/qar_config.py) |  |
| Command dispatcher | `operate-from-a-terminal:cli` | [quest_ai_runner/cli.py](../cli.py) |  |
| Declarative argument tree | `operate-from-a-terminal:cli_parser` | [quest_ai_runner/cli_parser.py](../cli_parser.py) |  |
| Quest API subcommands | `operate-from-a-terminal:cli_quest` | [quest_ai_runner/cli_quest.py](../cli_quest.py) |  |
| Card-store maintenance commands | `operate-from-a-terminal:store_cli` | [quest_ai_runner/store_cli.py](../store_cli.py) |  |
| Tool catalog shell entry | `operate-from-a-terminal:__main__` | [quest_ai_runner/tools/__main__.py](../tools/__main__.py) |  |
| Real-entry-point evaluation arm | `operate-from-a-terminal:qar_main` | [evaluation/components/operate_from_a_terminal/impls/qar_main.py](../../evaluation/components/operate_from_a_terminal/impls/qar_main.py) |  |
| Documentation index | `explain-qar:README.md` | [docs/README.md](../../docs/README.md) |  |
| Shipped-documents evaluation arm | `explain-qar:shipped_docs` | [evaluation/components/explain_qar/impls/shipped_docs.py](../../evaluation/components/explain_qar/impls/shipped_docs.py) |  |
| One vision-model call per image (today) | `describe-an-attachment:vision_description` | [quest_ai_runner/core/attachments.py](../core/attachments.py) |  |
| No description, the image is named only (proposed) | `describe-an-attachment:skip_images` |  |  |
| Fast-tier model calls in the terminal (today) | `assist-a-terminal-session:fast_model_helpers` | [quest_ai_runner/cli_config.py](../cli_config.py) |  |
| Rules only, no model call (floor, proposed) | `assist-a-terminal-session:rules_only` |  |  |
| The loop planner decides the route | `route-a-message:planner_route` | [quest_ai_runner/message_routing.py](../message_routing.py) |  |
| The update engine over its built-in channels | `feed-context-sources:quest_channels` | [quest_ai_runner/runner/context_updates.py](../runner/context_updates.py) |  |
| The command dispatcher run in process | `operate-from-a-terminal:cli_commands` | [quest_ai_runner/area_take_in_the_world.py](../area_take_in_the_world.py) |  |
| The names in the package __all__ | `expose-the-public-api:package_front_doors` | [quest_ai_runner/area_take_in_the_world.py](../area_take_in_the_world.py) |  |
| Implementation resolver | `select-an-implementation:resolve` | [quest_ai_runner/core/resolve.py](../core/resolve.py) |  |
| Live resolver driver | `select-an-implementation:live_resolver` | [evaluation/components/select_an_implementation/impls/live_resolver.py](../../evaluation/components/select_an_implementation/impls/live_resolver.py) |  |

### Metrics

- Routing correctness over the classification cases (`metric-route-a-message`): `esoi run classification --impl qar_planner`
- Update-offering correctness over synthetic quest channels (`metric-feed-context-sources`): `esoi run feed_context_sources --impl update_engine`
- Exactly-once mid-run delivery per run, and how fast it is seen (`metric-receive-input-mid-run`): `esoi run receive_input_mid_run --impl in_memory`
- Public surface conformance on fixture inputs (`metric-expose-the-public-api`): `esoi run expose_the_public_api --impl live_surface`
- Example runnability on an empty environment (`metric-learn-by-example`): `esoi run learn_by_example --impl live_examples`
- Configuration precedence and validation correctness (`metric-configure-a-lane`): `esoi run configure_a_lane --impl qar_config`
- Command-line parsing, refusal and output correctness (`metric-operate-from-a-terminal`): `esoi run operate_from_a_terminal --impl qar_main`
- Documentation coverage and voice conformance (`metric-explain-qar`): `esoi run explain_qar --impl shipped_docs`
- Answer pass rate gained per image described (`metric-describe-an-attachment`): `esoi run describe_an_attachment --impl vision_description`
- Correct terminal helper verdicts within the wait a person accepts (`metric-assist-a-terminal-session`): `esoi run assist_a_terminal_session --impl fast_model_helpers_live; esoi run assist_a_terminal_session --impl fast_model_helpers`
- Implementation selection (`metric-select-an-implementation`): `esoi run select_an_implementation --impl live_resolver`
