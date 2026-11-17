# quest_ai_runner

Breadcrumb: [Top](../esoi/README.md) > quest_ai_runner

Part of [Quest AI Runner](../esoi/README.md)

## Quest Self (`quest-self`)

Make each Quest user meet one whole assistant whose parts agree, so that a part passing its own test cannot hide a failure that the person finds out about.

- Out of scope: How each part does its job: the hub's The Self owns the general flow and its metric.
- Delegates to: quest-intelligence, quest-world, quest-body, quest-consciousness
- Entry call: `quest_ai_runner.run("quest-self", 'What should I work on first this week?')`
- Selected implementation: quest-ai-runner library (`library`)
- Metric: Judge pass rate over the chat quality cases, Whole-path turns right on a small mixed set, Quest answers that name the person's goal and circumstance and fit features to them, Day-with-Jarvis turns right end to end, Asks filed and deletes held before anything outward or irreversible happens, Replies that claim only what was done, Passing chat turns per dollar, Delegated tasks meeting their standard, Exact and consented changes, Data kept in scope, Engine reuse, Measured coverage, QAR overall score
- Implemented by: [`evaluation/components/act_as_the_body/__init__.py`](../evaluation/components/act_as_the_body/__init__.py), [`evaluation/components/act_as_the_body/broken/__init__.py`](../evaluation/components/act_as_the_body/broken/__init__.py), [`evaluation/components/act_as_the_body/broken/blind_replay.py`](../evaluation/components/act_as_the_body/broken/blind_replay.py), [`evaluation/components/act_as_the_body/cases.json`](../evaluation/components/act_as_the_body/cases.json), [`evaluation/components/act_as_the_body/impls/__init__.py`](../evaluation/components/act_as_the_body/impls/__init__.py), [`evaluation/components/act_as_the_body/impls/card_and_reads.py`](../evaluation/components/act_as_the_body/impls/card_and_reads.py), [`evaluation/components/act_as_the_body/interface.py`](../evaluation/components/act_as_the_body/interface.py), [`evaluation/components/act_as_the_body/score.py`](../evaluation/components/act_as_the_body/score.py) and 1148 more

### Top implementations of Quest Self

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Offline metric validity check (`reference_replies`) | 100% (331 of 331 cases) | free |  |
| 2 | Planner-only route check on the chat dataset (`planner_route`) | 94% (94 of 100 cases) | $0.0007 |  |
| 3 | Live chat on a disposable world (`dev_quest_chat`) | 80% (24 of 30 cases) | 1.2 calls |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

3 more compared; the full list is in the results log.

**How it is scored:** Passing chat turns per dollar. share of realistic chat turns that pass their rubric (grounded facts, right handling, exact changes), and cost per passing turn
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Quest Body (`quest-body`)

Give the assistant one declared place where a person's Quest data lives and where requests come from.

- Out of scope: Which context a request needs and how a change is made: Quest Context and Quest Change Executor own those.
- Delegates to: quest-context, quest-change-executor, the-science
- Entry call: `quest_ai_runner.run("quest-body", 'quest-id')`
- Selected implementation: Quest HTTP client from the environment (`quest_client`)
- Metric: Parity of Quest operations across access points
- Implemented by: [`quest_ai_runner/body/area_body.py`](body/area_body.py), [`quest_ai_runner/body/internal_context/quest/__init__.py`](body/internal_context/quest/__init__.py), [`quest_ai_runner/body/internal_context/quest/cached_db_adapter.py`](body/internal_context/quest/cached_db_adapter.py), [`quest_ai_runner/body/internal_context/quest/interface.py`](body/internal_context/quest/interface.py), [`quest_ai_runner/body/internal_context/quest/quest_ask_cards.py`](body/internal_context/quest/quest_ask_cards.py), [`quest_ai_runner/body/internal_context/quest/quest_retrieval_adapter.py`](body/internal_context/quest/quest_retrieval_adapter.py), [`quest_ai_runner/body/internal_context/quest/spec_records.py`](body/internal_context/quest/spec_records.py)

### Top implementations of Quest Body

No implementation has a recorded result yet, so the choice is not backed by a measurement.

