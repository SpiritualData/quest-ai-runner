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
| `disagrees` | `true` on both rows when a measured and a recorded row for the same metric give different numbers (see "When two numbers differ"). Both rows are kept; neither overwrites the other. |

A reading beats a shrug: when any entry of a goal holds a number (`before` or `after`, measured or recorded), the
`not_applicable` entries are left out of the payload, so one result never says a metric moved and that there was nothing to
measure. They stay in the ledger file, and a goal whose only entries are `not_applicable` ones still shows them.

An empty list means nothing to show. `metric_missing: true` is set next to it when the run changed something (by the
runner's own records) and no metric result was recorded for it, not even a `not_applicable`.

Where a consumer reads it:

- `DeepResult.metric_results` and `DeepResult.metric_missing`, one deep goal;
- `OrchestratorResult.metric_results` and `.metric_missing`, every deep goal of the turn in order;
- `data["metrics"]` (and `data["metric_missing"]` when set) on the `EVENT_MILESTONE` "Completed: ..." of a finished goal
  and on the `EVENT_RESULT` of a deep turn (also on an answer turn that ran deep work);
- one plain line at the end of the deep result text, for a consumer that shows text only, written by code from the
  entries: `Metric: <name> <before> -> <after> (measured)`, `Metric: not applicable (<reason>)`, or `No metric was recorded
  for this change.` A consumer that shows the entries itself drops that line with `metric_results.without_line(text,
  entries, missing)`, which removes only a line equal to what `render_line` wrote for those entries;
- the answer explanation (`docs/answer-explanation.md`, payload version 3): `explanation["metrics"]` and
  `explanation["metric_missing"]`, the same entries, kept in the facts half of the panel so no model writes or edits them.
  They are there only when the explanation is on and the turn is eligible; the result and the events above do not depend on it.

The QAR terminal shows the entries without a model and with the explanation off: once per turn, after everything the turn
said (a goal's result and its link, and an answer that follows it) and directly above the closing line, so the last things
on screen are the answer, the numbers, then how it ended. One line per metric in normal weight, ordered by the metric's
name (a number inside a name sorts as a number, a declared not-applicable last), the measured and the reported reading of
one metric together: `<name>: 0.38 → 0.91 (measured automatically)`, `(reported by the AI)`, `Nothing to measure.
<reason>`. When the readings differ, or the AI reported a number the measured reading lacks (a metric that could only be
read before the work), the AI's numbers follow as a note in the warning colour (`The AI reported 0.4 → 0.9.`). At most
four lines, then `K more metric results. Alt+D shows them all.`; or `No metric was recorded for this change.`. Written
once per turn however many events carry the same entry; a job's own record never writes it, because what follows a
record is not known until the turn ends. The numbers carry no unit: the data has none.

## When two numbers differ

A worker writes the number it reports with the decimals it chose (`0.4`), and the meter's is more exact (`0.38`): they say
the same thing. `metric_results.differs(measured, recorded)` therefore takes the measured value, rounds it half up to the
number of decimals the RECORDED value is written with (read from its shortest exact form, so `0.4` has one and `0.38` two),
and calls the two the same when that equals the recorded value. Anything else is a difference, compared exactly. There is no
tolerance: `0.38` against `0.4` and `0.91` against `0.9` agree, while `0.38` against `0.3`, or a recorded `0.38` against a
measured `0.4`, do not (a number written with more decimals than the other cannot be rounded into agreement).

## How a goal gets its record

For each goal the loop creates one `GoalMetrics` and calls it at four points.

1. **Before the first attempt**, inside the preparation that is already announced: the selected `iterate-by-spec` arm
   may add a short NEXT PRIORITY block to the brief (the next milestone, the metric, what to record) and may read the
   metric with its meters, within a short bound and with a status line when the reading takes time (see "Measuring
   costs no silent wait"). The worker is also told, in the tool list, the id to pass as `--run`, and a worker the runner
   starts has it in its environment as `QAR_TOOL_RUN_ID`, so a call that forgets the flag still lands on this run.
2. **After each attempt**: the loop notes whether the run changed something, from the runner's own records (a tool that
   acts succeeded in the tool-call ledger, `DeepResult.has_write_receipt`, or `DeepResult.changes_landed`, below).
3. **Before the attempt is verified**, the gate. A run that changed something and has no row at all is asked once, in its
   own session, to record one. That request is an attempt of the goal's existing budget. After it the flow accepts what
   there is and sets `metric_missing`; it never loops. The gate is judged on the ledger and the runner's records, never
   on the worker's or the verifier's wording. The request itself yields to the person: it says that when the request the
   worker was given told it not to record a metric, it records nothing, says so in one sentence and stops. Code never reads
   the request for that; the worker has the request, and a run that records nothing ends as `metric_missing`, which is
   the honest outcome.
4. **After the last attempt**: the arm reads the metric again with the meters that read the tree as it is now (a recorded
   row is not asked), the ledger's rows are reconciled, and the result carries them.

Where the gate does not apply, the record is still kept and shown, and `metric_missing` stays false:

- the worker was not offered `record_metric_result` (it could not have complied);
- no attempt of the budget is left;
- the runner exposes no record of change (see the table below for the ones that do not).

A runner that cannot resume a session (a re-run would repeat the work) is not asked, but the gap is not hidden: the
run is marked `metric_missing` unless a row exists.

### What tells the flow a run changed something

`DeepResult.changes_landed` is a count the runner writes from its own records, never from anyone's words. It is not
`has_write_receipt`: that flag means a governed quest-data write and feeds recipe learning, which a file edit must not.
The flow reads the tool-call ledger, `has_write_receipt` and `changes_landed`, and a run with a task id or without one
is read the same way (the tool-call ledger is keyed by the run id when there is no task id).

| Runner | The count is | Asked to record? | Without a row |
|---|---|---|---|
| `SubprocessGoalRunner` (`claude -p`) | the worker's file-changing tool uses (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`) that finished without an error, from its own session record (matched by the `--session-id` the runner generated), subagents' records included; a resumed run counts only what it added | yes, once, in its own session (`--resume`) | `metric_missing` |
| `StreamJsonGoalRunner` | the same, from the same session record | yes, as above | `metric_missing` |
| `AcpDeepRunner` | the agent's tool calls of kind `edit`, `delete` or `move` (or named like a file edit) whose status reached `completed` | no: it cannot resume a session | `metric_missing` |
| `FastEditRunner` | the files its `FileWriter` accepted, across its attempts | no: there is no worker, only one model call | `metric_missing` |
| `MCPOperationRunner` | 1 when the writer accepted the operation | no, as above | `metric_missing` |

In every row the code's own measurement (the selected arm, below) is still a record: when a slice resolves, a `measured` row
exists and the run is not marked missing.

**What is not covered.** A change made through a shell command (`sed -i`, a redirect, `git apply`, a script) is not a
file-changing tool use. The only way to tell it from any other command would be to read the command's text, which is a
decision on words a model wrote and is not made here. A Claude Code worker that changes files only through its shell is
therefore seen as having changed nothing (`changes_landed` 0), and is not asked. Such a run still gets the code's own
`measured` rows when a slice resolves, and the worker is still told the rule in its brief. A tool of the runner's own
(`ToolSpec.mutates`) that a worker calls from its shell is seen through the tool-call ledger, whichever way it changed things.

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
| `generic-doctrine` | Nothing. The standing instructions are the whole method; the record holds what the worker records. | no (the off switch) |
| `slice-only` | Adds the NEXT PRIORITY block for the solution the goal serves. | no |
| `slice-and-writeback` | The slice, plus the runner reads the metric before and after with its own meters and writes `measured` rows. | yes |

`slice-and-writeback` is selected because Joshua directed on 2026-10-21 that every run's metric result before and after is
recorded by code. The solution's replay metric (`task_replay`) is not built, so that is a direction and not a measured win.
Every consumer gets it: `build_orchestrator` (the terminal, the poll lanes) and a consumer that builds `Orchestrator` itself
(quest-backend does), which falls back to the selected arm when it is given none. A repository without ESOI records, or a
goal whose working folder is not known, has no slice, so such a run reads nothing, announces nothing and waits for nothing;
the gate and the worker's own record are the same as under any arm.

To turn the measuring off, select the doctrine: `ESOI_SELECT_QUEST_AI_RUNNER_ITERATE_BY_SPEC=generic-doctrine` for a whole
process, or `selections={"iterate-by-spec": "generic-doctrine"}` in `RunnerConfig`. To add only the slice to the brief and read
nothing, select `slice-only`.

### Measuring costs no silent wait

The reading before the work happens before the worker spawns, so it is bounded and told:

- it tries the cheap meter first (`recorded_row` reads a file) and announces nothing for it;
- before a meter that takes time (`offline_arm` runs a command) it says, as a status line, `Checking the starting value before
  any change.`, and again `Checking the result after the change.` for the second reading (never the metric's name, which a
  narrow screen cut mid-word);
- the whole reading has one budget, `QAR_METRIC_ARM_TIMEOUT_SECONDS` (default 15 seconds), whatever the number of meters;
  each meter runs on its own thread and one that has not answered when the budget is spent is left behind, and an offline arm
  is stopped at the limit with its process group;
- a reading that ran out of time gives no number and writes no row (a row without a number would count as a record). A status
  line says so (`Could not check the starting value in time. Going on without it.`), and the `measured` row written after the
  work carries the meter and the budget in its `reason`;
- the slice lookup is local and lexical (tens of milliseconds, bounded at 3 seconds) and starts nothing when there is no
  folder to look in.

**The slice** (`spec_slice`) is found without a model, from the `esoi/` records of the repository around the goal's working
folder, by the same lexical selection `find_specs` uses. It is given only when it is clear: the selection is confident, or
every candidate belongs to one solution. Otherwise there is none. The milestone is the first one not archived (a milestone
is archived when its text begins with `Archived`, `(archived)` or `[archived]`). Its target is a number only when the text
holds exactly one number; a percent is read as a fraction. The metric is the solution's `authoritative_metric_id`, or its
only metric. A goal whose working folder is not known gets no slice.

**The meters** (`metric_meter`), tried in the order `QAR_METRIC_METERS` names (default `recorded_row,offline_arm`):

- `recorded_row` reads the latest row of the results log for the metric's arm, while that row is current. A row recorded
  before the measured code changed gives nothing. It answers the reading before the work only: the row's staleness check
  sees the files the repository lists as measured code, so a change it does not list (a prose file, say) leaves a row that
  looks current and no longer describes the tree. A meter declares the moments it serves (`serves`; both when it says
  nothing), and the reading after the work asks only the meters that read the tree as it is now.
- `offline_arm` runs the metric's command (`esoi run <component> --impl <arm>`) only for an arm that cannot spend money: its
  latest row exists and shows no model call, and it is neither listed nor declared as paid. It runs with a budget of zero
  model calls, a scrubbed environment and the reading's time limit (`QAR_METRIC_ARM_TIMEOUT_SECONDS`, default 15). A refusal,
  a failure or a timeout gives no reading.

So after the work the offline arm is run (bounded and announced like any slow reading), and a metric with no offline arm
(a live one, which is run only by decision) gets no reading after: the code takes no `measured` after value for it, and the
worker's own `recorded` row stands alone. The runner does not first ask whether the work changed anything: its records
miss a change made through a shell command (see "What is not covered"), and a rule that rested on them would be blind to
exactly that.

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
