# Critical Thinking

**Vision statement:** The AI checks its work against what success means for the person.

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > Critical Thinking

This sub-intent arises as the implementation `critical-thinking:deep_runs_only` of its parent intent [`the_intelligence`](../README.md). Opportunities: A deep run can be held to the metric that serves its goal, and so can every other request, which is wider than the parent solution asks. That opens questions of its own: slicing the specification a task concerns before the run and writing back what the run learned, choosing the records an input concerns by lexical, vector and gated search, settling a narrow question with one guarded model call and a safe default, stopping unverified links and recognising repeated requests, telling a stuck run from a slow one, drafting a metric for a task that has none, and keeping background work to the days, hours and budget a person set.

## How the solutions fit together

Critical Thinking holds a request to a measure. Metric Translation turns the request into a metric with a target. A Run Applies ESOI finds the specifications the request concerns and works to them. ESOI Application edits the ESOI records when a change to them is proposed. Goal Completion judges whether the goal was met and not merely claimed. Response Quality guards what a reply says against what is known.

## One level below

This intent is implemented by the sub-intents and solutions below it; each shows whether it goes deeper.

- A Run Applies ESOI (solution with no page of its own, its implementations are in this intent's records): A deep run applies ESOI behaviour: it carries the method in its standing instructions, is shown the slice of the specification its goal serves before the task, and has its metric read before and after and written to the run's record.
- ESOI Application (solution with no page of its own, its implementations are in this intent's records)
- Metric Translation (solution with no page of its own, its implementations are in this intent's records)
- Goal Completion (solution with no page of its own, its implementations are in this intent's records)
- Response Quality (solution with no page of its own, its implementations are in this intent's records)
- Decision Check (solution with no page of its own, its implementations are in this intent's records)

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| A Run Applies ESOI | `run-applies-esoi` | `quest_ai_runner.run("run-applies-esoi", {'request': 'Ship the launch note'})` | Slice before and write-back after (`slice-and-writeback`) | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed, Principle-following pass rate under standing instructions |
| ESOI Application | `esoi-application` | `quest_ai_runner.run("esoi-application", {'change': {}})` | Edit a quest's specification in Quest (`quest_spec_records`) | none yet |
| Metric Translation | `metric-translation` | `quest_ai_runner.run("metric-translation", ...)` | Capped model draft per task (today) (`capped_model_draft`) | Drafted metrics accepted per unit of cost |
| Goal Completion | `goal-completion` | `quest_ai_runner.run("goal-completion", ...)` | Production judges under a scripted provider (`qar_judges`) | Judge deterministic-half correctness with a scripted provider |
| Response Quality | `response-quality` | `quest_ai_runner.run("response-quality", 'See https://example.org/a for details.')` | Production guard engines under test (`qar_guards`) | Reply guard correctness over link, recipe and anticipation cases, Judge agreement with labelled reference replies |
| Decision Check | `decision-check` | no entry declared | none selected | none yet |

## Solutions from a library

- [ESOI Application](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`esoi-application`): Applies a proposed change to the specs and metrics of the system, as an edit to one field of one record and never as a longer prompt, so what a request needs becomes a measured specification. Adopted from the library `hub`.
- [Metric Translation](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`metric-translation`): Turns a request into a metric with a target value or an improvement, adjustable in real time, and applies the same step recursively to each sub-goal, rolling each up to its parent objective. Adopted from the library `hub`.
- [Goal Completion](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`goal-completion`): Says whether the goal condition of a request or sub-goal was met, against the done-standard its metric names. Adopted from the library `hub`.
- [Response Quality](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`response-quality`): Judges a response against a quality profile: a named set of criteria with weights and target values. Adopted from the library `hub`.
- [Decision Check](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_intelligence/critical_thinking/README.md) (`decision-check`): Checks a draft decision (a pick among options, or a reply) by small independent checks, each a short structured call that sees only the slice of evidence it needs, so that a cheap model gives a reliable decision through structure. Adopted from the library `hub`.

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
- Work classification: the deliverable filed under the right solution (`metric-work-classification`)
- and 2 more: all 12 are in [metrics](#metrics) of the full record below

## Where to go next

- Parent intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_intelligence/critical_thinking/esoi_application](esoi_application/README.md)
- Records: [intent.csv](intent.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| A Run Applies ESOI | `run-applies-esoi` | Slice before and write-back after (`slice-and-writeback`) | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed, Principle-following pass rate under standing instructions |
| ESOI Application | `esoi-application` | Edit a quest's specification in Quest (`quest_spec_records`) | none yet |
| Metric Translation | `metric-translation` | Capped model draft per task (today) (`capped_model_draft`) | Drafted metrics accepted per unit of cost |
| Goal Completion | `goal-completion` | Production judges under a scripted provider (`qar_judges`) | Judge deterministic-half correctness with a scripted provider |
| Response Quality | `response-quality` | Production guard engines under test (`qar_guards`) | Reply guard correctness over link, recipe and anticipation cases, Judge agreement with labelled reference replies |
| Decision Check | `decision-check` | none selected | none yet |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Production judges under a scripted provider | `goal-completion:qar_judges` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Production guard engines under test | `response-quality:qar_guards` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Generic method in the standing instructions | `run-applies-esoi:generic-doctrine` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice of the specification before the task | `run-applies-esoi:slice-only` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice before and write-back after | `run-applies-esoi:slice-and-writeback` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice before and write-back onto each record (planned) | `run-applies-esoi:writeback-on-the-record` |  |  |
| Capped model draft per task (today) | `metric-translation:capped_model_draft` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Ask the owner to write it (floor, proposed) | `metric-translation:ask_owner` | [evaluation/components/draft_a_metric/impls/ask_owner.py](../../../evaluation/components/draft_a_metric/impls/ask_owner.py) |  |
| No-call floor of the quest ranker and reach judge | `goal-completion:no_call_floor` | [evaluation/components/judge_live_decisions/impls/no_call_floor.py](../../../evaluation/components/judge_live_decisions/impls/no_call_floor.py) |  |
| Live quest ranker and reach judge on a small model | `goal-completion:live_judges` | [evaluation/components/judge_live_decisions/impls/live_judges.py](../../../evaluation/components/judge_live_decisions/impls/live_judges.py) |  |
| Shipped deep-run prompt with standing instructions | `run-applies-esoi:insitu_standing_instructions` | [evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py](../../../evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py) |  |
| In-situ prompt without the instruction layer | `run-applies-esoi:insitu_plain` | [evaluation/components/esoi_behavior/impls/insitu_plain.py](../../../evaluation/components/esoi_behavior/impls/insitu_plain.py) |  |
| Chat-completion ablation with instructions | `run-applies-esoi:standing_instructions` | [evaluation/components/esoi_behavior/impls/standing_instructions.py](../../../evaluation/components/esoi_behavior/impls/standing_instructions.py) |  |
| Chat-completion baseline without instructions | `run-applies-esoi:plain` | [evaluation/components/esoi_behavior/impls/plain.py](../../../evaluation/components/esoi_behavior/impls/plain.py) |  |
| Rubric-based model judge | `response-quality:model_judge` | [evaluation/components/esoi_judge/impls/model_judge.py](../../../evaluation/components/esoi_judge/impls/model_judge.py) |  |
| Regex-check floor | `response-quality:regex_only` | [evaluation/components/esoi_judge/impls/regex_only.py](../../../evaluation/components/esoi_judge/impls/regex_only.py) |  |
| Edit a quest's specification in Quest | `esoi-application:quest_spec_records` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/quest_spec_edit.py](esoi_application/quest_spec_edit.py) |  |
| Read a metric's current value from its named source | `esoi-application:metric_source` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/tools_metric_source.py](esoi_application/tools_metric_source.py) |  |
| Chat reply held to the turn's reads and executed actions | `decision-check:chat_reply_views` | [quest_ai_runner/the_intelligence/critical_thinking/decision_check/reply_views.py](decision_check/reply_views.py) |  |

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
- Work classification: the deliverable filed under the right solution (`metric-work-classification`): `python evaluation/components/chat_quality/work_parts.py --records <records folder> --dataset <private dataset folder>`
- ESOI application: the deliverable meets its solution's own rubric (`metric-work-esoi-application`): `python evaluation/components/chat_quality/work_parts.py --records <records folder> --dataset <private dataset folder>`
- Harness: the work ran to a deliverable within its budget (`metric-work-harness`): `python evaluation/components/chat_quality/work_parts.py --records <records folder> --dataset <private dataset folder>`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
