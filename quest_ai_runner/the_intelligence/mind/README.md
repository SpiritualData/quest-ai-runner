# Mind

**Vision statement:** Each request is thought through step by step from everything its work has established so far.

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > Mind

This sub-intent arises as the implementation `the-mind:rebuilt_per_step` of its parent intent [`the_intelligence`](../README.md). Opportunities: The loop's prompt can be rebuilt for each step from a template, cached head and context layers, the newest observations in full and older ones in one line, so a step's input stays small while the final answer still sees everything. That opens questions the parent solution does not ask: how much of the earlier steps a planner must see to keep continuity (from the second step the selected arm replaces the transcript with a short note that refers to step one, and its continuity is unmeasured), how a person is kept informed while a turn works (a first line, interim replies, the trail of what was done) without paying a model call for every line, and how a request that worked before skips the loop by replaying its known steps.

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

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Context Intake | `context-intake` | `quest_ai_runner.run("context-intake", 'What should I work on first this week?')` | The planner prompt written by the real loop (`planner_prompt`) | Prompt size against budget and duplicated-block count |
| Planner | `planner` | `quest_ai_runner.run("planner", 'Save a note that the launch moves to Friday.')` | The loop planner decides the route (`planner_route`) | Routing correctness over the classification cases, Accuracy of core ML tasks over labelled cases |
| Quest Work Shown | `quest-work-shown` | `quest_ai_runner.run("quest-work-shown")` | The Trail (live line, one summary line, expandable groups) (`trail`) | Faithful explanations per unit of cost and time, What is shown of the AI's work is true, the same for everyone, and earns its place, The terminal shows the AI's work with the hub's rules and words, and keeps no copy, Supported first beats within the time and cost the turn allows |
| Replay Known Operations | `replay-known-operations` | `quest_ai_runner.run("replay-known-operations", 'Add a note that the launch moves to Friday')` | The recipe store of the loop (`stored_recipes`) | Fast-path hit rate and parity with the planner-only path |
| Reply In Steps | `reply-in-steps` | `quest_ai_runner.run("reply-in-steps")` | The structural gate of core/interim_policy.py (`gated`) | Interim messages and asks that are true, not repeated and only where a person is needed |
| Show Progress | `show-progress` | `quest_ai_runner.run("show-progress", {'event': 'milestone', 'mode': 'background'})` | Stream, milestone and fan-out sinks (`stream`) | Surfaced-event policy by mode, Progress shown while the work runs |

## Solutions from a library

- [Context Intake](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/mind/README.md) (`context-intake`): The one intake of the Mind. Adopted from the library `hub`.
- [Planner](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/mind/README.md) (`planner`): Breaks a request into pieces, each with the sub-goal it must meet and the worker category that fits it; on a replan it plans again only what failed. Adopted from the library `hub`.
- [Response Writer](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/mind/README.md) (`quest-work-shown`): Outputs the plan, the request or the communication the Mind produced, in the form its reader needs and to the quality profile that applies. Specializes the library `hub`.
- [Planner](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/mind/README.md) (`replay-known-operations`): Breaks a request into pieces, each with the sub-goal it must meet and the worker category that fits it; on a replan it plans again only what failed. Specializes the library `hub`.
- [Response Writer](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/mind/README.md) (`reply-in-steps`): Outputs the plan, the request or the communication the Mind produced, in the form its reader needs and to the quality profile that applies. Specializes the library `hub`.
- [Response Writer](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/mind/README.md) (`show-progress`): Outputs the plan, the request or the communication the Mind produced, in the form its reader needs and to the quality profile that applies. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Unreadable Work (`prob-progress-unreadable`): A person cannot see what their AI is doing for them: whether background work is moving, or what an answer rests on.

**Ideals**

