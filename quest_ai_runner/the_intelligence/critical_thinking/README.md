# Critical Thinking

Everything the AI does and says is continuously held to the metric that serves the person's goals.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `critical-thinking:deep_runs_only` of its parent intent [`quest-ai-runner`](../../../esoi/README.md). Opportunities: A deep run can be held to the metric that serves its goal, and so can every other request, which is wider than the parent solution asks. That opens questions of its own: slicing the specification a task concerns before the run and writing back what the run learned, choosing the records an input concerns by lexical, vector and gated search, settling a narrow question with one guarded model call and a safe default, stopping unverified links and recognising repeated requests, telling a stuck run from a slow one, drafting a metric for a task that has none, and keeping background work to the days, hours and budget a person set.

## How the solutions fit together

Critical Thinking holds deep runs to their metric through Specification slice and write-back on the records Intent specification selection picks, settles narrow questions through Guarded model-verdict judge, stops unsafe replies through Reply safety and anticipation guards, ties work to its purpose through Intent frame for quest-scoped prompts, tells a stuck run from a slow one through Run liveness reviewer, drafts a missing metric through Metric drafting for a task, and keeps background work inside its hours and budget through Autopilot run-or-skip gate.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Run liveness reviewer | `detect-a-stalled-run` | `quest_ai_runner.run("detect-a-stalled-run", ...)` | Live model reviewer arm (`live_reviewer`) | Stop-or-continue verdict accuracy with false stops counted apart |
| Guarded model-verdict judge | `judge-a-decision` | `quest_ai_runner.run("judge-a-decision", ...)` | Production judges under a scripted provider (`qar_judges`) | Judge deterministic-half correctness with a scripted provider |
| Reply safety and anticipation guards | `guard-a-reply` | `quest_ai_runner.run("guard-a-reply", 'See https://example.org/a for details.')` | Production guard engines under test (`qar_guards`) | Reply guard correctness over link, recipe and anticipation cases |
| Autopilot run-or-skip gate | `gate-an-autopilot-pass` | `quest_ai_runner.run("gate-an-autopilot-pass", 'quest-id')` | Scripted-client gate evaluation arm (`live_pass`) | Autopilot gate decision correctness |
| Intent frame for quest-scoped prompts | `ground-in-intent` | `quest_ai_runner.run("ground-in-intent", 'quest-id')` | Computed frame in the prompt (`frame-line`) | Intent-grounded pass rate |
| Specification slice and write-back | `iterate-by-spec` | `quest_ai_runner.run("iterate-by-spec", 'Tidy the card store')` | Slice before and write-back after (`slice-and-writeback`) | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed |
| Metric drafting for a task | `draft-a-metric` | `quest_ai_runner.run("draft-a-metric", ...)` | Capped model draft per task (today) (`capped_model_draft`) | Drafted metrics accepted per unit of cost |
| Intent specification selection | `spec-selection` | `esoi spec index --out FILE` | Lexical vector gated (`lexical-vector-gated`) | Specification selection pass rate |

## Problems and ideals

**Problems**

- Unwanted Background Work (`prob-autopilot-works-when-unwanted`): Recurring background passes can work when the person's settings say they should not: past the daily budget, on a day outside the roster, or without a cadence being due.
- Work Cut Off From Its Purpose (`prob-work-misses-its-why`): Replies, experiments and finished work are chosen and judged from the quest alone, not from the top intent and metrics the quest serves, so effort can drift from what the person cares about most and gaps under an intent can go unnoticed.

**Ideals**

- Permitted Timing (`ideal-permitted-timing`): Background work happens exactly when the person's settings allow and never otherwise.
- Purposeful Work (`ideal-purposeful-work`): Every piece of work names the ideal or gap it serves and is judged by that intent's metric.

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

