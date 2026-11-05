# Quest Requests

**Vision statement:** Every request made in Quest is taken in once by the same AI.

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > Quest Requests

This sub-intent arises as the implementation `quest-requests:poller` of its parent intent [`the_consciousness`](../README.md). Opportunities: A runner is reached from many places and each has its own failure and cost, which the parent solution measures one by one and none together: a short chat message must not pay for a long run, an image has to be described before it can ground an answer, input that arrives while a run works must be heard at the next decision, a terminal session needs helper calls that cost a fraction of a cent, and a developer needs a package that installs and runs on an empty environment, learned from documents and examples that are kept true.

## How the solutions fit together

Quest Requests takes every request in once and gives each Quest surface the same understanding of it. Take In The World accepts a request from a chat channel, the terminal or the embedded package and says where it goes. Receive Input Mid Run holds what a person sends while work runs and hands it to the run's next decision exactly once. Describe An Attachment turns an attached image into text the AI can use.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- Take In The World (solution with no page of its own, its implementations are in this intent's records): Let a person or a program reach the runner from wherever they work and be understood the same way.
- Receive Input Mid Run (solution with no page of its own, its implementations are in this intent's records): Lets a person change direction, pause or stop an assistant while it works, and be heard at its next decision.
- Describe An Attachment (solution with no page of its own, its implementations are in this intent's records): Gives a person the text a vision model writes for an image a person attached at a cost and delay that its metric accepts.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Take In The World | `take-in-the-world` | `quest_ai_runner.run("take-in-the-world", 'Save a note that the launch moves to Friday.')` | Channels, terminal and package as ways in (`ways_in`) | Ways in that pass their own metric |
| Receive Input Mid Run | `receive-input-mid-run` | `quest_ai_runner.run("receive-input-mid-run", 'conversation-1')` | In-memory thread-safe inbox (`in_memory`) | Exactly-once mid-run delivery per run, and how fast it is seen |
| Describe An Attachment | `describe-an-attachment` | `quest_ai_runner.run("describe-an-attachment", ...)` | One vision-model call per image (today) (`vision_description`) | Answer pass rate gained per image described |

## Solutions from a library

- [Request Intake](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_consciousness/README.md) (`take-in-the-world`): Takes in the requests people make from every source Body declares and attaches each to the person who made it, so the rest of the flow can ask for that person's Representative. Specializes the library `hub`.
- [Request Intake](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_consciousness/README.md) (`receive-input-mid-run`): Takes in the requests people make from every source Body declares and attaches each to the person who made it, so the rest of the flow can ask for that person's Representative. Specializes the library `hub`.
- [Request Intake](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_consciousness/README.md) (`describe-an-attachment`): Takes in the requests people make from every source Body declares and attaches each to the person who made it, so the rest of the flow can ask for that person's Representative. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Lost Or Unreported Tasks (`QP1`): Quest tasks attached to goals can be lost, run twice, or end without an outcome the person sees.

**Ideals**

- Dependable Delivery (`ideal-dependable-delivery`): What is handed to the system arrives once, in order, and is accounted for.

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
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Lost Or Unreported Tasks (`QP1`): Quest tasks attached to goals can be lost, run twice, or end without an outcome the person sees.

### Ideals

- Dependable Delivery (`ideal-dependable-delivery`): What is handed to the system arrives once, in order, and is accounted for.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Take In The World | `take-in-the-world` | Channels, terminal and package as ways in (`ways_in`) | Ways in that pass their own metric |
| Receive Input Mid Run | `receive-input-mid-run` | In-memory thread-safe inbox (`in_memory`) | Exactly-once mid-run delivery per run, and how fast it is seen |
| Describe An Attachment | `describe-an-attachment` | One vision-model call per image (today) (`vision_description`) | Answer pass rate gained per image described |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Channels, terminal and package as ways in | `take-in-the-world:ways_in` | [quest_ai_runner/the_consciousness/ways_in.py](../ways_in.py) |  |
| In-memory thread-safe inbox | `receive-input-mid-run:in_memory` | [quest_ai_runner/the_intelligence/mind/delegation/inbox.py](../../the_intelligence/mind/delegation/inbox.py) |  |
| One vision-model call per image (today) | `describe-an-attachment:vision_description` | [quest_ai_runner/the_intelligence/mind/context_intake/attachments.py](../../the_intelligence/mind/context_intake/attachments.py) |  |
| No description, the image is named only (proposed) | `describe-an-attachment:skip_images` |  |  |

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


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
