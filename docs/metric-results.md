# The metric record of a deep goal

**Code:** `core/metric_results.py` (the row, the ledger, the payload), `core/goal_metrics.py` (the flow),
`core/tools_metric_results.py` (the tool), `core/spec_slice.py` and `core/metric_meter.py` (the slice and the meters),
`core/goal_iteration.py` (the arms). **Specification:** solution `iterate-by-spec`.

ESOI asks every piece of work to say what the relevant metric was before and after the change. Left to a worker's closing
words that is a claim. Here the runner keeps the record itself: the numbers are written to a ledger by code, the goal loop
reads the ledger (never the wording of anything a model wrote), and every consumer, the terminal and a backend alike, is
handed the same structured entries. Nothing in this flow calls a model.

## The payload

A finished deep goal carries `metrics`, a list with one entry per metric (at most 12, as in the answer explanation):

```json
{"solution_id": "receive-input-mid-run", "metric_id": "metric-receive-input-mid-run",
 "name": "Exactly-once mid-run delivery per run", "milestone": "Exactly-once delivery, send-to-seen latency recorded",
 "before": 0.5, "after": 0.75, "delta": 0.25, "source": "measured",
 "reason": "measured by offline_arm", "disagrees": false}
```

| Key | Meaning |
|---|---|
| `solution_id`, `metric_id`, `name`, `milestone` | What was measured. Any may be empty when the worker did not say and the repository has no record of it. |
| `before`, `after` | Numbers or `null`. A value is a finite int or float; a string a model wrote is refused, never converted. |
| `delta` | `after - before`, or `null` when either end is missing. |
| `source` | Who vouches for the numbers: `measured` (the runner's own meter), `recorded` (the worker called `record_metric_result`; nobody checked), or `not_applicable` (the worker said no metric applies, and `reason` says why). |
| `reason` | How it was measured, or why no metric applies. |
| `disagrees` | `true` on both rows when a measured and a recorded row for the same metric give different numbers. Both rows are kept; neither overwrites the other. |

An empty list means nothing to show. `metric_missing: true` is set next to it when the run changed something (by the
runner's own records) and no metric result was recorded for it, not even a `not_applicable`.

Where a consumer reads it:

- `DeepResult.metric_results` and `DeepResult.metric_missing`, one deep goal;
- `OrchestratorResult.metric_results` and `.metric_missing`, every deep goal of the turn in order;
- `data["metrics"]` (and `data["metric_missing"]` when set) on the `EVENT_MILESTONE` "Completed: ..." of a finished goal
  and on the `EVENT_RESULT` of a deep turn (also on an answer turn that ran deep work);
- one plain line at the end of the deep result text, for a consumer that shows text only, written by code from the
  entries: `Metric: <name> <before> -> <after> (measured)`, `Metric: not applicable (<reason>)`, or `No metric was recorded
  for this change.`

## How a goal gets its record

For each goal the loop creates one `GoalMetrics` and calls it at four points.

1. **Before the first attempt**, inside the preparation that is already announced: the selected `iterate-by-spec` arm
   may add a short NEXT PRIORITY block to the brief (the next milestone, the metric, what to record) and may read the
   metric with its meters. The worker is also told, in the tool list, the id to pass as `--run`.
2. **After each attempt**: the loop notes whether the run changed something, from the runner's own records (a tool that
   acts succeeded in the tool-call ledger, or `DeepResult.has_write_receipt`).
3. **Before the attempt is verified**, the gate. A run that changed something and has no row at all is asked once, in its
   own session, to record one. That request is an attempt of the goal's existing budget. After it the flow accepts what
   there is and sets `metric_missing`; it never loops. The gate is judged on the ledger and the runner's records, never
   on the worker's or the verifier's wording.
4. **After the last attempt**: the arm reads the metric again, the ledger's rows are reconciled, and the result carries
   them.

Where the gate does not apply, the record is still kept and shown, and `metric_missing` stays false:

- the worker was not offered `record_metric_result` (it could not have complied);
- the runner cannot resume a session (a re-run would repeat the work);
- no attempt of the budget is left;
- the runner exposes no record of change. The tool-call ledger is keyed by the task id, so a run with no task id has none,
  and a Claude Code worker that edits files with its own tools reports no write receipt. Such a run is not gated.

## The tool a worker calls

`record_metric_result` is offered to every deep run (and to no chat turn). With a metric: `metric_id` and a number for
`before`, `after` or both, optionally `solution_id` and `milestone`. With none: `not_applicable` and a `reason`, which is
how a run on a draft or an answer satisfies the rule honestly. It works where there is no specification at all. The call is
kept under the run's id (`--run`, or `QAR_TOOL_RUN_ID`); with only `--task` it is kept under the task. Standing
instructions name the tool and never its arguments.

## The arms of iterate-by-spec

The record, the tool and the gate run under every arm. The arms differ in what the code adds:

| Arm | What the code does | Selected |
|---|---|---|
| `generic-doctrine` | Nothing. The standing instructions are the whole method; the record holds what the worker records. | yes |
| `slice-only` | Adds the NEXT PRIORITY block for the solution the goal serves. | no |
| `slice-and-writeback` | The slice, plus the runner reads the metric before and after with its own meters and writes `measured` rows. | no |

The records say the slice arms are proposals until the solution's replay metric is run by decision, so the selection is
unchanged. A lane turns an arm on with `ESOI_SELECT_QUEST_AI_RUNNER_ITERATE_BY_SPEC=slice-and-writeback` or the
`iterate-by-spec` key of `RunnerConfig.selections`.

**The slice** (`spec_slice`) is found without a model, from the `esoi/` records of the repository around the goal's working
folder, by the same lexical selection `find_specs` uses. It is given only when it is clear: the selection is confident, or
every candidate belongs to one solution. Otherwise there is none. The milestone is the first one not archived (a milestone
is archived when its text begins with `Archived`, `(archived)` or `[archived]`). Its target is a number only when the text
holds exactly one number; a percent is read as a fraction. The metric is the solution's `authoritative_metric_id`, or its
only metric. A goal whose working folder is not known gets no slice.

**The meters** (`metric_meter`), tried in the order `QAR_METRIC_METERS` names (default `recorded_row,offline_arm`):

- `recorded_row` reads the latest row of the results log for the metric's arm, while that row is current. A row recorded
  before the measured code changed gives nothing.
- `offline_arm` runs the metric's command (`esoi run <component> --impl <arm>`) only for an arm that cannot spend money: its
  latest row exists and shows no model call, and it is neither listed nor declared as paid. It runs with a budget of zero
  model calls, a scrubbed environment and a time limit (`QAR_METRIC_ARM_TIMEOUT_SECONDS`, default 60). A refusal, a
  failure or a timeout gives no reading.

A meter that has no reading writes no row. A row with no number would otherwise count as a record.

## Where it is kept

One JSON-lines file per run under `QAR_METRIC_LEDGER_DIR` (default: a folder in the system temp directory, like the
tool-call ledger), shared by the runner and the worker's shell commands. Files untouched for two days are removed. An
append is one locked write of a whole line, so two processes never tear one.

## Reported to a backend (the task executor)

A lane that runs a queued task (`runner/executor_reporting.py`) hands the record to the backend as data too, from the
turn's `OrchestratorResult` and without working anything out again:

- **The terminal report.** `result_data` on the PATCH of every terminal status (`done`, `incomplete`, `needs_you`,
  `failed`) holds `{"metrics": [...], "metric_missing": true}`, with only the keys that have something to say, capped at 12
  entries. On `done` it sits beside the outcome stamp: `{"outcome": "done", "metrics": [...]}`. A run with neither adds
  no key, a client whose report methods do not declare `result_data` is called as it always was, and a backend that
  refuses the key (422) gets the same report without it.
- **The live feed.** The finished goal's "Completed: ..." milestone, which carries `data["metrics"]` /
  `data["metric_missing"]`, is posted on the task's progress feed with those two keys (and no others) as the post's `data`.
  Which events a mode shows at all stays the sink's rule; the executor adds data to the milestone it already posts.
