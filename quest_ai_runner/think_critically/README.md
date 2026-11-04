# Thinking critically by metric

Everything the AI does for a person's goals is held to the metric that serves it, and what it says is held to what is known.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `think-critically:deep_runs_only` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A deep run can be held to the metric that serves its goal, and so can every other request, which is wider than the parent solution asks. That opens questions of its own: slicing the specification a task concerns before the run and writing back what the run learned, choosing the records an input concerns by lexical, vector and gated search, settling a narrow question with one guarded model call and a safe default, stopping unverified links and recognising repeated requests, telling a stuck run from a slow one, drafting a metric for a task that has none, and keeping background work to the days, hours and budget a person set.

## How the solutions fit together

think-critically holds deep runs to their metric through iterate-by-spec on the records spec-selection picks, settles narrow questions through judge-a-decision, stops unsafe replies through guard-a-reply, ties work to its purpose through ground-in-intent, tells a stuck run from a slow one through detect-a-stalled-run, drafts a missing metric through draft-a-metric, and keeps background work inside its hours and budget through gate-an-autopilot-pass.

## Solutions at a glance

| Solution | Entry call | Selected implementation | Metric |
|---|---|---|---|
| `detect-a-stalled-run` | `quest_ai_runner.run("detect-a-stalled-run", ...)` | `live_reviewer` | Stop-or-continue verdict accuracy with false stops counted apart |
| `judge-a-decision` | `quest_ai_runner.run("judge-a-decision", ...)` | `qar_judges` | Judge deterministic-half correctness with a scripted provider |
| `guard-a-reply` | `quest_ai_runner.run("guard-a-reply", 'See https://example.org/a for details.')` | `qar_guards` | Reply guard correctness over link, recipe and anticipation cases |
| `gate-an-autopilot-pass` | `quest_ai_runner.run("gate-an-autopilot-pass", 'quest-id')` | `live_pass` | Autopilot gate decision correctness |
| `ground-in-intent` | `quest_ai_runner.run("ground-in-intent", 'quest-id')` | `frame-line` | Intent-grounded pass rate |
| `iterate-by-spec` | `quest_ai_runner.run("iterate-by-spec", 'Tidy the card store')` | `slice-and-writeback` | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed |
| `spec-selection` | `esoi spec index --out FILE` | `lexical-vector-gated` | Specification selection pass rate |
| `draft-a-metric` | `quest_ai_runner.run("draft-a-metric", ...)` | `capped_model_draft` | Drafted metrics accepted per unit of cost |

## Problems and ideals

**Problems**

- `prob-deep-work-wanders`: Long autonomous work can run without bound, get stuck, repeat the same action, wait forever on a person, hit a usage limit unnoticed, or be killed while a slow build or download was actually still...
- `prob-autopilot-works-when-unwanted`: Recurring background passes can work when the person's settings say they should not: past the daily budget, on a day nobody is rostered, or without a cadence being due.
- `prob-same-ask-repeats-cost`: People ask for the same operation again and again, and each time the AI searches and plans from scratch at full cost, while what it learned about their habits is lost on restart or mixes between...
- `prob-narrow-verdicts-unreliable`: A small model call used to settle one narrow question (reach, quest choice, overseer signal, sufficiency) can return a malformed, hallucinated or missing verdict, and a wrong verdict silently changes...
- `prob-work-misses-its-why`: Replies, experiments and finished work are chosen and judged from the quest alone, never from the top intent and metrics the quest serves, so effort drifts from what the person cares about most and...
- `prob-intent-specs-not-found-for-input`: A new request or input arrives and the system cannot tell which of a project's intent specifications (problems, ideals, solutions, implementations, metrics) concern it, so it either loads none, loads...
- `prob-run-improvement-unchecked`: Whether a run improved anything is recorded only in part and only for a while.

**Ideals**

- `ideal-accountable-autonomy`, Accountable Autonomy: Autonomous work runs to a written goal, within bounds, and stops exactly when it should.
- `ideal-permitted-timing`, Permitted Timing: Background work happens exactly when the person's settings allow and never otherwise.
- `ideal-purposeful-work`, Purposeful Work: Every piece of work names the ideal or gap it serves and is judged by that intent's metric.
- `ideal-specs-that-apply-are-in-view`, Specifications that apply are in view: Every run sees the intent specifications that concern its input, and only those, at the cheapest tier that lets it decide.

## Metrics and their leaders