- Parent intent: [Grounded AI that advances people's goals](../../../esoi/README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_intelligence/critical_thinking/esoi_application](esoi_application/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Unwanted Background Work (`prob-autopilot-works-when-unwanted`): Recurring background passes can work when the person's settings say they should not: past the daily budget, on a day outside the roster, or without a cadence being due.
- Work Cut Off From Its Purpose (`prob-work-misses-its-why`): Replies, experiments and finished work are chosen and judged from the quest alone, not from the top intent and metrics the quest serves, so effort can drift from what the person cares about most and gaps under an intent can go unnoticed.

### Ideals

- Permitted Timing (`ideal-permitted-timing`): Background work happens exactly when the person's settings allow and never otherwise.
- Purposeful Work (`ideal-purposeful-work`): Every piece of work names the ideal or gap it serves and is judged by that intent's metric.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Run liveness reviewer | `detect-a-stalled-run` | Live model reviewer arm (`live_reviewer`) | Stop-or-continue verdict accuracy with false stops counted apart |
| Guarded model-verdict judge | `judge-a-decision` | Production judges under a scripted provider (`qar_judges`) | Judge deterministic-half correctness with a scripted provider |
| Reply safety and anticipation guards | `guard-a-reply` | Production guard engines under test (`qar_guards`) | Reply guard correctness over link, recipe and anticipation cases |
| Autopilot run-or-skip gate | `gate-an-autopilot-pass` | Scripted-client gate evaluation arm (`live_pass`) | Autopilot gate decision correctness |
| Intent frame for quest-scoped prompts | `ground-in-intent` | Computed frame in the prompt (`frame-line`) | Intent-grounded pass rate |
| Specification slice and write-back | `iterate-by-spec` | Slice before and write-back after (`slice-and-writeback`) | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed |
| Metric drafting for a task | `draft-a-metric` | Capped model draft per task (today) (`capped_model_draft`) | Drafted metrics accepted per unit of cost |
| Intent specification selection | `spec-selection` | Lexical vector gated (`lexical-vector-gated`) | Specification selection pass rate |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Production liveness review in the goal runner | `detect-a-stalled-run:goal_runner` | [quest_ai_runner/the_intelligence/mind/delegation/goal_runner.py](../mind/delegation/goal_runner.py) |  |
| Live model reviewer arm | `detect-a-stalled-run:live_reviewer` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Keyword and threshold rule floor | `detect-a-stalled-run:rule_floor` | [evaluation/components/deep_review/impls/rule_floor.py](../../../evaluation/components/deep_review/impls/rule_floor.py) |  |
| Production judges under a scripted provider | `judge-a-decision:qar_judges` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Production guard engines under test | `guard-a-reply:qar_guards` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Eligibility gate mixin | `gate-an-autopilot-pass:autopilot_gates` | [quest_ai_runner/the_intelligence/proactivity/autopilot_gates.py](../proactivity/autopilot_gates.py) |  |
| Scripted-client gate evaluation arm | `gate-an-autopilot-pass:live_pass` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Prompts without the intent frame | `ground-in-intent:none` | [quest_ai_runner/the_intelligence/proactivity/autopilot_briefs.py](../proactivity/autopilot_briefs.py) |  |
| Computed frame in the prompt | `ground-in-intent:frame-line` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Frame plus intent card and record drill-down | `ground-in-intent:frame-card-drilldown` | [quest_ai_runner/the_body/internal_context/quest/client/quest_client_intents.py](../../the_body/internal_context/quest/client/quest_client_intents.py) |  |
| Frame plus a lazily generated quest specification | `ground-in-intent:frame-lazy-spec` | [quest_ai_runner/the_body/hands/quest_operations/tools_quest_intents.py](../../the_body/hands/quest_operations/tools_quest_intents.py) |  |
| Generic method in the standing instructions | `iterate-by-spec:generic-doctrine` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice of the specification before the task | `iterate-by-spec:slice-only` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice before and write-back after | `iterate-by-spec:slice-and-writeback` | [quest_ai_runner/the_intelligence/critical_thinking/goal_iteration.py](goal_iteration.py) |  |
| Slice before and write-back onto each record (planned) | `iterate-by-spec:writeback-on-the-record` |  |  |
| Capped model draft per task (today) | `draft-a-metric:capped_model_draft` | [quest_ai_runner/the_intelligence/critical_thinking/area.py](area.py) |  |
| Ask the owner to write it (floor, proposed) | `draft-a-metric:ask_owner` | [evaluation/components/draft_a_metric/impls/ask_owner.py](../../../evaluation/components/draft_a_metric/impls/ask_owner.py) |  |
| No-call floor of the quest ranker and reach judge | `judge-a-decision:no_call_floor` | [evaluation/components/judge_live_decisions/impls/no_call_floor.py](../../../evaluation/components/judge_live_decisions/impls/no_call_floor.py) |  |
| Live quest ranker and reach judge on a small model | `judge-a-decision:live_judges` | [evaluation/components/judge_live_decisions/impls/live_judges.py](../../../evaluation/components/judge_live_decisions/impls/live_judges.py) |  |
| Lexical TF-IDF, optional vector search, parallel batched model judging | `spec-selection:lexical-vector-llm` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py](esoi_application/spec_selection.py) |  |
| Lexical top few only | `spec-selection:lexical-only` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py](esoi_application/spec_selection.py) |  |
| Lexical vector gated | `spec-selection:lexical-vector-gated` | [quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py](esoi_application/spec_selection.py) |  |

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