**How it is scored:** Parity of Quest operations across access points. Ensures that an operation on Quest's data gives the same result whether it is reached in the same process as Quest or over its HTTP API.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Quest Intelligence (`quest-intelligence`)

Let every request be answered for what the person is working toward.

- Out of scope: How each member does its job, and the order a whole request meets them, which self/respond.py owns.
- Delegates to: quest-intents, the-subconscious, the-mind, nervous-system, critical-thinking, quest-autopilot, proactivity
- Entry call: `quest_ai_runner.run("quest-intelligence", {'request': 'what is the latest Rust version?'})`
- Selected implementation: `composition`
- Metric: Quest Intelligence on Quest cases
- Implemented by: [`quest_ai_runner/intelligence/flow.py`](intelligence/flow.py), [`quest_ai_runner/intelligence/interface.py`](intelligence/interface.py)

### Top implementations of Quest Intelligence

No implementation has a recorded result yet, so the choice is not backed by a measurement.

**How it is scored:** Quest Intelligence on Quest cases. One balanced number over Quest cases for the composition of the Intelligence members (Quest Intents, Subconscious, Mind, Nervous System, Critical Thinking, Quest Autopilot and Proactivity), run on Quest-only cases.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Quest World (`quest-world`)

Let the assistant learn what lies outside a person's Quest and act on it through one measured path.

- Out of scope: How each member does its job, and web search and the freshness of the hub's research, which the hub's World owns.
- Delegates to: model-plumbing, the-science, the-tools
- Entry call: `quest_ai_runner.run("quest-world", 'latest Rust version')`
- Selected implementation: `composition`
- Metric: Quest World on Quest cases
- Implemented by: [`quest_ai_runner/world/flow.py`](world/flow.py), [`quest_ai_runner/world/interface.py`](world/interface.py)

### Top implementations of Quest World

No implementation has a recorded result yet, so the choice is not backed by a measurement.

**How it is scored:** Quest World on Quest cases. One balanced number over Quest cases for the composition of the World members (Model Plumbing, Science and Tools), run on Quest-only cases.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Quest Consciousness (`quest-consciousness`)

Let each Quest user grow with an AI aligned to them.

- Out of scope: How each of those does its job.
- Delegates to: quest-requests, quest-representatives, quest-decision-requests, intent-evolution, feedback-learning
- Entry call: `quest_ai_runner.run("quest-consciousness", 'team-1')`
- Selected implementation: `composition`
- Metric: Quest Consciousness on Quest cases
- Implemented by: [`quest_ai_runner/consciousness/flow.py`](consciousness/flow.py), [`quest_ai_runner/consciousness/interface.py`](consciousness/interface.py)

### Top implementations of Quest Consciousness

No implementation has a recorded result yet, so the choice is not backed by a measurement.

**How it is scored:** Quest Consciousness on Quest cases. One balanced number over Quest cases for the composition of the five Consciousness records (Quest Requests, Quest Representatives, Quest Decision Requests, Feedback Learning and Intent Evolution), built on the hub's metric-the-consciousness and run on Quest-only cases.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## One level below

- [Quest Body](body/README.md) (sub-intent): Quest is the AI's body.
- [Quest Consciousness](consciousness/README.md) (sub-intent): The AI acts for each person the way that person would want.
- [Intelligence](intelligence/README.md) (sub-intent): Each request is understood against what the person is working toward.
- [Quest Self](self/README.md) (sub-intent): Each Quest user meets one whole assistant.
- [World](world/README.md) (sub-intent): What lies beyond a person's Quest reaches the AI through one measured path.
- [Feedback Learning](consciousness/feedback_learning/README.md) (solution with implementations)
- [Write Through MCP](world/tools/connected_system_operations/README.md) (solution with implementations): Carry out one named change on an external tool server, and only when an explicit allowlist permits that operation.
- [Place On A Quest](intelligence/higher_mind/objectives/place_on_a_quest/README.md) (solution with implementations): Say which of the person's quests a message, goal, note or task belongs to, or none, the same way on every surface that picks one.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Quest Self<br/><i>quest-self</i>"]
    s1["Quest Body<br/><i>quest-body</i>"]
    s2["Quest Intelligence<br/><i>quest-intelligence</i>"]
    s3["Quest World<br/><i>quest-world</i>"]
    s4["Quest Consciousness<br/><i>quest-consciousness</i>"]
    s1 -->|"The Quest client through which every read and write of the person's Quest passes"| s0
    s2 -->|"The response to one request, from its intents, its context, its reasoning and its measure"| s0
    s3 -->|"What the world beyond the person's Quest answered, with its source"| s0
    s4 -->|"The request taken in, handled by a representative and answered"| s0