- `metric-detect-a-stalled-run`, Stop-or-continue verdict accuracy with false stops counted apart: leader `rule_floor`, 3 of 3 cases
- `metric-judge-a-decision`, Judge deterministic-half correctness with a scripted provider: leader `qar_judges`, 61 of 61 cases
- `metric-guard-a-reply`, Reply guard correctness over link, recipe and anticipation cases: leader `qar_guards`, 54 of 54 cases
- `metric-gate-an-autopilot-pass`, Autopilot gate decision correctness: leader `live_pass`, 33 of 33 cases
- `metric-ground-in-intent`, Intent-grounded pass rate: leader `frame_line`, 3 of 3 cases
- `metric-iterate-by-spec`, First-pass acceptance and exploration before the first edit: leader `single_attempt`, 8 of 9 cases
- `metric-spec-selection`, Specification selection pass rate: leader `lexical_vector_gated`, 70 of 76 cases
- `metric-track-runs-on-records`, Runs that leave a durable, attributed record of what they changed
- `metric-draft-a-metric`, Drafted metrics accepted per unit of cost: leader `capped_model_draft`, 10 of 10 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- `prob-deep-work-wanders`: Long autonomous work can run without bound, get stuck, repeat the same action, wait forever on a person, hit a usage limit unnoticed, or be killed while a slow build or download was actually still making progress.
- `prob-autopilot-works-when-unwanted`: Recurring background passes can work when the person's settings say they should not: past the daily budget, on a day nobody is rostered, or without a cadence being due.
- `prob-same-ask-repeats-cost`: People ask for the same operation again and again, and each time the AI searches and plans from scratch at full cost, while what it learned about their habits is lost on restart or mixes between people.
- `prob-narrow-verdicts-unreliable`: A small model call used to settle one narrow question (reach, quest choice, overseer signal, sufficiency) can return a malformed, hallucinated or missing verdict, and a wrong verdict silently changes the turn.
- `prob-work-misses-its-why`: Replies, experiments and finished work are chosen and judged from the quest alone, never from the top intent and metrics the quest serves, so effort drifts from what the person cares about most and gaps under an intent go unnoticed.
- `prob-intent-specs-not-found-for-input`: A new request or input arrives and the system cannot tell which of a project's intent specifications (problems, ideals, solutions, implementations, metrics) concern it, so it either loads none, loads all, or relies on whichever the person remembers to mention.
- `prob-run-improvement-unchecked`: Whether a run improved anything is recorded only in part and only for a while. A deep run records a metric reading before and after (the record_metric_result tool and GoalMetrics) into a per-run file of readings in a temporary folder that is cleared after two days, and the task report carries it to Quest; but nothing says what the run moved, the reading never reaches the record it concerns, a card's last_outcome comes from the kind of turn rather than from any metric, and when the work shows a record is wrong or missing nothing makes the run fix it. Improvement is hoped for rather than checked per run.

### Ideals

- `ideal-accountable-autonomy`, Accountable Autonomy: Autonomous work runs to a written goal, within bounds, and stops exactly when it should.
- `ideal-permitted-timing`, Permitted Timing: Background work happens exactly when the person's settings allow and never otherwise.
- `ideal-purposeful-work`, Purposeful Work: Every piece of work names the ideal or gap it serves and is judged by that intent's metric.
- `ideal-specs-that-apply-are-in-view`, Specifications that apply are in view: Every run sees the intent specifications that concern its input, and only those, at the cheapest tier that lets it decide.

### Solutions

| Solution | Name | Selected | Metric |
|---|---|---|---|
| `detect-a-stalled-run` | Run liveness reviewer | live_reviewer | Stop-or-continue verdict accuracy with false stops counted apart |
| `judge-a-decision` | Guarded model-verdict judge | qar_judges | Judge deterministic-half correctness with a scripted provider |
| `guard-a-reply` | Reply safety and anticipation guards | qar_guards | Reply guard correctness over link, recipe and anticipation cases |
| `gate-an-autopilot-pass` | Autopilot run-or-skip gate | live_pass | Autopilot gate decision correctness |
| `ground-in-intent` | Intent frame for quest-scoped prompts | frame-line | Intent-grounded pass rate |
| `iterate-by-spec` | Specification slice before a task and write-back after | slice-and-writeback | First-pass acceptance and exploration before the first edit, Runs that leave a durable, attributed record of what they changed |
| `spec-selection` | Select the intent specifications an input concerns | lexical-vector-gated | Specification selection pass rate |
| `draft-a-metric` | Metric drafting for a task | capped_model_draft | Drafted metrics accepted per unit of cost |

### Implementations

