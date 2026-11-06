# Running a priority dataset on production, safely

A dataset of chat cases (the repository's `acceptance` suite, or a private set of real inputs the assistant once answered badly)
can be run two ways, both through the `chat_quality` metric and the same judge, so their results are comparable:

| Arm | Where it answers | Side effects |
|---|---|---|
| `dev_quest_chat` | a disposable development account (the dev eval world) | none that last: the world is put back after each case |
| `prod_qar_readonly` | the production brain, in one process, with the lane's own configuration, context sources, preamble, personas and Quest account | none: every state change is recorded and not made |

`esoi run chat_quality --impl prod_qar_readonly --only ID,ID --max-calls N --keep-going --record` runs any chat case on the production
brain. Each case is a fresh conversation of one turn. Results land in `evaluation/components/results.csv` and `per_case.csv` like every other
arm, and each case appends a line to the private output log (`QAR_EVAL_OUTPUT_DIR`, a folder outside the repository) with its reply, route,
judge verdict and an `environment` of `production (writes blocked)` beside the reads, the writes it would have made, the model calls and
the time.

## How the writes are blocked

`QAR_DRY_RUN_WRITES=1` (set by the arm; read by `the_world/tools/dry_run_writes.py`) closes the process. With it unset, nothing changes.

- **Quest client**: any request that is not a GET, HEAD or OPTIONS is answered with a stand-in success and recorded, never sent.
- **Tools**: a tool whose spec has `mutates` true is recorded with its arguments and reports done; a read tool runs and is counted.
- **Deep worker**: replaced by a stub that records the goal and brief it would have been given and reports the run as queued.
- **Everything else**: an HTTP request that is not a read is refused unless it is on the short list of read-only POST endpoints in
  `dry_run_writes.py` (each one checked against the code that sends it); a file opened for writing is redirected to a scratch copy and
  a removal or rename is refused; a subprocess is refused unless it is the model CLI answering with no tools.

The runner's own memory writes (its card store, its vector index, its state files) are blocked the same way and counted apart, as
bookkeeping; the writes a request caused are shown to the judge as `would have done` lines. Because a blocked write returns a stand-in
success, a turn is judged only as far as that stand-in goes, and a reply that depends on what a write returned reads as an action
taken. Reads are real, so the answers use the real data: keep the output log private.

## Setting it up on a machine that holds a lane

The arm needs three paths, all outside the repository: `QAR_PROD_LANE_ENV_FILE` (the lane's environment file), `QAR_CONFIG_FILE` (its TOML
configuration) and optionally `QAR_PROD_LANE_DIR` (the folder the lane runs in). Without them the arm prints SKIPPED and exits 0. The
model is the configured Claude CLI on the subscription; the arm spends no API credit. `--max-calls` bounds the model calls of the whole run;
one case stops at 45 model calls or 5 minutes (`QAR_PROD_CASE_MODEL_CALLS`, `QAR_PROD_CASE_SECONDS`). A tool the lane lacks is reported in the log as a lane gap, apart from a model failure.

## One command per deployment

A deployment wraps the steps so there is one way to run a dataset: choose the dataset and the environment, run the cases, refresh the sheet
that shows every dataset and input with its latest result per environment, print the sheet link and the pass count. See your
deployment's wrapper; `scripts/qar_datasets_sheet.py` is the generic exporter that wrapper calls (its `--run-command` option puts the
wrapper's command in the sheet's HOW TO RUN column), and `docs/how-to/public-hygiene.md` says what must never enter this repository.

## What each result records

- **Metric before and after.** A case may carry a `metric_probe` (or get one from the rules file named by `QAR_PROD_CASE_RULES_FILE`, a JSON
  object keyed by case id that may also give the structured `effect` and `state_check`): what to read, how to pick the number, and
  optionally the same number worked out from readable data. The arm reads it as the acting account before and after the turn and records
  name, before, after, delta and `moved` (`yes`, `no`, or `not-movable-by-AI-in-one-turn`). A metric that did not change is `no`, however good
  the step; a recorded value that differs from the computed one fails the case.
- **Messages in order.** Every assistant message of the turn (interim, progress, final) is kept with its time. A case that acts is judged on
  two roles, not wording: message 1 states the current status and what is being done, message 2 reports what was done, the new state and the
  metric before and after. A reply-only case is judged as one message.
- **Tokens, model calls, lane gaps, the account acted as.** A plain read-and-answer turn on this path took 16 to 32 model calls, about half of them the context engine (query generation, review and card filtering, run again for each overseer consultation).

## Judging a blocked write

The arm tells the judge that the changes listed are attempts that were blocked. A case whose ideal handling is to act (create a task,
schedule a run, look something up) passes when the attempt was the right one; a case whose ideal handling is to ask, to answer without
acting or to say what it cannot see fails if it attempted a write.
