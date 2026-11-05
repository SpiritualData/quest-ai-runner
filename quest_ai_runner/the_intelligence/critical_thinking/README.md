# Critical Thinking

Everything the AI does and says is continuously held to the metric that serves the person's goals.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `critical-thinking:deep_runs_only` of its parent intent [`the_intelligence`](../README.md). Opportunities: A deep run can be held to the metric that serves its goal, and so can every other request, which is wider than the parent solution asks. That opens questions of its own: slicing the specification a task concerns before the run and writing back what the run learned, choosing the records an input concerns by lexical, vector and gated search, settling a narrow question with one guarded model call and a safe default, stopping unverified links and recognising repeated requests, telling a stuck run from a slow one, drafting a metric for a task that has none, and keeping background work to the days, hours and budget a person set.

## How the solutions fit together

Critical Thinking holds a request to a measure. Metric Translation turns the request into a metric with a target. ESOI Application finds the specifications the request concerns and works to them, so the work is checked against the person's standard. Quest Autopilot decides whether work that nobody asked for may start now, from the person's own days, hours and budget.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| ESOI Application | `esoi-application` | `quest_ai_runner.run("esoi-application", 'Tidy the card store')` | Slice before and write-back after (`slice-and-writeback`) | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed, Principle-following pass rate under standing instructions |
| Metric Translation | `metric-translation` | `quest_ai_runner.run("metric-translation", ...)` | Capped model draft per task (today) (`capped_model_draft`) | Drafted metrics accepted per unit of cost |
| Quest Autopilot | `quest-autopilot` | `quest_ai_runner.run("quest-autopilot", 'quest-id')` | Scripted-client gate evaluation arm (`live_pass`) | Autopilot gate decision correctness |

## Solutions from a library

- [ESOI Application](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`esoi-application`): Creates and edits specs and metrics, applies the structure's principles and runs the iterative improvement loop, so what a request needs becomes a measured specification rather than a longer prompt. Adopted from the library `hub`.
- [Metric Translation](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`metric-translation`): Turns a request into a metric with a target value or an improvement, adjustable in real time, and applies the same step recursively to each sub-goal, rolling each up to its parent objective. Adopted from the library `hub`.
- [Proactivity](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/README.md) (`quest-autopilot`): Says which of the system's standing schedules are due, by calendar period in their own zone, by a requested run or by an event since the last run, so the system acts without being asked. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Unwanted Background Work (`prob-autopilot-works-when-unwanted`): Recurring background passes can work when the person's settings say they should not: past the daily budget, on a day outside the roster, or without a cadence being due.

**Ideals**

- Permitted Timing (`ideal-permitted-timing`): Background work happens exactly when the person's settings allow and never otherwise.

## Metrics and their leaders

- Stop-or-continue verdict accuracy with false stops counted apart (`metric-detect-a-stalled-run`): leader `rule_floor`, 3 of 3 cases
- Judge deterministic-half correctness with a scripted provider (`metric-judge-a-decision`): leader `qar_judges`, 61 of 61 cases
- Reply guard correctness over link, recipe and anticipation cases (`metric-guard-a-reply`): leader `qar_guards`, 54 of 54 cases
- Autopilot gate decision correctness (`metric-gate-an-autopilot-pass`): leader `live_pass`, 33 of 33 cases
- Intent-grounded pass rate (`metric-ground-in-intent`): leader `frame_line`, 3 of 3 cases
- First-pass acceptance and exploration before the first edit (`metric-iterate-by-spec`): leader `single_attempt`, 8 of 9 cases
- Runs that leave a durable, attributed record of what they changed (`metric-track-runs-on-records`)
- Drafted metrics accepted per unit of cost (`metric-draft-a-metric`): leader `capped_model_draft`, 10 of 10 cases
- Specification selection pass rate (`metric-spec-selection`): leader `lexical_vector_gated`, 70 of 76 cases

## Where to go next

