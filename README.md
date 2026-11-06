# Quest AI Runner

[![CI](https://github.com/SpiritualData/quest-ai-runner/actions/workflows/ci.yml/badge.svg)](https://github.com/SpiritualData/quest-ai-runner/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

Quest AI Runner (QAR) is the open-source brain of an AI that answers, acts and does deep work on a
person's own goals and material. It plans, gathers what it needs, answers in plain words, and hands
long jobs to a coding agent that works until the written standard for the job is met.

It connects Quest (the goal and task app) to a model. It also takes its parts from the hub of
hubs, [ai-hub-of-hubs-structure](https://github.com/SpiritualData/ai-hub-of-hubs-structure)
(package `hubofhubs`), which is built on the esoi library. The brain knows nothing about any one
organization, person or product. Those details arrive as configuration and adapters.

## What it can do

- **Answer.** A bounded loop plans, gathers evidence, re-plans and answers, grounded in the person's
  quests, documents, past conversations and preferences.
- **Act.** Direct tools, such as sending an email or writing a note, run in one planner step. Steps
  that only a person should decide are raised as decision requests (asks).
- **Do deep work.** Long jobs go to Claude Code, or to any agent that speaks the Agent Client
  Protocol, with a written goal, a turn budget, a review of the result and a check that the goal was met.
- **Run queued tasks.** A poller finds due tasks in Quest, claims them, runs them through the brain
  and reports the result.
- **Chat in a terminal.** A Textual interface offers a `/` command menu, saved conversations you can
  resume, and a quest picker.
- **Build context from many sources.** Files, databases, Claude conversations, Notion, Google Drive
  and Chat, and MCP servers feed one context engine. It orients the agent with docstring cards,
  semantic vectors and BM25, and it remembers which context helped on similar past tasks.
- **Search the web and scholarly sources.** Live web search runs through several backends, and paper
  search runs through OpenAlex, arXiv, Semantic Scholar and PubMed.
- **Guard its replies.** A link guard checks every link, a support check weighs web claims, and a
  minimal-intervention overseer sends a signal only when a run needs correcting.
- **Show its work.** The work trace (the "Trail") lists the sites read, papers cited and records used
  in a turn. It is built from structural results, never from the model's wording.
- **Send interim replies and asks.** A turn that is unfinished can say what is done, why more is
  coming and what comes next, and a turn blocked on a person files one ask. A lane switches this on
  with `OrchestratorConfig.interim_replies`.
- **Choose models by tier.** Each piece of work starts on the cheapest model tier its difficulty
  allows, and moves to another provider when one is down.

## Architecture

QAR uses ports and adapters. The core defines the interfaces, the adapters implement them, and one
configuration per lane wires them together. A lane is one deployment with its own credentials,
corpus and personas.

```
            one configuration per lane (qar.toml, RunnerConfig)
                                |
                                v
   +------------------------------------------------------------+
   | core/    plan -> gather -> re-plan -> answer | deep | ask    |
   |          ports: Retrieval, ModelProvider, DeepRunner,        |
   |          EscalationSink, ProgressSink                        |
   +--------+----------------------+---------------------+-------+
            |                      |                     |
     adapters/ (reference)    runner/ (executor)    cli and Textual UI
     files, card stores,      Quest client, poller,  chat, poll, doctor,
     the model providers      executor, autopilot,   quest, bootstrap
     (what several areas      personas, lane
     use); each area's own
     code is in its folder
            (act_as_the_body: DB cache, MCP, Notion, Google, deep runners;
             know_the_world: web; remember_and_recall: vectors)
            |
     hubofhubs seats: models, recall, critique, deep, websearch, science, risk
```

[Anatomy](docs/explanation/anatomy.md) draws QAR as a mind and as a hub of hubs.
[Streaming and modes](docs/explanation/streaming-and-modes.md) covers the live and background
modes of the same brain.

## How QAR uses the hub of hubs (AHOHS)

The hub of hubs holds one seat for each part of a mind, such as calling a model, recalling context,
judging a reply, doing deep work, searching the web and watching for risk. Each seat has Protocols
and measured implementations. QAR takes its implementations from the hub and keeps only what is
specific to Quest:

| QAR solution | Hub seat and package |
|---|---|
| `vendor-model-caller` | model providers, prefix router, retry and outage helpers (`hubofhubs.the_world.model_plumbing.vendor_model_caller`) |
| `context-retriever`, `index-by-meaning` | Qdrant vectors, BM25, hybrid assembler (`hubofhubs.the_intelligence.subconscious.context_retriever`) |
| `critical-thinking` | link guard and sufficiency gate (`hubofhubs.the_intelligence.critical_thinking`) |
| `run-deep-work` | one-shot, stream-json and ACP runners (`hubofhubs.the_world.tools.delegated_agent_sessions`) |
| `search-the-web` | search backends, cache, fetch, extraction (`hubofhubs.the_world.tools.web_search`) |
| `the-science` | scholarly search (`hubofhubs.the_world.science`) |
| `nervous-system` | the overseer engine (`hubofhubs.the_intelligence.mind.delegation.overseer.risk`) |

`esoi/hub_seats.csv` records which QAR solution fills which hub area, with the vendor names and code
paths. The hub's scorer reads it, and `esoi run hub_of_hubs --impl registry_scan` measures the
connection.

**Defaults come from a selection snapshot.** Every solution has a `selected` implementation in
its `solutions.csv` (kept beside its code; `esoi/README.md` links to every folder of records). A lane resolves an implementation by name in this order: a per-solution
override in the lane configuration, an `ESOI_SELECT_` environment variable, a `[selection]` table in
`esoi.toml`, then the project default. An installed copy ships without the records, so it reads
`quest_ai_runner/esoi_selection.json`, which `esoi select --snapshot` writes.

**Each implementation declares what it needs.** Environment variables, API keys, packages, programs
and services are listed beside the implementation, and the hub-declared variables are generated into
`.env.example`. `quest-ai-runner doctor` lists what is unmet and gives the fix for each. It calls no
model and prints names only, never values. A deployment records its selections with
`esoi lock --package quest_ai_runner`, and `doctor --lock` reports newer selections as safe or
blocked by an unmet requirement. See
[keep QAR current with the hub](docs/how-to/keep-quest-ai-runner-current-with-the-hub.md).

## How the esoi layer works here

The `esoi/` folder holds QAR's intent as CSV files: **problems** people have, **ideals** to reach,
**solutions** that address them, **implementations** (the competing arms of a solution) and
**metrics** that score the arms. The evaluation code in `evaluation/components/` runs every arm of a
solution on the same cases and records one row per run in `evaluation/components/results.csv`.

`esoi leaderboard` ranks the arms of each solution, and the highest scorer on the metric is the
default. A lead is labelled **significant** when an exact paired test gives an adjusted p-value below
0.05, and **best guess** otherwise. `metric-qar-overall` combines the sub-metrics, weighted by the
ideals, from recorded rows only, and reports a floor and a ceiling.

Measured results, copied from `results.csv` (pass counts on QAR's own cases, with the label that
`esoi leaderboard` prints):

| What was measured | Result | Label |
|---|---|---|
| End to end, 55 turns on the cheapest model tier through the Claude Code subscription (2026-10-31) | `qar_live` 52 of 55 right; `rules_only` 7 of 55 | counts as recorded |
| End to end, sample of 10 turns (2026-11-01) | `qar_live` 10 of 10; `rules_only` 4 of 10 | best guess (p=0.377) |
| Model provider failover, 56 cases | `multi_provider` 56 of 56; `single_provider` 16 of 56 | significant (p=9.09e-13) |
| Interim replies and asks, 20 cases | `gated` 20 of 20; `today` 11 of 20; `every_attempt` 3 of 20 | significant (p=0.00195 and 1.53e-05) |
| Work trace, 16 offline turns | `trail` 16 of 16; `plain_list` 14 of 16; `no_trace` 5 of 16 | counts as recorded |
| Work trace, 10 real turns (Haiku, judged by Sonnet) | `trail` 3 of 10; `plain_list` 3 of 10; `no_trace` 1 of 10 | counts as recorded |
| Context selection, top 3 of 63 cases | `hybrid_union` 46; `vector_bge_small` 34; `blind_grep` 21 | significant (p=0.00627 and 2.19e-05) |
| Retrieval across sources, 21 cases | `composite` 19 of 21; `first_three` 0 of 21 | significant (p=1.91e-06) |
| Proactive guidance, 24 cases | `guided_v2` 20 of 24; `no_nudge` 12 of 24 | significant (p=0.0457) |
| Web answers with a support check, 9 cases | `web_with_support_check` 9 of 9; `memory_only` 0 of 9 | significant (p=0.00195) |
| Model tier choice, 42 cases | `qar_selection` 42 of 42 | no rival measured |

Every record and result is in [esoi/README.md](esoi/README.md) and
[evaluation/README.md](evaluation/README.md).

## The front door

One line runs any solution by its id, QAR's own or any hub solution beneath it (`quest_ai_runner.run("<id>", input)` returns what the solution's interface returns, `quest_ai_runner.run(Interface)` returns its typed handle; keywords override only what differs and `environ` defaults to the process environment):

```python
import quest_ai_runner

quest_ai_runner.run("quest-ai-runner", "What should I work on first this week?")      # the whole flow (the_self/respond.py)
quest_ai_runner.run("context-retriever", "What should I work on first this week?")       # the context a request reads
quest_ai_runner.run("proactivity", {"schedules": [...], "now": "2026-07-12T09:00:00Z"})  # when standing work is due (the hub's Proactivity)
quest_ai_runner.run("prioritization", {"person": "", "request": "..."})               # what matters first (the hub's Prioritization, answered by Quest's ordering arm)
```

A host decides what is due and what comes first through these two lines and no code of its own: a schedule is `{"name", "request", "cadence", "zone", "last_run", "run_requested_at"}` (and `"event_source"` plus its pulled `events` for a reactive one), a candidate of Prioritization is `{"id", "kind", ...}` with the fields Quest's data carries (`rank`, `rank_by_person`, `owner`, `horizon`, `deadline`, `asked_at`). No name is added to `quest_ai_runner.api` for them: `run` is the whole surface.

Every solution declares its interface in one `interface.py` beside its code, with the entry `run` calls. A host application imports only from `quest_ai_runner.api`, which lists the names a consumer outside this repository needs (ports it implements, constants, value objects), grouped by the solution they belong to, and `run`. Under Quest Autopilot it exports the markers autopilot tasks carry (`PROPOSAL_TEXT_PREFIX`, `AUTOPILOT_PASS_KIND`, `AUTOPILOT_WORK_KIND`) and the bundled default instructions a host seeds a quest and a persona with (`BUNDLED_DEFAULT_QUEST_INSTRUCTIONS`, `BUNDLED_DEFAULT_PERSONA_INSTRUCTIONS`).

The flow of one request is `quest_ai_runner/the_self/respond.py`: Request intake, Higher mind, Subconscious, Mind, Nervous System and Critical Thinking, in that order, with the Mind's orchestrator loop as the selected arm. Autopilot is the hub's Proactivity (when standing work starts) and Prioritization (what matters first); QAR supplies Quest as the source of events and priorities.

| Solution id | Interface | Entry | Declared in |
|---|---|---|---|
| `cache-cards` | `CardCache` | `load_listing` | `quest_ai_runner/the_body/internal_context/card_store/interface.py` |
| `card-store` | `CardKeeping` | `search` | `quest_ai_runner/the_body/internal_context/card_store/interface.py` |
| `cards-from-files` | `CardBuilding` | `build` | `quest_ai_runner/the_intelligence/subconscious/cards_from_files/interface.py` |
| `choose-the-context` | `ContextChoosing` | `choose` | `quest_ai_runner/the_intelligence/subconscious/context_retriever/interface.py` |
| `context-intake` | `PromptAssembling` | `assemble` | `quest_ai_runner/the_intelligence/mind/planner/interface.py` |
| `context-retriever` | `ContextSelecting` | `assemble` | `quest_ai_runner/the_intelligence/subconscious/context_retriever/interface.py` |
| `critical-thinking` | `MetricHolding` | `evaluate` | `quest_ai_runner/the_intelligence/critical_thinking/interface.py` |
| `delegated-agent-sessions` | `DeepSessions` | `run_goal` | `quest_ai_runner/the_intelligence/mind/delegation/interface.py` |
| `describe-an-attachment` | `AttachmentPreparing` | `prepare` | `quest_ai_runner/the_intelligence/mind/context_intake/attachments.py` |
| `detect-a-stalled-run` | `StallDetecting` | `review` | `quest_ai_runner/the_intelligence/mind/delegation/interface.py` |
| `esoi-application` | `RecordsEditing` | `apply` | `quest_ai_runner/the_intelligence/critical_thinking/esoi_application/interface.py` |
| `feed-context-sources` | `ContextFeeding` | `collect` | `quest_ai_runner/the_body/intake_sources/interface.py` |
| `feedback-learning` | `FeedbackKeeping` | `open_items` | `quest_ai_runner/the_consciousness/feedback_learning/feedback_ledger.py` |
| `file-editing` | `FileWriting` | `read_file` | `quest_ai_runner/the_world/tools/file_editing/interface.py` |
| `goal-completion` | `DecisionJudging` | `reach_verdict` | `quest_ai_runner/the_intelligence/critical_thinking/goal_completion/interface.py` |
| `guidance` | `IntentGrounding` | `frame` | `quest_ai_runner/the_intelligence/higher_mind/objectives/interface.py` |
| `guide-by-feedback` | `GuidanceProvider` | `select` | `quest_ai_runner/the_intelligence/higher_mind/guidance/interface.py` |
| `guide-toward-the-long-term-goal` | `LongTermGuiding` | `directive` | `quest_ai_runner/the_intelligence/higher_mind/guidance/interface.py` |
| `hand-off-a-goal` | `GoalHandingOff` | `assign` | `quest_ai_runner/the_consciousness/interface.py` |
| `index-by-meaning` | `MeaningIndexing` | `search` | `quest_ai_runner/the_intelligence/subconscious/context_retriever/interface.py` |
| `intake-sources` | `ChannelTransport` | `send` | `quest_ai_runner/the_consciousness/request_intake/interface.py` |
| `intent-evolution` | `IntentEvolving` | `suggest` | `quest_ai_runner/the_consciousness/intent_evolution/interface.py` |
| `long-term-memory` | `ConversationRemembering` | `current_slice` | `quest_ai_runner/the_intelligence/higher_mind/long_term_memory/interface.py` |
| `metric-translation` | `MetricDrafting` | `draft` | `quest_ai_runner/the_intelligence/critical_thinking/metric_translation/interface.py` |
| `model-tier-chooser` | `RegistryTierSelection` | `choose` | `quest_ai_runner/the_world/model_plumbing/model_tier_chooser/interface.py` |
| `nervous-system` | `RunWatching` | `watch` | `quest_ai_runner/the_intelligence/nervous_system/interface.py` |
| `persist-cards` | `CardPersisting` | `read` | `quest_ai_runner/the_body/internal_context/card_store/interface.py` |
| `planner` | `MessageRouting` | `route` | `quest_ai_runner/the_intelligence/mind/planner/interface.py` |
| `quest-account-truth` | `BodyActing` | `describe` | `quest_ai_runner/the_body/interface.py` |
| `quest-autopilot` | `PassGating` | `gate` | `quest_ai_runner/the_intelligence/proactivity/interface.py` |
| `quest-body` | `QuestOperations` | `list_quest_goals` | `quest_ai_runner/the_body/internal_context/quest/interface.py` |
| `quest-change-executor` | `QuestOperating` | `operate` | `quest_ai_runner/the_body/hands/quest_operations/interface.py` |
| `quest-consciousness` | `ConsciousnessFlowing` | `take_in` | `quest_ai_runner/the_consciousness/interface.py` |
| `quest-context` | `RetrievalAdapter` | `read_section` | `quest_ai_runner/the_body/internal_context/interface.py` |
| `quest-decision-requests` | `EscalationSink` | `escalate` | `quest_ai_runner/the_consciousness/judgment_calls/interface.py` |
| `quest-intents` | `IntentHolding` | `hold` | `quest_ai_runner/the_intelligence/higher_mind/objectives/interface.py` |
| `quest-representatives` | `RepSelecting` | `resolve` | `quest_ai_runner/the_consciousness/representatives/interface.py` |
| `quest-requests` | `QueueRunning` | `scan` | `quest_ai_runner/the_consciousness/request_intake/interface.py` |
| `quest-self` | `GoalAgent` | `answer` | `quest_ai_runner/the_self/interface.py` |
| `quest-work-shown` | `WorkShowing` | `start` | `quest_ai_runner/the_intelligence/mind/response_writer/work_trace.py` |
| `recall-recent-context` | `RecentContextStore` | `load` | `quest_ai_runner/the_intelligence/higher_mind/long_term_memory/interface.py` |
| `receive-input-mid-run` | `InputInbox` | `peek` | `quest_ai_runner/the_intelligence/mind/delegation/interface.py` |
| `replay-known-operations` | `OperationReplaying` | `match` | `quest_ai_runner/the_intelligence/mind/planner/interface.py` |
| `reply-in-steps` | `InterimPolicy` | `decide` | `quest_ai_runner/the_intelligence/mind/response_writer/interim_policy.py` |
| `response-quality` | `ReplyGuarding` | `links` | `quest_ai_runner/the_intelligence/critical_thinking/response_quality/interface.py` |
| `run-applies-esoi` | `GoalIteration` | `slice_for` | `quest_ai_runner/the_intelligence/critical_thinking/goal_iteration_interface.py` |
| `show-progress` | `ProgressShowing` | `update` | `quest_ai_runner/the_consciousness/terminal/interface.py` |
| `system-integrity` | `KeepingGeneric` | `audit` | `quest_ai_runner/the_self/integrity/interface.py` |
| `take-in-the-world` | `RequestTakingIn` | `take_in` | `quest_ai_runner/the_consciousness/request_intake/interface.py` |
| `the-intuition` | `PredictionStore` | `load_predictions` | `quest_ai_runner/the_intelligence/subconscious/intuition/interface.py` |
| `the-mind` | `StepReasoning` | `think` | `quest_ai_runner/the_intelligence/mind/interface.py` |
| `the-science` | `KnowingTheWorld` | `search` | `quest_ai_runner/the_world/science/interface.py` |
| `the-subconscious` | `CardRecall` | `recall` | `quest_ai_runner/the_intelligence/subconscious/interface.py` |
| `the-tools` | `ReachClaiming` | `claim` | `quest_ai_runner/the_world/interface.py` |
| `vendor-model-caller` | `RoutedModelCalling` | `answer` | `quest_ai_runner/the_world/model_plumbing/vendor_model_caller/interface.py` |
| `web-search` | `WebSearching` | `search` | `quest_ai_runner/the_world/tools/web_search/web_research.py` |
| `write-through-mcp` | `OperationWriting` | `write_operation` | `quest_ai_runner/the_world/tools/connected_system_operations/interface.py` |

## Quickstart

Install from a checkout with Python 3.11 or newer. The hub and esoi install with it.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[anthropic,dev]'
quest-ai-runner --help
```

**Chat in your terminal** over a folder of notes. A Quest account is optional.

```bash
export QAR_CORPUS_ROOT=/path/to/your/notes
export ANTHROPIC_API_KEY=...        # or log in to the claude CLI
quest-ai-runner chat --check        # confirms the provider and the context store
quest-ai-runner chat                # ask a question; /quit prints the resume command
quest-ai-runner chat --continue     # reopen the latest conversation
```

**Use the brain from Python:**

```python
from quest_ai_runner.the_body.configuration.config import RunnerConfig, build_orchestrator
from hubofhubs.the_world.model_plumbing.vendor_model_caller.anthropic import AnthropicProvider
from quest_ai_runner.the_world.tools.file_reading.files_adapter import FilesAdapter

cfg = RunnerConfig(retrieval=FilesAdapter("/path/to/docs"),
                   model_provider=AnthropicProvider(api_key="..."))
orch = build_orchestrator(cfg)
print(orch.run("What does the pricing doc say?").text)
```

**Run the executor lane** that polls Quest for due tasks:

```bash
export QUEST_BASE_URL=... QUEST_API_KEY=qsk_... QUEST_TEAM_ID=... QAR_CORPUS_ROOT=...
quest-ai-runner poll --check        # validates the configuration and the key
quest-ai-runner poll --once         # one scan, then exit (good for cron)
```

**Call the Quest API from a script.** `quest-ai-runner quest --list` shows every callable method,
tagged read or write, and `load_client("qar.toml")` returns the same client in Python.

**Check the environment** with `quest-ai-runner doctor`.

## Configuration

A lane is one TOML file plus environment variables. Every setting, its default and the code that
reads it are listed in [docs/reference/configuration.md](docs/reference/configuration.md). The shape
of a file is in [a lane is a config file](docs/how-to/a-lane-is-a-config-file.md), and
[examples/](examples/) holds runnable lanes.

## Documentation

The docs follow four categories, and [docs/README.md](docs/README.md) is the index.

- **Tutorials:** [quickstart](docs/tutorials/quickstart.md),
  [terminal chat in ten minutes](docs/tutorials/terminal-chat-in-ten-minutes.md),
  [your first lane](docs/tutorials/tutorial-your-first-lane.md).
- **How-to guides:** [the terminal chat](docs/how-to/chat.md),
  [writing a consumer](docs/how-to/writing-a-consumer.md),
  [web search](docs/how-to/web-search.md), [tools](docs/how-to/tools.md),
  [adapters](docs/how-to/adapters.md), [deployment](docs/how-to/deployment.md).
- **Explanation:** [context assembly](docs/explanation/context-assembly.md),
  [the hub of hubs](docs/explanation/hub-of-hubs.md), [link guard](docs/explanation/link-guard.md),
  [overseer](docs/explanation/overseer.md), [live work trace](docs/explanation/live-work-trace.md),
  [incremental replies and asks](docs/explanation/incremental-replies-and-asks.md),
  [metric results](docs/explanation/metric-results.md).
- **Reference:** [configuration](docs/reference/configuration.md),
  [architecture standards](docs/reference/ARCHITECTURE_STANDARDS.md),
  [Quest API contract](docs/reference/quest-api-contract.md),
  [consumer registry](docs/reference/consumer-registry.md).

## Develop and test

```bash
python -m pytest tests/test_core_adapters_public_names.py -q   # run only the tests for what you changed
esoi check                                                     # validate the ESOI records
esoi docs check                                                # validate the docs layout
esoi leaderboard                                               # rank the arms of every solution
```

Tests use stub adapters and a mock Quest client, so they run offline.

## Contributing, security and license

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [CLAUDE.md](CLAUDE.md) first. The rule that matters
most is to keep the library generic: no organization names, ids, keys, emails or absolute paths in
the package. Everyone taking part agrees to the [Code of Conduct](CODE_OF_CONDUCT.md). Report
vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).

Licensed under the [Apache License 2.0](LICENSE). Copyright Spiritual Data.
