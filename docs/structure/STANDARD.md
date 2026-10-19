# The standard shape for applying ESOI (copy this to another repo)

One solution id names every part of one idea. Nothing in the repo exists outside a solution, and a
solution is not done until a recorded run says how well it works. This page is enough to add a
solution; the numbered rules that enforce it are a reference in [README.md](README.md), and the philosophy is the evolvable-structure skill (`principles.csv` is its short form).

## The shape of one solution

| Part | Where | Holds |
|---|---|---|
| Records | `problems.csv`, `ideals.csv`, `solutions.csv`, `implementations.csv`, `metrics.csv` | The intent network, one CSV per type, each with the columns of the evolvable-structure skill's intent template (generated section below). A solution also carries the registry columns: `port`, `implementations`, `selected` (the selection-table name when there is a port, S23) and `selected_by`, `metric_kind` (`eval`, or `invariant` with `invariant_reason`), any `gap` and `action`; no numbers in prose (S22) |
| Port | `quest_ai_runner/core/adapters*.py` (a `Protocol`), only when implementations are swappable | the interface; callers depend on it, never on an implementation |
| Selection | `quest_ai_runner/core/selection_table.py` (S14) | `name -> "module:Class"` and a default; `resolve(solution_id, name)` returns the class |
| Metric | `docs/structure/metrics.csv` | one record per metric: the template fields plus `solution_ids` and `command` (an `esoi run <component> --impl <impl>` command per arm) |
| Evaluation | `evaluation/components/<solution_id with underscores>/` | `interface.py` (one function with a fixed signature), `cases.json`, `score.py`, `impls/<impl>.py` driving LIVE code (S12) |
| Result | `evaluation/components/results.csv` | one row per `esoi run <component> --impl <impl> --record`, with `git_sha` and `cases_sha` (S16) |
| Decision | `esoi leaderboard` | the winner on the same cases, and a verdict per solution: WIN (the metric chose it), TIE (inside the noise: exact pass counts and a 10% spend tolerance when no model was called; a 10 point pass-rate margin and a 15% spend tolerance when one was, because sampled, model-judged runs move that much between re-runs, overridable per component in `noise.json`; kept for the reason in `selected_by`, not by the number), LOSS or UNMEASURED (no rival compared); ablation and baseline arms are tagged; S10 fails when the selected implementation loses without a waiver |
| Place | `docs/structure/components.csv` | `component_id`, `layer`, `path` globs, `role`, `solutions`: which files serve the solution; a file in no component, or a component serving no solution, fails the check (S1, S19) |

Whether "best" can vary decides the kind. An `eval` solution has implementations that differ on the metric, so they are
ranked on the same cases. An `invariant` solution is a rule that holds or does not (a write stays inside its root); it keeps
its cases and a recorded run, is listed apart on the leaderboard, and has no floor on it: a deliberately broken
implementation sits in `<component>/broken/` and one test proves it fails the cases.

Eval folder names equal solution ids with underscores. Where one folder deliberately serves several
solutions (for example one retrieval corpus scoring several sources), its README says so and each
solution row points its `metric_command` at it.

## Adding a solution end to end (a worked example)

Say QAR must `summarize-a-thread`, and two ways exist: a rule-based cut and a model call.

1. **Records.** Fill every field of the template for each record, in the skill's own terms. A problem is a difficulty
   someone has ("long threads overflow the planner view, so replies miss what was asked"), never a task. Add it to
   `problems.csv` (or link an existing one), an ideal to `ideals.csv` (an adjective and a noun, for example "Bounded
   Prompt"), the solution to `solutions.csv` (name, scoped purpose with uses and avoids, goals, knowledge, measures of
   success, interface), one record per implementation to `implementations.csv`, and the metric (its name, what it
   attempts to measure, what is recorded) to `metrics.csv` with `command`
   `esoi run summarize_a_thread --impl rule_cut`. Link both ways: a problem lists its
   `solution_ids` and `ideal_ids`, and the solution and ideal list the problem back.
2. **Port and table.** If callers will swap them, add a `Protocol` in `core/adapters*.py` and a line in
   `core/selection_table.py`; list each implementation file in the row.
3. **Evaluation.** Create `evaluation/components/summarize_a_thread/`: `interface.py` with
   `summarize(case) -> str`, `cases.json` (synthetic threads, a share held out), `score.py`, and
   `impls/rule_cut.py` and `impls/model_call.py` that call the live code. Add a `BASELINE` impl only for a floor
   someone would plausibly ship; if "best" cannot vary, mark the row `invariant` instead and put any broken
   baseline in `broken/`.
4. **Record.** `esoi run summarize_a_thread --impl rule_cut --record`, once per
   implementation. Commit `results.csv` after the code, so `git_sha` is clean.
5. **Place.** Put the new module under the component that owns its role in `components.csv` (add a row
   only for a new role) and list the solution id in that row's `solutions` column.
6. **Check.** `esoi check --write-docs` then `esoi check`.
   If the model call wins but live switching needs a human, a waiver names the owner; otherwise change the
   `selected` cell and the table default. Humans review the leaderboard, not the implementations.

## Adopting it in another repo

Install the `esoi` package and follow its guide, `docs/guides/apply-with-tools.md` in the evolvable-structure repository:
write `esoi.toml`, then `components.csv` first (every file in one component), then `intent.csv` (the project's own why) and the intent
records with a gap noted where no metric exists yet, and turn the gaps into components one at a time. Keep the rules that
fit (`esoi rules` lists them; S1, S10 and S19 are the core) and record any exception in `waivers.csv`, which only shrinks.
QAR's own `esoi.toml`, `scripts/esoi_rules.py` and `evaluation/components/common.py` are a worked example of the
configuration, a plugin and the shared offline builder. The AI behaviour half is `evaluation/components/esoi_behavior/`,
which scores whether an AI follows the principles.

## Record fields

The columns of each record file are `esoi_template.csv`, parsed from the evolvable-structure skill's intent template. After
the skill changes, run `esoi template sync` (it needs `ESOI_TEMPLATE_SOURCE`, or the `template_source` path in `esoi.toml`),
then `esoi check`: S26 names every record missing a new required field and S27 says when the committed template is stale
(it is skipped where the skill is not reachable, as in public CI). Fill those fields with real content, run
`esoi check --write-docs`, and commit. Rows of a solution whose records are still being written are listed in `waivers.csv`
under S26 and can only shrink.