- Parent intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_intelligence/critical_thinking/esoi_application](esoi_application/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Unwanted Background Work (`prob-autopilot-works-when-unwanted`): Recurring background passes can work when the person's settings say they should not: past the daily budget, on a day outside the roster, or without a cadence being due.

### Ideals

- Permitted Timing (`ideal-permitted-timing`): Background work happens exactly when the person's settings allow and never otherwise.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| ESOI Application | `esoi-application` | Slice before and write-back after (`slice-and-writeback`) | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed, Principle-following pass rate under standing instructions |
| Metric Translation | `metric-translation` | Capped model draft per task (today) (`capped_model_draft`) | Drafted metrics accepted per unit of cost |
| Quest Autopilot | `quest-autopilot` | Scripted-client gate evaluation arm (`live_pass`) | Autopilot gate decision correctness |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Production liveness review in the goal runner | `delegated-agent-sessions:goal_runner` | [quest_ai_runner/the_intelligence/mind/delegation/goal_runner.py](../mind/delegation/goal_runner.py) |  |
| Live model reviewer arm | `delegated-agent-sessions:live_reviewer` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Keyword and threshold rule floor | `delegated-agent-sessions:rule_floor` | [evaluation/components/deep_review/impls/rule_floor.py](../../../evaluation/components/deep_review/impls/rule_floor.py) |  |
| Production judges under a scripted provider | `goal-completion:qar_judges` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Production guard engines under test | `response-quality:qar_guards` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Eligibility gate mixin | `quest-autopilot:autopilot_gates` | [quest_ai_runner/the_intelligence/proactivity/autopilot_gates.py](../proactivity/autopilot_gates.py) |  |
| Scripted-client gate evaluation arm | `quest-autopilot:live_pass` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Prompts without the intent frame | `guidance:none` | [quest_ai_runner/the_intelligence/proactivity/autopilot_briefs.py](../proactivity/autopilot_briefs.py) |  |
| Computed frame in the prompt | `guidance:frame-line` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Frame plus intent card and record drill-down | `guidance:frame-card-drilldown` | [quest_ai_runner/the_body/internal_context/quest/client/quest_client_intents.py](../../the_body/internal_context/quest/client/quest_client_intents.py) |  |
| Frame plus a lazily generated quest specification | `guidance:frame-lazy-spec` | [quest_ai_runner/the_body/hands/quest_operations/tools_quest_intents.py](../../the_body/hands/quest_operations/tools_quest_intents.py) |  |
| Generic method in the standing instructions | `esoi-application:generic-doctrine` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice of the specification before the task | `esoi-application:slice-only` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice before and write-back after | `esoi-application:slice-and-writeback` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice before and write-back onto each record (planned) | `esoi-application:writeback-on-the-record` |  |  |
| Capped model draft per task (today) | `metric-translation:capped_model_draft` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Ask the owner to write it (floor, proposed) | `metric-translation:ask_owner` | [evaluation/components/draft_a_metric/impls/ask_owner.py](../../../evaluation/components/draft_a_metric/impls/ask_owner.py) |  |
| No-call floor of the quest ranker and reach judge | `goal-completion:no_call_floor` | [evaluation/components/judge_live_decisions/impls/no_call_floor.py](../../../evaluation/components/judge_live_decisions/impls/no_call_floor.py) |  |
| Live quest ranker and reach judge on a small model | `goal-completion:live_judges` | [evaluation/components/judge_live_decisions/impls/live_judges.py](../../../evaluation/components/judge_live_decisions/impls/live_judges.py) |  |
| Lexical TF-IDF, optional vector search, parallel batched model judging | `context-retriever:lexical-vector-llm` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py](esoi_application/spec_selection.py) |  |
| Lexical top few only | `context-retriever:lexical-only` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py](esoi_application/spec_selection.py) |  |
| Lexical vector gated | `context-retriever:lexical-vector-gated` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py](esoi_application/spec_selection.py) |  |

### Metrics

- Stop-or-continue verdict accuracy with false stops counted apart (`metric-detect-a-stalled-run`): `esoi run deep_review --impl live_reviewer`
- Judge deterministic-half correctness with a scripted provider (`metric-judge-a-decision`): `esoi run judges --impl qar_judges; esoi run judge_live_decisions --impl live_judges`
- Reply guard correctness over link, recipe and anticipation cases (`metric-guard-a-reply`): `esoi run reply_guards --impl qar_guards`
- Autopilot gate decision correctness (`metric-gate-an-autopilot-pass`): `esoi run gate_an_autopilot_pass --impl live_pass`
- Intent-grounded pass rate (`metric-ground-in-intent`): `esoi run intent_grounding --impl frame_line`
- First-pass acceptance and exploration before the first edit (`metric-iterate-by-spec`): `esoi run task_replay --impl slice_and_writeback`
- Runs that leave a durable, attributed record of what they changed (`metric-track-runs-on-records`): `esoi run record_tracking`
- Drafted metrics accepted per unit of cost (`metric-draft-a-metric`): `esoi run draft_a_metric --impl capped_model_draft`
- Specification selection pass rate (`metric-spec-selection`): `esoi run spec_selection --impl lexical_vector_gated; esoi run spec_selection --impl lexical_vector_llm; esoi run spec_selection --impl lexical_vector; esoi run spec_selection --impl lexical_only`
