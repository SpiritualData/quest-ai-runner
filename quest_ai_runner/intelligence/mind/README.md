# Mind

**Vision statement:** Each request is thought through step by step from everything its work has established so far.

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > Mind

This sub-intent arises as the implementation `the-mind:rebuilt_per_step` of its parent intent [`intelligence`](../README.md). Opportunities: The loop's prompt can be rebuilt for each step from a template, cached head and context layers, the newest observations in full and older ones in one line, so a step's input stays small while the final answer still sees everything. That opens questions the parent solution does not ask: how much of the earlier steps a planner must see to keep continuity (from the second step the selected arm replaces the transcript with a short note that refers to step one, and its continuity is unmeasured), how a person is kept informed while a turn works (a first line, interim replies, the trail of what was done) without paying a model call for every line, and how a request that worked before skips the loop by replaying its known steps.

## How the solutions fit together

Mind works one request in steps, each building on what the earlier ones established. Context Intake takes in what was prepared for the request and what workers brought back, as one view. Planner decides where the request goes, whether to answer it, use a tool or hand it to deep work. Replay Known Operations repeats a request that worked before without planning it again. Quest Work Shown tells the person what the assistant is doing. Reply In Steps sends an interim message when an attempt has not met the goal. Show Progress chooses what the person sees of a run by how they follow it.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- Context Intake (solution with no page of its own, its implementations are in this intent's records)
- Planner (solution with no page of its own, its implementations are in this intent's records)
- Quest Work Shown (solution with no page of its own, its implementations are in this intent's records): Let a person see what their AI is doing for them and what an answer rests on.
- Replay Known Operations (solution with no page of its own, its implementations are in this intent's records): Speeds up a quest's repeated operations by recognising a request that worked before and replaying the known steps.
- Reply In Steps (solution with no page of its own, its implementations are in this intent's records): Lets a person see progress and the reason for more work during a long turn without being spammed.
- Show Progress (solution with no page of its own, its implementations are in this intent's records): Lets a person follow a run at the level they want, every step while they watch and only milestones once they step away.
- Route a Request (solution with no page of its own, its implementations are in this intent's records)
- Resolve a Reference (solution with no page of its own, its implementations are in this intent's records)

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Context Intake | `context-intake` | `quest_ai_runner.run("context-intake", 'What should I work on first this week?')` | The planner prompt written by the real loop (`planner_prompt`) | Prompt size against budget and duplicated-block count, Dashboard suggestion answers that are right and useful |
| Planner | `planner` | `quest_ai_runner.run("planner", 'Save a note that the launch moves to Friday.')` | `MessageRouting` | none yet |
| Quest Work Shown | `quest-work-shown` | `quest_ai_runner.run("quest-work-shown")` | The Trail (live line, one summary line, expandable groups) (`trail`) | Faithful explanations per unit of cost and time, What is shown of the AI's work is true, the same for everyone, and earns its place, The terminal shows the AI's work with the hub's rules and words, and keeps no copy, Supported first beats within the time and cost the turn allows |
| Replay Known Operations | `replay-known-operations` | `quest_ai_runner.run("replay-known-operations", 'Add a note that the launch moves to Friday')` | The recipe store of the loop (`stored_recipes`) | Fast-path hit rate and parity with the planner-only path |
| Reply In Steps | `reply-in-steps` | `quest_ai_runner.run("reply-in-steps")` | The structural gate of core/interim_policy.py (`gated`) | Interim messages and asks that are true, not repeated and only where a person is needed |
| Show Progress | `show-progress` | `quest_ai_runner.run("show-progress", {'event': 'milestone', 'mode': 'background'})` | Stream, milestone and fan-out sinks (`stream`) | Surfaced-event policy by mode, Progress shown while the work runs |
| Route a Request | `route-a-request` | no entry declared | Route read off the planner decision (`planner_derived`) | Requests routed to what they ask for |
| Resolve a Reference | `resolve-a-reference` | no entry declared | none selected | none yet |

### Top implementations of Context Intake

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Live assembly-point driver (`live`) | 97% (28 of 29 cases) | free |  |
| 2 | Planner prompt with only the rules that apply to the turn (`situational`) | 97% (28 of 29 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Prompt size against budget and duplicated-block count. Characters per assembly point against a budget derived from the cheapest tier's window, a stated share and the cost per call (evaluation/components/prompt_size/budgets.py, inputs in budget_inputs.json), duplicated-block count (zero required), healthy, incident and production-sized fixtures for the planner, autopilot batch, context-update bundle and deep prompt; rows report headroom; zero model calls The shares of the window and the dollar caps are policy stated in budget_inputs.json for a human to confirm, and the fixtures are generated filler at incident-measured sizes, not real content.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Planner

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | `single_prompt` | 96% (52 of 54 cases) | $0.0011 |  |
| 2 | Full planner decision (`qar_planner`) | 91% (49 of 54 cases) | $0.0051 |  |
| 3 | `pre_route_then_planner` | 89% (48 of 54 cases) | $0.0015 |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

1 more compared; the full list is in the results log.

**How it is scored:** Routing correctness over the classification cases. The router's whole job over the classification cases, a share held out: route (answer, tool, deep, confirm, clarify), the tool, the arguments it fills in, every tool of a multi-step request, no confirming argument on a first destructive ask, no tool call where no change was asked Read in this direction: maximise correct route, tool and argument choice over labelled cases; minimise deep runs launched for plain questions, destructive requests sent without confirmation, and tool calls made where no change was asked; minimise model spend per routed message.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Quest Work Shown

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | One model call writes the explanation from the trace (today) (`model_explanation`) | score 0.79 (9 of 10 cases) | $0.0095 |  |
| 2 | Template over the trace, no model call (floor, proposed) (`trace_template`) | score 0.72 (7 of 10 cases) | $0.0080 |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Faithful explanations per unit of cost and time. offline cases of a turn's trace with the explanation a model wrote: maximise the share of explanations whose every step is in the trace and which omit no step that changed the answer (value), at a cost per explanation below a stated share of the turn's cost and with no time added before the reply.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Replay Known Operations

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Offline planner-only baseline (`planner_only_offline`) | 100% (30 of 30 cases) | free |  |
| 2 | Offline recipe replay arm (`recipe_replay_offline`) | 100% (30 of 30 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Fast-path hit rate and parity with the planner-only path. fast-path hit rate and parity, offline arm with a deterministic rule model: cases (right tool and args on a repeat, no lookalike write on a near miss, no tool on chat), share answered by a replayed recipe, model calls vs the planner-only path Read in this direction: match the slow path's correctness while cutting model calls and cost, and raise the share of repeat requests answered by a replayed recipe without any false match.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Reply In Steps

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | The structural gate of core/interim_policy.py (`gated`) | 100% (24 of 24 cases) | free | selected |
| 2 | Behaviour before the build (no interim for an unmet attempt) (`today`) | 58% (14 of 24 cases) | free |  |
| 3 | An interim on every unmet attempt (spam baseline) (`every_attempt`) | 8% (2 of 24 cases) | free |  |

**How it is scored:** Interim messages and asks that are true, not repeated and only where a person is needed. from a turn's record (events, ledger, decision rows), never from reply wording: every interim is followed by another attempt and a later message (more-coming truth), every turn ends with exactly one final or one ask (no dangling end), an ask exists exactly when the case is blocked on a person (precision and recall), no two consecutive messages are alike and count and spacing stay within bounds (delta and bounds); met turns must not get worse (no harm).
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Show Progress

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Sink policy cases (`progress_sinks`) | 100% (14 of 14 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Surfaced-event policy by mode. offline cases: LIVE shows every event in order, BACKGROUND shows only result, decision, milestone, context, tokens and done, mid-run detach, broken stream or callback Scores the sinks, not which events the orchestrator emits.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Route a Request

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | Always information (`majority_floor`) | 25% (26 of 104 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Requests routed to what they ask for. Each request goes to the route a reader says it asks for: information, act, draft or clarify.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Solutions from a library

- [Context Intake](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`context-intake`): The one intake of the Mind. Adopted from the library `hub`.
- [Planner](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`planner`): Breaks a request into pieces, each with the sub-goal it must meet and the worker category that fits it; on a replan it plans again only what failed. Specializes the library `hub`.
- [Response Writer](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`quest-work-shown`): Outputs the plan, the request or the communication the Mind produced, in the form its reader needs and to the quality profile that applies. Specializes the library `hub`.
- [Planner](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`replay-known-operations`): Breaks a request into pieces, each with the sub-goal it must meet and the worker category that fits it; on a replan it plans again only what failed. Specializes the library `hub`.
- [Response Writer](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`reply-in-steps`): Outputs the plan, the request or the communication the Mind produced, in the form its reader needs and to the quality profile that applies. Specializes the library `hub`.
- [Response Writer](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`show-progress`): Outputs the plan, the request or the communication the Mind produced, in the form its reader needs and to the quality profile that applies. Specializes the library `hub`.
- [Route a Request](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`route-a-request`): Decides once what a request asks of the system: to be told something (information), to have something done or changed (act), to have text written and handed back in the reply (draft), or that it cannot be acted on yet (clarify). Adopted from the library `hub`.
- [Resolve a Reference](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/intelligence/mind/README.md) (`resolve-a-reference`): Says which one of a list of candidates a phrase refers to (a person for an owner's name, a goal for a draft's title, a post for a version of it), or abstains. Adopted from the library `hub`.

## Problems and ideals

**Problems**

- Unreadable Work (`prob-progress-unreadable`): A person cannot see what their AI is doing for them: whether background work is moving, or what an answer rests on.
- General Question Answered As A Records Question (`prob-general-question-answered-as-records-question`): A person asks a general question (how to practice, learn or improve something) inside a quest, and the answer is a decline ("your records do not cover this") or a thin line, or reads the question in an everyday sense instead of the quest's own subject.

**Ideals**

- Legible Work (`ideal-legible-progress`): A person can see what their AI is doing for them and what an answer rests on, as much as they need and no more, for the mode they are in.
- Answers In The Quest's Own Sense (`ideal-answers-in-the-sense-of-the-quest`): A general question asked inside a quest is answered in full, from general knowledge, about the quest's own subject, and the answer states its basis once and briefly.

## Metrics and their leaders

- Judge pass rate over the chat quality cases (`metric-answer-a-turn`): leader `reference_replies`, 331 of 331 cases
- Surfaced-event policy by mode (`metric-show-progress`): leader `progress_sinks`, 14 of 14 cases
- Fast-path hit rate and parity with the planner-only path (`metric-replay-known-operations`): leader `planner_only_offline`, 30 of 30 cases
- Prompt size against budget and duplicated-block count (`metric-assemble-a-prompt`): leader `live`, 28 of 29 cases
- Accuracy of core ML tasks over labelled cases (`metric-core-ml-tasks`): leader `labelled_rows`, 3 of 11 cases
- Faithful explanations per unit of cost and time (`metric-explain-an-answer`): leader `model_explanation`, 9 of 10 cases
- Whole-path turns right on a small mixed set (`metric-answer-end-to-end`): leader `reference_script`, 13 of 13 cases
- Quest answers that name the person's goal and circumstance and fit features to them (`metric-quest-answer-tied-to-goal-and-circumstance`): leader `reference_script`, 13 of 13 cases
- Day-with-Jarvis turns right end to end (`metric-jarvis-ness`): leader `reference_script`, 13 of 13 cases
- Asks filed and deletes held before anything outward or irreversible happens (`metric-jarvis-floor-consent`): leader `reference_script`, 13 of 13 cases
- and 9 more: all 19 are in [metrics](#metrics) of the full record below

## Where to go next

- Parent intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/intelligence/mind/planner](planner/README.md), [quest_ai_runner/intelligence/mind/response_writer](response_writer/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Unreadable Work (`prob-progress-unreadable`): A person cannot see what their AI is doing for them: whether background work is moving, or what an answer rests on.
- General Question Answered As A Records Question (`prob-general-question-answered-as-records-question`): A person asks a general question (how to practice, learn or improve something) inside a quest, and the answer is a decline ("your records do not cover this") or a thin line, or reads the question in an everyday sense instead of the quest's own subject.

### Ideals

- Legible Work (`ideal-legible-progress`): A person can see what their AI is doing for them and what an answer rests on, as much as they need and no more, for the mode they are in.
- Answers In The Quest's Own Sense (`ideal-answers-in-the-sense-of-the-quest`): A general question asked inside a quest is answered in full, from general knowledge, about the quest's own subject, and the answer states its basis once and briefly.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Context Intake | `context-intake` | The planner prompt written by the real loop (`planner_prompt`) | Prompt size against budget and duplicated-block count, Dashboard suggestion answers that are right and useful |
| Planner | `planner` | `MessageRouting` | none yet |
| Quest Work Shown | `quest-work-shown` | The Trail (live line, one summary line, expandable groups) (`trail`) | Faithful explanations per unit of cost and time, What is shown of the AI's work is true, the same for everyone, and earns its place, The terminal shows the AI's work with the hub's rules and words, and keeps no copy, Supported first beats within the time and cost the turn allows |
| Replay Known Operations | `replay-known-operations` | The recipe store of the loop (`stored_recipes`) | Fast-path hit rate and parity with the planner-only path |
| Reply In Steps | `reply-in-steps` | The structural gate of core/interim_policy.py (`gated`) | Interim messages and asks that are true, not repeated and only where a person is needed |
| Show Progress | `show-progress` | Stream, milestone and fan-out sinks (`stream`) | Surfaced-event policy by mode, Progress shown while the work runs |
| Route a Request | `route-a-request` | Route read off the planner decision (`planner_derived`) | Requests routed to what they ask for |
| Resolve a Reference | `resolve-a-reference` | none selected | none yet |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Full planner decision | `planner:qar_planner` | [evaluation/components/classification/impls/qar_planner.py](../../../evaluation/components/classification/impls/qar_planner.py) |  |
| Keyword rules floor | `planner:keyword_rules` | [evaluation/components/classification/impls/keyword_rules.py](../../../evaluation/components/classification/impls/keyword_rules.py) |  |
| Stream, milestone and fan-out sinks | `show-progress:stream` | [quest_ai_runner/consciousness/terminal/adapters_sinks.py](../../consciousness/terminal/adapters_sinks.py) |  |
| Sink policy cases | `show-progress:progress_sinks` | [evaluation/components/show_progress/impls/progress_sinks.py](../../../evaluation/components/show_progress/impls/progress_sinks.py) |  |
| Live recipe replay arm | `replay-known-operations:recipe_replay` | [evaluation/components/recipe_fast_path/impls/recipe_replay.py](../../../evaluation/components/recipe_fast_path/impls/recipe_replay.py) |  |
| Live planner-only baseline | `replay-known-operations:planner_only` | [evaluation/components/recipe_fast_path/impls/planner_only.py](../../../evaluation/components/recipe_fast_path/impls/planner_only.py) |  |
| Offline recipe replay arm | `replay-known-operations:recipe_replay_offline` | [evaluation/components/recipe_fast_path/impls/recipe_replay_offline.py](../../../evaluation/components/recipe_fast_path/impls/recipe_replay_offline.py) |  |
| Offline planner-only baseline | `replay-known-operations:planner_only_offline` | [evaluation/components/recipe_fast_path/impls/planner_only_offline.py](../../../evaluation/components/recipe_fast_path/impls/planner_only_offline.py) |  |
| Live assembly-point driver | `context-intake:live` | [evaluation/components/prompt_size/impls/live.py](../../../evaluation/components/prompt_size/impls/live.py) |  |
| Milestone sink | `show-progress:milestone` | [quest_ai_runner/consciousness/terminal/adapters_sinks.py](../../consciousness/terminal/adapters_sinks.py) |  |
| Classify through the message router alternatives | `planner:message-route-family` | evaluation/components/classification/impls |  |
| Route with the registry of examples, by model tier | `planner:registry-conditioned-routing` |  |  |
| Fixed status line, no model call (floor, proposed) | `quest-work-shown:no_beat` |  |  |
| One model call writes the explanation from the trace (today) | `quest-work-shown:model_explanation` | [quest_ai_runner/intelligence/critical_thinking/goal_completion/orchestrator_judges.py](../critical_thinking/goal_completion/orchestrator_judges.py) |  |
| Template over the trace, no model call (floor, proposed) | `quest-work-shown:trace_template` |  |  |
| Measured only on labelled rows | `planner:labelled_rows` | [evaluation/components/core_ml_tasks/impls/labelled_rows.py](../../../evaluation/components/core_ml_tasks/impls/labelled_rows.py) |  |
| No task mapped to a measure | `planner:none_mapped` | [evaluation/components/core_ml_tasks/impls/none_mapped.py](../../../evaluation/components/core_ml_tasks/impls/none_mapped.py) |  |
| Planner prompt with only the rules that apply to the turn | `context-intake:situational` | [evaluation/components/prompt_size/impls/situational.py](../../../evaluation/components/prompt_size/impls/situational.py) |  |
| Nothing shown (the status quo before the Trail) | `quest-work-shown:no_trace` | [evaluation/components/work_trace_experience/impls/no_trace.py](../../../evaluation/components/work_trace_experience/impls/no_trace.py) |  |
| A plain list of every step | `quest-work-shown:plain_list` | [evaluation/components/work_trace_experience/impls/plain_list.py](../../../evaluation/components/work_trace_experience/impls/plain_list.py) |  |
| Behaviour before the build (no interim for an unmet attempt) | `reply-in-steps:today` | [evaluation/components/interim_reply/impls/today.py](../../../evaluation/components/interim_reply/impls/today.py) |  |
| An interim on every unmet attempt (spam baseline) | `reply-in-steps:every_attempt` | [evaluation/components/interim_reply/impls/every_attempt.py](../../../evaluation/components/interim_reply/impls/every_attempt.py) |  |
| The terminal draws the Trail from the hub's renderers | `quest-work-shown:terminal_trail` | [quest_ai_runner/consciousness/terminal/textual_ui_trail.py](../../consciousness/terminal/textual_ui_trail.py) |  |
| The planner prompt written by the real loop | `context-intake:planner_prompt` | [quest_ai_runner/intelligence/mind/area_mind.py](area_mind.py) |  |
| The recipe store of the loop | `replay-known-operations:stored_recipes` | [quest_ai_runner/intelligence/mind/area_mind.py](area_mind.py) |  |
| Route read off the planner decision | `route-a-request:planner_derived` | [quest_ai_runner/intelligence/mind/request_router/planner_derived.py](request_router/planner_derived.py) |  |
| One cheap classifier call | `route-a-request:single_classifier` | [evaluation/components/route_a_request/impls/single_classifier.py](../../../evaluation/components/route_a_request/impls/single_classifier.py) |  |
| Always information | `route-a-request:majority_floor` | [evaluation/components/route_a_request/impls/majority_floor.py](../../../evaluation/components/route_a_request/impls/majority_floor.py) |  |
| Run facts relay over the hub's data form (producer of the run-state ticks) | `quest-work-shown:run_facts` | [quest_ai_runner/intelligence/mind/delegation/run_facts.py](delegation/run_facts.py) |  |
| Resume the stored session (the person's short message) | `receive-input-mid-run:resume_session` | [quest_ai_runner/intelligence/mind/delegation/resume_request.py](delegation/resume_request.py) |  |
| The loop planner decides the route | `planner:planner_route` | [quest_ai_runner/intelligence/mind/planner/message_routing.py](planner/message_routing.py) |  |
| One cheap model call per turn writes the beat (today) | `quest-work-shown:model_beat` | [quest_ai_runner/intelligence/mind/response_writer/orchestrator_turn.py](response_writer/orchestrator_turn.py) |  |
| The Trail (live line, one summary line, expandable groups) | `quest-work-shown:trail` | [quest_ai_runner/intelligence/mind/response_writer/work_trace.py](response_writer/work_trace.py) |  |
| The structural gate of core/interim_policy.py | `reply-in-steps:gated` | [quest_ai_runner/intelligence/mind/response_writer/interim_policy.py](response_writer/interim_policy.py) |  |
| First message in plain words, read from goal and milestone records only | `reply-in-steps:plain-status-from-records` | [quest_ai_runner/intelligence/mind/response_writer/act_status.py](response_writer/act_status.py) |  |

### Metrics

- Judge pass rate over the chat quality cases (`metric-answer-a-turn`): `esoi run chat_quality --impl dev_quest_chat`
- Surfaced-event policy by mode (`metric-show-progress`): `esoi run show_progress --impl progress_sinks`
- Fast-path hit rate and parity with the planner-only path (`metric-replay-known-operations`): `esoi run recipe_fast_path --impl recipe_replay_offline`
- Prompt size against budget and duplicated-block count (`metric-assemble-a-prompt`): `esoi run prompt_size --impl live`
- Accuracy of core ML tasks over labelled cases (`metric-core-ml-tasks`): `esoi run core_ml_tasks --impl labelled_rows; esoi run classification --impl qar_planner`
- Faithful explanations per unit of cost and time (`metric-explain-an-answer`): `esoi run explain_an_answer --impl model_explanation`
- Whole-path turns right on a small mixed set (`metric-answer-end-to-end`): `esoi run end_to_end --impl qar_live --sample 3 --max-calls 60`
- Quest answers that name the person's goal and circumstance and fit features to them (`metric-quest-answer-tied-to-goal-and-circumstance`): `esoi run end_to_end --impl qar_live`
- Day-with-Jarvis turns right end to end (`metric-jarvis-ness`): `esoi run end_to_end --impl qar_live`
- Asks filed and deletes held before anything outward or irreversible happens (`metric-jarvis-floor-consent`): `esoi run end_to_end --impl qar_live`
- Replies that claim only what was done (`metric-jarvis-floor-honesty`): `esoi run end_to_end --impl qar_live`
- Progress shown while the work runs (`metric-incremental-output-and-still-working`): `esoi run show_progress --impl progress_sinks`
- What is shown of the AI's work is true, the same for everyone, and earns its place (`metric-work-trace-experience`): `esoi run work_trace_experience --impl trail`
- Interim messages and asks that are true, not repeated and only where a person is needed (`metric-interim-reply-truthful-and-useful`): `esoi run interim_reply --impl gated`
- The terminal shows the AI's work with the hub's rules and words, and keeps no copy (`metric-terminal-uses-hub-renderers`): `python3 -m pytest tests/test_terminal_trail.py tests/test_terminal_uses_hub_renderers.py`
- Supported first beats within the time and cost the turn allows (`metric-narrate-a-turn`): `esoi run narrate_a_turn --impl model_beat`
- Requests routed to what they ask for (`metric-route-a-request`): `esoi run route_a_request --impl planner_derived --sample 3 --max-calls 3; esoi run route_a_request --impl single_classifier --sample 3 --max-calls 3; esoi run route_a_request --impl majority_floor`
- The terminal meets the requirements of working with the AI while it works (`metric-terminal-requirements`): `python3 -m pytest -s tests/test_terminal_requirements.py`
- Dashboard suggestion answers that are right and useful (`metric-dashboard-suggestion-answers`): `python -m pytest -q tests/test_status_snapshot.py`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
