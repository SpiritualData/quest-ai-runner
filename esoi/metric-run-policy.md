# When quest-ai-runner's metrics are run

The filled-in metric run policy for this repository, from esoi's template (`docs/templates/metric-run-policy.md` in the
esoi repository). Lines that start "Pending Joshua's decision" are proposals that nobody has approved yet; the proposed default
is what is followed until he decides. The policy is prose here: esoi does not require it as records (`governs_metric_ids` on a
solution and `run_policy_ids` on a metric are optional columns that `esoi check` verifies once they exist). To make it records,
add the policy solution with its problem, ideal, alternatives and metric under `esoi/`, list the governed metrics in
`governs_metric_ids` and name the policy in each metric's `run_policy_ids`.

## Problem (`prob-metrics-run-at-the-wrong-time`)

QAR's metrics range from zero-cost offline arms to live arms that spend model calls through the claude CLI or an API. Results
go stale when the planner prompt, a component or a case file changes after the row was recorded; live arms were once run in
full out of habit, and one run had to be killed mid-flight for its spend. People then either decide on an old number or avoid
running the metrics at all.

## Ideal (`ideal-timely-affordable-evidence`)

A selection in `solutions.csv` always cites a current row of `results.csv`, and what that row cost was small, estimated first and
approved.

## The solution: when QAR's metrics are run (`when-qar-metrics-are-run`)

- **Purpose:** governs every Metric of `evaluation/components/`. Does not govern the product's own tests.
- **Goals:** offline arms current on every change; live arms run only for a decision; every recorded row current under
  `esoi check --strict`.
- **Parameters (triggers and stages):**
  - Offline arms (deterministic, zero model calls: for example `staleness`, `context_selection` hybrid and floor arms, the
    reference_good and reference_bad arms of `esoi_behavior`, the fixtures of `check_the_structure` and
    `measure_implementations`, `feedback_loop`, `receive_input_mid_run`): run for the component you changed on every change, and
    with `evaluation/components/test_*.py` before committing. Never the whole `tests/` suite unless a human asks (standing rule).
  - Live arms (model calls: `classification`, `quest_ops`, `recipe_fast_path` real-model arms, `esoi_behavior` chat arm with the
    model judge, `esoi_judge`, `answer-a-turn` dev arm, `chat_quality`): run only when a decision depends on them: a new
    alternative, a selection change, or a suspected regression. Staged: 3 cases, then 10, then 30, by `--sample N` (a stratified
    sample, the same cases each time) with `--max-calls`. A full run only if a decision still depends on it after the 30 (size is a cost choice, not an approval gate).
    - Pending Joshua's decision: whether this 3, 10, 30 ladder matches his "3/10/30 to 100%" staging. Proposed default: the
      ladder as written.
  - Cost first: estimate calls and price before the first stage and write it in the task.
- **Inputs:** committed code, the component's case files, the model backend named by the environment, a disposable world for
  the dev arm (never real user data).
- **Outputs:** a row in `results.csv` with `git_sha`, `cases_sha`, `model`, calls, tokens and cost.
- **Effects:** model spend on live arms; the dev arm drives a development server only.
- **Who approves spend:** approved by Joshua on 2026-10-19 and widened on 2026-10-21 ("You don't need to ask me for every eval
  run, only if total across Spiritual Data for the day would be over $5"). A run on the cheapest tier against development, of any
  size including a full set, an AI may approve for itself, inside a call budget written in the task before the run, whenever the
  day's total including the run stays within the cap. The cap is
  **$5 across all of Spiritual Data in any rolling 24 hours** (his words: "budget is $5 across all of Spiritual Data in a given
  24 hours"). The cap is counted in one ledger shared by every repository and every AI run, which Spiritual Data keeps
  outside this public repository: before the run the AI reserves its estimate there, and a reservation that would take
  the last 24 hours past $5 is refused, so that run goes to Joshua instead; after the run it records the actual cost from
  the results row. A run that
  would take the day past $5, a stronger tier, or anything against production or user data goes to Joshua through the
  approval-gate as a decision-request. Run size alone no longer does (this replaces his 2026-10-06 rule on full runs). Mirror:
  quest-backend's `esoi/metric-run-policy.md`; commit a293903a (checks on the esoi library) and the October follow-up that
  moved the policy into `esoi/`.
- **Stale when:** the row has no `git_sha` or `cases_sha`; the case files changed (hash differs); it was recorded on uncommitted code
  (`-dirty`); the measured code or the arm's files changed since its commit (`esoi check --strict`); or the model, its price or
  the provider's behavior changed. The first four are checked by the tool; the last needs the scheduled re-measurement below.
- **Re-measure when models or prices change:** when a model alias or price table changes (for example a new tier default or a
  price correction), re-run the selected arm and its nearest rival at the 10-case stage, then 30 if the verdict is not WIN.
  - Pending Joshua's decision: a quarterly re-measure of the selected arms of the live components. Proposed default: no
    schedule; re-measure only on a model or price change.

## Alternatives (implementations of the policy)

| Alternative | Used for |
|---|---|
| On every change | offline arms |
| On a decision | live arms (staged, budgeted) |
| On drift | model or price changes: re-measure the selected arm and its rival |
| On a schedule | considered for a periodic re-measure of the live selections; not adopted until a model change slips through |

## The metric of the policy

The share of selection changes (edits of `selected` in `solutions.csv`) that cite a current row at the time of the commit, and the
spend of the runs behind each. Counted from `git log` of `solutions.csv` and `results.csv` until a harness run records it.

## Governed metric runs (rows for `metric_runs.csv`)

| metric_id | arm | trigger | stages | call_budget | cost_estimate | approver | stale_when |
|---|---|---|---|---|---|---|---|
| each offline component's metric | offline | every change to the component or its cases | whole set | none | none | none | cases changed, code changed since commit, dirty |
| classification, quest_ops, recipe_fast_path, esoi_behavior (chat arm), esoi_judge, chat_quality | live | a decision | 3, 10, 30 | set per run in the task before the first stage, inside the $5 per rolling 24 hours cap across Spiritual Data | calls x price, written first | AI for dev 3 and 10-case stages within the cap, Joshua beyond | as above, and a model or price change |
| answer-a-turn (dev arm) | live | a decision | 3, 10 | set per run | calls x price | AI for dev, Joshua beyond | as above, and any change to the dev world |