```

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Per-consumer worker | `quest-self:per-consumer-worker` | Each product writes its own worker and chat backend around a model vendor's API. | no code yet |  |
| Agent framework with Quest glue | `quest-self:agent-framework-with-quest-glue` | Build the engine from a general framework (LangGraph, CrewAI, an agent SDK) and add Quest grounding, escalation and the executor as glue. | no code yet |  |
| Deep runner only | `quest-self:deep-runner-only` | Hand every task straight to a coding agent such as Claude Code with the person's material available, with no routing or answering loop in front. | no code yet |  |
| quest-ai-runner library | `quest-self:library` | A domain-free Python library: a bounded plan, gather, re-plan, answer loop that escalates to a deep runner when real work is needed (the core), a task executor that polls Quest, claims, runs, escalates and reports (the runner), and reference adapters for retrieval, model providers, cards, vectors, web research and deep runners, all wired by one configuration per lane and each swappable part chosen by metric. | [`quest_ai_runner/self/respond.py`](self/respond.py) | selected |
| Quest HTTP client from the environment | `quest-body:quest_client` | Builds the Quest client from QUEST_BASE_URL (or QUEST_API_URL), QUEST_API_KEY and, when set, QUEST_TEAM_ID, so the runner works in the account those name. Every write through it reports its outcome as a typed WriteResult (applied, held or noop) or a raised QuestWriteError; the specification and intent writes return the WriteResult for a refusal too (status refused, failed or unknown, falsy, with the server's code and locked fields in its data), so a refused write is never a plain dict that reads as success. It carries the quest creation flow's routes: the strategy map (read, answer, picks, reviewed, the person's own strategies), a habit's update, resume and disable, and a habit created with no strategy (tests/test_quest_client_spec_habit_and_map_writes.py). | [`quest_ai_runner/body/area_body.py`](body/area_body.py) | selected |
| Composition | `quest-consciousness:composition` | One Quest user's alignment: each line is handed to the member that owns it. | [`quest_ai_runner/consciousness/flow.py`](consciousness/flow.py) | selected |
| Composition | `quest-intelligence:composition` | A request read against the person's intents, its context chosen, thought through and held to a measure. | [`quest_ai_runner/intelligence/flow.py`](intelligence/flow.py) | selected |
| Composition | `quest-world:composition` | Research and reach: the outside world answered from public research, and a request placed inside or outside the readable sources. | [`quest_ai_runner/world/flow.py`](world/flow.py) | selected |
| QAR as shipped, live on the subscription (today) | `quest-self:qar_live` | The orchestrator built the way production builds it, with the claude CLI backend on the cheapest tier, the real tool catalogue and the real send command, over the placeholder account. | [`quest_ai_runner/intelligence/mind/orchestrator.py`](intelligence/mind/orchestrator.py) |  |
| Scripted ideal planner (offline) | `quest-self:reference_script` | The same orchestrator and tool catalogue with a planner that replays each case's ideal decisions and reply. | no code yet |  |
| Rules only, no model (floor) | `quest-self:rules_only` | No orchestrator: a few message shapes become a note, a goal or a queued task, everything else gets a fixed non-answer, and the terminal prints the fixed Queued line. | no code yet |  |
| One turn of the orchestrator over the model | `quest-self:orchestrator_turn` | The loop plans, gathers, re-plans and answers one message over the model seat, with no cards behind it, and returns the reply, how the loop ended and what ran. | [`quest_ai_runner/intelligence/mind/area_mind.py`](intelligence/mind/area_mind.py) |  |
| The shipped path as one call | `quest-self:shipped_path` | Answers a message with one turn of the loop over the model and returns the turn whole, which is the path the end-to-end metric measures. | [`quest_ai_runner/intelligence/mind/area_mind.py`](intelligence/mind/area_mind.py) |  |

## Metrics

- Task lifecycle correctness over offline cases (`metric-run-queued-tasks`): leader `qar_runner`, 43 of 43 cases
- Requests held to their ESOI metric by code (`metric-think-critically`): leader `every_request_measured`, 7 of 12 cases
- Retrieval recall of the right document over handbook questions (`metric-gather-evidence`): leader `composite`, 19 of 21 cases
- Combined world score (`metric-claim-reach`): leader `scripted_ideal`, 12 of 12 cases
- Ways in that pass their own metric (`metric-take-in-the-world`)
- Parity of Quest operations across access points (`metric-quest-operations-parity`)

## Problems

- Different Science Evidence In Two Chats (`prob-science-search-duplicates-research-chat`): One science question can get different evidence in Quest chat than in Quest's research chat.
- Different Answers On Different Surfaces (`QP4`): The same request can get a different answer depending on the Quest surface it came from.
- Parts Pass While The Assistant Fails (`prob-assistant-not-jarvis-from-the-users-seat`): Each part of QAR can pass its own test while the assistant, as a person meets it across a day, forgets what it knows, acts unasked, claims work it did not do, or answers a vague request with a generic plan, and the person is the one who finds out.

## Ideals

- Consistent Science Evidence (`ideal-science-through-one-search`): A science question gets the same evidence, weighed the same way, in Quest chat and in the research chat.
- Same Assistant Everywhere (`QI4`): A person gets the same assistant, with the same answer to the same request, on every Quest surface.
- Capable From The User's Seat (`ideal-jarvis-from-the-users-seat`): The assistant is judged as the person meets it across a day: one capable assistant that gets real work done in the person's own quests, asks before it does anything outward or hard to undo, and says only what it did.
- Present And Prompt (`ideal-jarvis-j01-always-there-and-fast`): The assistant answers whenever the person asks, and quickly enough that asking it is easier than doing the thing alone.

## Where to go next

- Intent: [Quest AI Runner](../esoi/README.md)
- Down: [quest_ai_runner/body/configuration](body/configuration/README.md)
- Down: [quest_ai_runner/consciousness/feedback_learning](consciousness/feedback_learning/README.md)
- Down: [quest_ai_runner/consciousness/intent_evolution](consciousness/intent_evolution/README.md)
- Down: [quest_ai_runner/consciousness/prioritization](consciousness/prioritization/README.md)
- Down: [quest_ai_runner/consciousness/representatives](consciousness/representatives/README.md)
- Down: [quest_ai_runner/intelligence/nervous_system](intelligence/nervous_system/README.md)
- Down: [quest_ai_runner/intelligence/proactivity](intelligence/proactivity/README.md)
- Down: [quest_ai_runner/body/internal_context/card_store](body/internal_context/card_store/README.md)
- Down: [quest_ai_runner/intelligence/critical_thinking/esoi_application](intelligence/critical_thinking/esoi_application/README.md)
- Down: [quest_ai_runner/intelligence/mind/planner](intelligence/mind/planner/README.md)
- Down: [quest_ai_runner/intelligence/mind/response_writer](intelligence/mind/response_writer/README.md)
- Down: [quest_ai_runner/intelligence/subconscious/cards_from_files](intelligence/subconscious/cards_from_files/README.md)
- Down: [quest_ai_runner/intelligence/subconscious/intuition](intelligence/subconscious/intuition/README.md)
- Down: [quest_ai_runner/world/model_plumbing/model_tier_chooser](world/model_plumbing/model_tier_chooser/README.md)
- Down: [quest_ai_runner/world/model_plumbing/vendor_model_caller](world/model_plumbing/vendor_model_caller/README.md)
- Down: [quest_ai_runner/world/tools/connected_system_operations](world/tools/connected_system_operations/README.md)
- Down: [quest_ai_runner/world/tools/delegated_agent_sessions](world/tools/delegated_agent_sessions/README.md)
- Down: [quest_ai_runner/world/tools/file_editing](world/tools/file_editing/README.md)
- Down: [quest_ai_runner/world/tools/web_search](world/tools/web_search/README.md)
- Down: [quest_ai_runner/intelligence/higher_mind/objectives/place_on_a_quest](intelligence/higher_mind/objectives/place_on_a_quest/README.md)
- Repository: [top README](../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
