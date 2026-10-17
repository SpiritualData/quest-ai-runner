# The standard shape for applying ESOI (copy this to another repo)

One solution id names every part of one idea. Nothing in the repo exists outside a solution, and a
solution is not done until a recorded run says how well it works. The rules that enforce this are in
[README.md](README.md); the philosophy is the evolvable-structure skill (`principles.csv` is its short form).

## The shape of one solution

| Part | Where | Holds |
|---|---|---|
| Row | `docs/structure/solutions.csv` | `solution_id`, the problem, the ideal, the port, every implementation, `selected` and `selected_by`, the metric and its `metric_command`, any `gap` and `action` |
| Port | `quest_ai_runner/core/adapters*.py` (a `Protocol`), only when implementations are swappable | the interface; callers depend on it, never on an implementation |
| Selection | `quest_ai_runner/core/selection_table.py` (S14) | `name -> "module:Class"` and a default; `resolve(solution_id, name)` returns the class |
| Evaluation | `evaluation/components/<solution_id with underscores>/` | `interface.py` (one function with a fixed signature), `cases.json`, `score.py`, `impls/<impl>.py` driving LIVE code (S12) |
| Result | `evaluation/components/results.csv` | one row per `run.py <component> --impl <impl> --record`, with `git_sha` and `cases_sha` (S16) |
| Decision | `evaluation/components/leaderboard.py` | the winner on the same cases; S10 fails when the selected implementation loses without a waiver |
| Place | `docs/structure/components.csv` | which files serve the solution (`solutions` column); a file in no component, or a component serving no solution, fails the check (S1, S19) |

Eval folder names equal solution ids with underscores. Where one folder deliberately serves several
solutions (for example one retrieval corpus scoring several sources), its README says so and each
solution row points its `metric_command` at it.

## Adding a solution end to end (a worked example)

Say QAR must `summarize-a-thread`, and two ways exist: a rule-based cut and a model call.

1. **Row.** Append to `solutions.csv`: problem ("long threads overflow the planner view"), ideal
   ("the summary keeps every open ask and decision, in under 400 characters"), implementations, a draft
   `metric` ("share of fixture threads whose summary keeps all marked facts"), and `metric_command`
   `evaluation/components/run.py summarize_a_thread --impl rule_cut`.
2. **Port and table.** If callers will swap them, add a `Protocol` in `core/adapters*.py` and a line in
   `core/selection_table.py`; list each implementation file in the row.
3. **Evaluation.** Create `evaluation/components/summarize_a_thread/`: `interface.py` with
   `summarize(case) -> str`, `cases.json` (synthetic threads, a share held out), `score.py`, and
   `impls/rule_cut.py` and `impls/model_call.py` that call the live code. Add a `BASELINE` impl if a naive
   floor helps show the metric discriminates.
4. **Record.** `python evaluation/components/run.py summarize_a_thread --impl rule_cut --record`, once per
   implementation. Commit `results.csv` after the code, so `git_sha` is clean.
5. **Place.** Put the new module under the component that owns its role in `components.csv` (add a row
   only for a new role) and list the solution id in that row's `solutions` column.
6. **Check.** `python scripts/check_structure.py --write-docs` then `python scripts/check_structure.py`.
   If the model call wins but live switching needs a human, a waiver names the owner; otherwise change the
   `selected` cell and the table default. Humans review the leaderboard, not the implementations.

## Adopting it in another repo

Copy `docs/structure/`, `scripts/check_structure.py`, `build_tracker.py`, `build_audit.py` and
`evaluation/components/{common,run,leaderboard,expect}.py`. Write `components.csv` first (every file in
one component), then one `solutions.csv` row per problem with a gap noted where no metric exists yet, and
turn the gaps into components one at a time. Keep the rules that fit (S1, S10, S19 are the core) and
record any exception in `waivers.csv`, which only shrinks. The AI behaviour half is
`evaluation/components/esoi_behavior/`, which scores whether an AI follows the principles.
