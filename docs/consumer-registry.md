# Consumer registry: adopting QAR in any system

A system built on QAR (a backend that implements QAR ports, passes config, calls the library) keeps
one CSV that lists every integration point. QAR ships the check, so adopting it is one file and one
test.

## 1. Write the registry

One row per integration point. Columns (extra columns such as `tracking_status`, `tracking_owner`,
`tracking_updated` and `tracking_notes` are ignored by the check, use them for review):

| Column | Meaning |
|---|---|
| `integration_id` | unique id |
| `solution_id` | the consumer's own solution this point belongs to (needed with `--solutions`/`--results`) |
| `module`, `symbol` | the consumer's dotted module and its class or function |
| `qar_solution_id` | a `solution_id` from QAR's `esoi/solutions.csv`, or empty |
| `qar_port` | for an implementation: the Protocol class it claims (`RetrievalAdapter`, `EscalationSink`, ...) |
| `qar_symbol` | for a call or config row: the `quest_ai_runner.module:Name` the consumer uses |
| `kind` | `implementation` (a consumer class for a QAR port), `config` (config passed to a QAR object), `call` (the consumer calls a QAR symbol), `duplicate` (library plumbing copied or patched in the consumer: must name the action that removes it) |
| `selected_by` | how the piece is chosen: a config key, a selection-table entry, or hand-wired |
| `metric` | an existing eval or live proof; empty means the gap and action must say what proof is missing |
| `tests` | `;`-separated test paths under the root |
| `status`, `gap`, `action` | `done`, `partial` or `gap`; the last two are required unless `done` |

## 2. Run the check

    esoi consumer check --registry esoi/qar_integration.csv \
        --root . --requirements requirements.txt [--fail-on-lag]

It verifies that every implementation imports and has every public method of the port it claims
(accepting the port's parameter names), that every solution id exists in the installed QAR, that every
`qar_symbol` resolves, that no duplicate row lacks an action, that tests exist, and it reports pin lag
(the commit pinned in the requirements file against the installed checkout). Exit 0 clean, 1 violations.
Call `esoi.consumer.check_consumer` from a
test (see esoi's `docs/guides/apply-with-tools.md`; the `[consumer]` table of `esoi.toml` configures it) so drift fails a normal test run.

Every integration point must sit in a solution with a current result. Give the check
`--solutions solutions.csv --results results.csv`: `solutions.csv` has `solution_id`, `selected`, and
`metric_files` (the case files); `results.csv` has `solution_id`, `cases`, `passed`, `git_sha`,
`cases_sha` (use `esoi.consumer.cases_hash(files, root)`). A point in no solution, a solution with no
selected implementation, a missing, failing or stale result (the case files changed since the run)
fails. The solution/problem/ideal/metric shape is QAR's own `esoi/` standard.

Two more optional solution columns say what kind of metric a solution has, as QAR's own standard does:
`metric_kind` is `eval` (a scored component where the best implementation can vary; name its `component` and
`component_impl`, and record a `kind=component` row in `results.csv` for them) or `invariant` (a rule that holds
or does not; `invariant_reason` says why best cannot vary). A consumer can run a QAR component against its own
implementation without copying the component: the harness accepts `--impl module:build` for an implementation
that lives outside the library.

## 3. Do not copy library plumbing

Shared helpers belong in the library. Two a consumer would otherwise copy:
`quest_ai_runner.core.async_bridge` (`run_coro_sync`, `run_detached`: one persistent background loop for
sync adapter code calling async I/O) and `quest_ai_runner.core.signature_probe` (`accepted_keywords`,
`accepted_fields`, `filter_keywords`: drop keywords an older installed library lacks).