| Implementation | Name | Where | Sub-intent |
|---|---|---|---|
| `detect-a-stalled-run:goal_runner` | Production liveness review in the goal runner | [quest_ai_runner/core/goal_runner.py](../core/goal_runner.py) |  |
| `detect-a-stalled-run:live_reviewer` | Live model reviewer arm | [quest_ai_runner/think_critically/area.py](area.py) |  |
| `detect-a-stalled-run:rule_floor` | Keyword and threshold rule floor | [evaluation/components/deep_review/impls/rule_floor.py](../../evaluation/components/deep_review/impls/rule_floor.py) |  |
| `judge-a-decision:qar_judges` | Production judges under a scripted provider | [quest_ai_runner/think_critically/area.py](area.py) |  |
| `guard-a-reply:qar_guards` | Production guard engines under test | [quest_ai_runner/think_critically/area.py](area.py) |  |
| `gate-an-autopilot-pass:autopilot_gates` | Eligibility gate mixin | [quest_ai_runner/think_critically/autopilot_gates.py](autopilot_gates.py) |  |
| `gate-an-autopilot-pass:live_pass` | Scripted-client gate evaluation arm | [quest_ai_runner/think_critically/area.py](area.py) |  |
| `ground-in-intent:none` | Prompts without the intent frame | [quest_ai_runner/think_critically/autopilot_briefs.py](autopilot_briefs.py) |  |
| `ground-in-intent:frame-line` | Computed frame in the prompt | [quest_ai_runner/think_critically/area.py](area.py) |  |
| `ground-in-intent:frame-card-drilldown` | Frame plus intent card and record drill-down | [quest_ai_runner/think_critically/quest_client_intents.py](quest_client_intents.py) |  |
| `ground-in-intent:frame-lazy-spec` | Frame plus a lazily generated quest specification | [quest_ai_runner/core/tools_quest_intents.py](../core/tools_quest_intents.py) |  |
| `iterate-by-spec:generic-doctrine` | Generic method in the standing instructions | [quest_ai_runner/think_critically/goal_iteration.py](goal_iteration.py) |  |
| `iterate-by-spec:slice-only` | Slice of the specification before the task | [quest_ai_runner/think_critically/goal_iteration.py](goal_iteration.py) |  |
| `iterate-by-spec:slice-and-writeback` | Slice before and write-back after | [quest_ai_runner/think_critically/goal_iteration.py](goal_iteration.py) |  |
| `spec-selection:lexical-vector-llm` | Lexical TF-IDF, optional vector search, parallel batched model judging | [quest_ai_runner/core/spec_selection.py](../core/spec_selection.py) |  |
| `spec-selection:lexical-only` | Lexical top few only | [quest_ai_runner/core/spec_selection.py](../core/spec_selection.py) |  |
| `spec-selection:lexical-vector-gated` | Lexical vector gated | [quest_ai_runner/core/spec_selection.py](../core/spec_selection.py) |  |
| `iterate-by-spec:writeback-on-the-record` | Slice before and write-back onto each record (planned) |  |  |
| `draft-a-metric:capped_model_draft` | Capped model draft per task (today) | [quest_ai_runner/think_critically/area.py](area.py) |  |
| `draft-a-metric:ask_owner` | Ask the owner to write it (floor, proposed) | [evaluation/components/draft_a_metric/impls/ask_owner.py](../../evaluation/components/draft_a_metric/impls/ask_owner.py) |  |
| `judge-a-decision:no_call_floor` | No-call floor of the quest ranker and reach judge | [evaluation/components/judge_live_decisions/impls/no_call_floor.py](../../evaluation/components/judge_live_decisions/impls/no_call_floor.py) |  |
| `judge-a-decision:live_judges` | Live quest ranker and reach judge on a small model | [evaluation/components/judge_live_decisions/impls/live_judges.py](../../evaluation/components/judge_live_decisions/impls/live_judges.py) |  |

### Metrics

- `metric-detect-a-stalled-run`, Stop-or-continue verdict accuracy with false stops counted apart: `esoi run deep_review --impl live_reviewer`
- `metric-judge-a-decision`, Judge deterministic-half correctness with a scripted provider: `esoi run judges --impl qar_judges; esoi run judge_live_decisions --impl live_judges`
- `metric-guard-a-reply`, Reply guard correctness over link, recipe and anticipation cases: `esoi run reply_guards --impl qar_guards`
- `metric-gate-an-autopilot-pass`, Autopilot gate decision correctness: `esoi run gate_an_autopilot_pass --impl live_pass`
- `metric-ground-in-intent`, Intent-grounded pass rate: `esoi run intent_grounding --impl frame_line`
- `metric-iterate-by-spec`, First-pass acceptance and exploration before the first edit: `esoi run task_replay --impl slice_and_writeback`
- `metric-spec-selection`, Specification selection pass rate: `esoi run spec_selection --impl lexical_vector_gated; esoi run spec_selection --impl lexical_vector_llm; esoi run spec_selection --impl lexical_vector; esoi run spec_selection --impl lexical_only`
- `metric-track-runs-on-records`, Runs that leave a durable, attributed record of what they changed: `esoi run record_tracking`
- `metric-draft-a-metric`, Drafted metrics accepted per unit of cost: `esoi run draft_a_metric --impl capped_model_draft`