- Legible Work (`ideal-legible-progress`): A person can see what their AI is doing for them and what an answer rests on, as much as they need and no more, for the mode they are in.

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
- and 6 more: all 16 are in [metrics](#metrics) of the full record below

## Where to go next

- Parent intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_intelligence/mind/planner](planner/README.md), [quest_ai_runner/the_intelligence/mind/response_writer](response_writer/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Unreadable Work (`prob-progress-unreadable`): A person cannot see what their AI is doing for them: whether background work is moving, or what an answer rests on.

### Ideals

- Legible Work (`ideal-legible-progress`): A person can see what their AI is doing for them and what an answer rests on, as much as they need and no more, for the mode they are in.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Context Intake | `context-intake` | The planner prompt written by the real loop (`planner_prompt`) | Prompt size against budget and duplicated-block count |
| Planner | `planner` | The loop planner decides the route (`planner_route`) | Routing correctness over the classification cases, Accuracy of core ML tasks over labelled cases |
| Quest Work Shown | `quest-work-shown` | The Trail (live line, one summary line, expandable groups) (`trail`) | Faithful explanations per unit of cost and time, What is shown of the AI's work is true, the same for everyone, and earns its place, The terminal shows the AI's work with the hub's rules and words, and keeps no copy, Supported first beats within the time and cost the turn allows |
| Replay Known Operations | `replay-known-operations` | The recipe store of the loop (`stored_recipes`) | Fast-path hit rate and parity with the planner-only path |
| Reply In Steps | `reply-in-steps` | The structural gate of core/interim_policy.py (`gated`) | Interim messages and asks that are true, not repeated and only where a person is needed |
| Show Progress | `show-progress` | Stream, milestone and fan-out sinks (`stream`) | Surfaced-event policy by mode, Progress shown while the work runs |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Full planner decision | `planner:qar_planner` | [evaluation/components/classification/impls/qar_planner.py](../../../evaluation/components/classification/impls/qar_planner.py) |  |
| Keyword rules floor | `planner:keyword_rules` | [evaluation/components/classification/impls/keyword_rules.py](../../../evaluation/components/classification/impls/keyword_rules.py) |  |
| Stream, milestone and fan-out sinks | `show-progress:stream` | [quest_ai_runner/the_consciousness/terminal/adapters_sinks.py](../../the_consciousness/terminal/adapters_sinks.py) |  |
| Sink policy cases | `show-progress:progress_sinks` | [evaluation/components/show_progress/impls/progress_sinks.py](../../../evaluation/components/show_progress/impls/progress_sinks.py) |  |
| Live recipe replay arm | `replay-known-operations:recipe_replay` | [evaluation/components/recipe_fast_path/impls/recipe_replay.py](../../../evaluation/components/recipe_fast_path/impls/recipe_replay.py) |  |
| Live planner-only baseline | `replay-known-operations:planner_only` | [evaluation/components/recipe_fast_path/impls/planner_only.py](../../../evaluation/components/recipe_fast_path/impls/planner_only.py) |  |
| Offline recipe replay arm | `replay-known-operations:recipe_replay_offline` | [evaluation/components/recipe_fast_path/impls/recipe_replay_offline.py](../../../evaluation/components/recipe_fast_path/impls/recipe_replay_offline.py) |  |
| Offline planner-only baseline | `replay-known-operations:planner_only_offline` | [evaluation/components/recipe_fast_path/impls/planner_only_offline.py](../../../evaluation/components/recipe_fast_path/impls/planner_only_offline.py) |  |
| Live assembly-point driver | `context-intake:live` | [evaluation/components/prompt_size/impls/live.py](../../../evaluation/components/prompt_size/impls/live.py) |  |
| Milestone sink | `show-progress:milestone` | [quest_ai_runner/the_consciousness/terminal/adapters_sinks.py](../../the_consciousness/terminal/adapters_sinks.py) |  |
| Classify through the message router alternatives | `planner:message-route-family` | evaluation/components/classification/impls |  |
| Route with the registry of examples, by model tier | `planner:registry-conditioned-routing` |  |  |
| Fixed status line, no model call (floor, proposed) | `quest-work-shown:no_beat` |  |  |
| One model call writes the explanation from the trace (today) | `quest-work-shown:model_explanation` | [quest_ai_runner/the_intelligence/critical_thinking/goal_completion/orchestrator_judges.py](../critical_thinking/goal_completion/orchestrator_judges.py) |  |
| Template over the trace, no model call (floor, proposed) | `quest-work-shown:trace_template` |  |  |
| Measured only on labelled rows | `planner:labelled_rows` | [evaluation/components/core_ml_tasks/impls/labelled_rows.py](../../../evaluation/components/core_ml_tasks/impls/labelled_rows.py) |  |
| No task mapped to a measure | `planner:none_mapped` | [evaluation/components/core_ml_tasks/impls/none_mapped.py](../../../evaluation/components/core_ml_tasks/impls/none_mapped.py) |  |
| Planner prompt with only the rules that apply to the turn | `context-intake:situational` | [evaluation/components/prompt_size/impls/situational.py](../../../evaluation/components/prompt_size/impls/situational.py) |  |
| Nothing shown (the status quo before the Trail) | `quest-work-shown:no_trace` | [evaluation/components/work_trace_experience/impls/no_trace.py](../../../evaluation/components/work_trace_experience/impls/no_trace.py) |  |
| A plain list of every step | `quest-work-shown:plain_list` | [evaluation/components/work_trace_experience/impls/plain_list.py](../../../evaluation/components/work_trace_experience/impls/plain_list.py) |  |
| Behaviour before the build (no interim for an unmet attempt) | `reply-in-steps:today` | [evaluation/components/interim_reply/impls/today.py](../../../evaluation/components/interim_reply/impls/today.py) |  |
| An interim on every unmet attempt (spam baseline) | `reply-in-steps:every_attempt` | [evaluation/components/interim_reply/impls/every_attempt.py](../../../evaluation/components/interim_reply/impls/every_attempt.py) |  |
| The terminal draws the Trail from the hub's renderers | `quest-work-shown:terminal_trail` | [quest_ai_runner/the_consciousness/terminal/textual_ui_trail.py](../../the_consciousness/terminal/textual_ui_trail.py) |  |
| The planner prompt written by the real loop | `context-intake:planner_prompt` | [quest_ai_runner/the_intelligence/mind/area_mind.py](area_mind.py) |  |
| The recipe store of the loop | `replay-known-operations:stored_recipes` | [quest_ai_runner/the_intelligence/mind/area_mind.py](area_mind.py) |  |
| The loop planner decides the route | `planner:planner_route` | [quest_ai_runner/the_intelligence/mind/planner/message_routing.py](planner/message_routing.py) |  |
| One cheap model call per turn writes the beat (today) | `quest-work-shown:model_beat` | [quest_ai_runner/the_intelligence/mind/response_writer/orchestrator_turn.py](response_writer/orchestrator_turn.py) |  |
| The Trail (live line, one summary line, expandable groups) | `quest-work-shown:trail` | [quest_ai_runner/the_intelligence/mind/response_writer/work_trace.py](response_writer/work_trace.py) |  |
| The structural gate of core/interim_policy.py | `reply-in-steps:gated` | [quest_ai_runner/the_intelligence/mind/response_writer/interim_policy.py](response_writer/interim_policy.py) |  |

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


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
