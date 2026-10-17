# Evolvable structure (ESOI) in quest-ai-runner

This folder is the source of truth for how the repo is organised and why. It applies the
evolvable-structure (ESOI) philosophy (structure that is stable where it should be and changeable where
change is expected) to QAR's architecture, not only to individual modules. `CLAUDE.md` is the
pre-flight checklist; `ARCHITECTURE_STANDARDS.md` is the reasoning for the code rules; this folder is
the inventory and the automated check.

| File | What it holds | Edited by |
|---|---|---|
| `components.csv` | One row per functional component: path globs, role, interface, metric, docs, tests, ideals met, gap, action, status | people and AI, by hand |
| `solutions.csv` | One row per solution: the problem, the ideal, the port, every implementation, the selected one (the selection-table name when the solution has a port) and how it is selected, the `metric_kind` (`eval`, or `invariant` with its reason) and the metric and its command | people and AI, by hand |
| `principles.csv` | The 13 principles of the evolvable-structure skill in short, with how each is measured here | people and AI, by hand |
| `tracker/*.csv` | The ESOI intent network as five tabs (problems, solutions, implementations, ideals, metrics), each with `tracking_*` columns; import each as a Google Sheet tab | generated: `python scripts/build_tracker.py` (tracking columns are hand-edited and preserved) |
| `audit.csv` | Every component against principles P1 to P13: objective facts (size, unmeasured solutions, waivers) plus a hand-written finding and action; `tracking_*` columns for review | generated: `python scripts/build_audit.py` (finding, action and tracking columns are hand-edited and preserved) |
| `STANDARD.md` | The one shape every solution takes (row, port, selection entry, `evaluation/components/<id>/`, recorded result, leaderboard), a worked example of adding one, and how another repo adopts this | people and AI, by hand |
| `waivers.csv` | Known, justified exceptions to a structure rule. Each must still apply, so the list only shrinks | people and AI, by hand |
| `COMPONENTS.md` | A readable table of `components.csv` | generated: `python scripts/check_structure.py --write-docs` |

The check is `python scripts/check_structure.py` (no network, no model calls). It also runs as
`tests/test_structure_registry.py`, so a normal test run fails when the structure drifts.

## The idea in QAR terms

A **problem** is something QAR must do for a user (route a message, gather evidence, call a model, run
deep work). A **solution** is a named port plus the implementations behind it. The network between
problems and solutions is `solutions.csv`. A **component** is a place in the tree that owns a role;
`components.csv` is where each one is defined, found and measured.

| Principle (evolvable-structure skill) | How QAR meets it | Where it is enforced |
|---|---|---|
| P1 Practical evolvable structure first | One registry of components; AI scales the structure | S1, S2 |
| P2 Ontology of solutions, ideals, metrics | Ports (`Protocol`/`ABC`); one solution row per problem whose "best" differs, each with an `ideal` and a `metric_command` or a stated gap | S7, S11 |
| P3 Source of truth for solutions | `solutions.csv` (which implementation), `components.csv` (where), `tracker/` (problems, solutions, implementations, ideals, metrics with tracking) | S1, S7, S17 |
| P4 Automatic use of the best implementation | `selected` and `selected_by` name the live choice; `core/selection_table.py` resolves it; the leaderboard produces the deciding number | S10, S14 |
| P5 Structure improves over time | `status`, `gap`, `action` columns; waivers can only shrink | S2, S9 |
| P6 High-level principles get the same treatment | This folder applies the philosophy to the architecture; `evaluation/components/esoi_behavior/` scores whether the AI's behaviour follows ESOI | the check, esoi_behavior |
| P7 Humility | Metrics decide, not opinion; unmeasured solutions are flagged | S7, S16 |
| P8 Leverage existing structure | Reference adapters, vendored code listed, providers behind `MultiProvider` | S1 |
| P9 No duplication | Role mixins and helper modules instead of copies; a port is defined once | S4 |
| P10 Long-term outcomes cascade to tasks | Real behaviour is measured by the evaluation harness, not only unit tests | `evaluation/components/` |
| P11 Avoid retaining what is not useful | One registry, iterated in place; no per-session files | S3 |
| P12 Minimalism | Configuration enters through `RunnerConfig` only; a lane is a config file | S13, S15 |
| P13 Clarity | Module names state a role; every module opens with a docstring; each component lists its docs | S2, S5 |

The `rules` column of `components.csv` uses these P-numbers (the skill was renamed from adaptable-structure
on 2026-10-15 and its principles were renumbered; the old list is retired).

## Rules the check enforces

* **S1 coverage.** Every file in the repo belongs to exactly one component and every path glob matches
  a file. In a glob, `*` stays inside one folder and `**` crosses folders.
* **S2 component rows.** Docs and tests named by a row exist. A row that is not `done` states its gap
  and its action.
* **S3 root.** No `.py` file at the repo root.
* **S4 size.** No module over 1,500 lines. A size problem is solved by splitting by role and
  re-exporting the old names from the original module so imports keep working. Tests and the legacy
  evaluation scripts are waived (see `waivers.csv`).
* **S5 docstring.** Every library module opens with a docstring that says its role.
* **S6 direction.** Dependencies point inward along one order: core < adapters < runner < config <
  cli/ui/tools. A module's layer is its package, or for top-level modules the `layer` of its
  `components.csv` row (`config*`, `usage`, `pricing`, `resources` are config; `cli*`,
  `interactive_session*`, `textual_*` are cli). A module may import its own layer or a lower one. Lazy
  imports count. `TYPE_CHECKING`-only imports are ignored (they create no runtime dependency). The runner takes the
  config layer's builders through `runner/config_wiring.py`, registered by the package `__init__`, so
  `runner` no longer imports `config`; `usage`, `pricing` and `resources` are import-free leaves in the
  `core` layer. The one remaining violation (a runner to UI import) is waived with a reason.
* **S13 config.** No `os.environ` / `os.getenv` in `core/` (no waivers). Core reads its tunables from
  `core.runtime_settings.CoreSettings`, which the config layer builds from the environment
  (`config_core_settings.py`) or a consumer pins with `RunnerConfig.core_settings`.
* **S15 env docs.** Every environment variable read in `quest_ai_runner/` is a row of
  `docs/configuration.md`, which `--write-docs` generates from the code (`scripts/env_vars.py`) with
  descriptions from `docs/structure/env_descriptions.csv`. A variable with no description and no doc that
  explains it fails; a rare exemption is an `S15,<VARIABLE>` waiver.
* **S7 solutions.** Every `Protocol` class under `quest_ai_runner/` (and `CardRepository`) is listed in the `port`
  cell of some solution row. A row may list several ports separated by `;`, so a port needs no row of its own, and
  a row exists only for a problem a user has. For a solution with a selection-table entry `selected` is one of the
  table's names; for any other it is one of the listed implementations. Implementation paths and metric scripts
  exist; a solution without a metric states its gap and action.
* **S8 generated docs.** `COMPONENTS.md` matches `components.csv`.
* **S18 ids.** Solution, component and tracker ids are unique (a duplicated id silently merged two things).
* **S19 serves.** Every component names at least one solution it serves. Infrastructure counts: configuration,
  the CLI, the tests, the docs and the evaluation harness each serve a named solution with an ideal and a metric.
  Code that serves none is removed, not waived.
* **S20 complete.** Every solution has a problem, an ideal, implementations, a metric, a selected
  implementation and a recorded run on the current cases with a `git_sha` (`--strict` also rejects a run
  recorded on uncommitted code). "Tests only" is not a metric. A live-only metric needs an `S20,<solution_id>`
  waiver saying why.
* **S9 waivers.** A waiver that no longer applies is deleted, and every waiver has a reason.

## When you change the repo

* **New module:** put it in the component that owns its role (a new method goes in the mixin that owns
  the role, see `CLAUDE.md`). If the glob already covers it, nothing else is needed. If it is a new
  role, add a row to `components.csv`, run `--write-docs`, and write or extend the doc the row names.
* **New implementation of a port:** add it to `implementations` in `solutions.csv`. Select it only
  when its metric beats the current one on the same cases.
* **New solution or port:** add a row with its problem, ideal and metric. If there is no metric yet,
  say so in `gap` and `action`; the next person then knows what to build.
* **New evaluation:** add `evaluation/components/<component>/` with one function behind a fixed
  signature and a `cases.json`, then point the solution's `metric_command` at it. Results and private
  data go in the quest working folder, not here.
* **New exception:** add a row to `waivers.csv` with a reason. Prefer fixing the cause.
* **Always:** run `python scripts/check_structure.py` before committing.
* **S10 selection.** The selected implementation of an `eval` solution must be the leaderboard winner
  (`evaluation/components/leaderboard.py`), and must have a recorded row in `results.csv`. A challenger
  that wins, or an unrecorded selection, needs a waiver naming the pending decision and its owner, so
  a better implementation cannot sit unnoticed (ESOI principle 4). An `invariant` solution is never ranked
  (there is nothing to beat), but its selected implementation still needs a recorded run, and the leaderboard
  lists it apart under "Invariant solutions".
* **S11 metric kind.** Every component row has `metric_kind`: `eval` (a `components/run.py` command with a
  recorded result), `invariant` (pinned by named tests), `exempt` (metric text starts `exempt:` with the
  reason) or `none` (never `done`; state gap and action). `solutions` links a component to its
  `solutions.csv` ids, which must exist. A harness command in `solutions.csv` must name a component with
  `score.py` and an `impls/<impl>.py`.
* **Ports.** S7 finds every `Protocol` class under `quest_ai_runner/` (not only `core/adapters*.py`);
  each needs a `solutions.csv` row.
* **Solution kinds (S11).** Every solution row has `metric_kind`: `eval` (the best implementation can vary, so
  implementations are ranked on the same cases) or `invariant` (a rule that holds or does not, such as a write
  staying inside its root; `invariant_reason` says in one line why "best" cannot vary). An invariant solution keeps
  its cases and recorded run but has no leaderboard floor: a deliberately broken implementation lives in
  `evaluation/components/<x>/broken/` and a test (`evaluation/components/test_components.py`) proves it fails the
  cases. Only a floor someone would plausibly ship (`dump_all`, `most_recent`, `keyword_rules`) stays an `impls/`
  arm of an `eval` solution.
* **Invariant reason (S11).** `metric_kind=invariant` needs `invariant_reason`: one line on why "best"
  cannot vary for that component. A component whose quality can vary (prompt size, for one) is `partial`
  with `metric_kind=none`, not an invariant. Every solution must be linked from some component's
  `solutions` column, or carry an `S11 solution:<id>` waiver.
* **Results are comparable.** `results.csv` records `git_sha` and `cases_sha` with each run. `model` records
  the model for runs that make model calls. `leaderboard.py` compares rows only when `cases_sha` matches;
  rows from before the column existed have it blank and compare as one group. The committed record is
  `evaluation/components/results.csv`; per-run JSON goes to `/tmp/qar_components/`. `evaluation/components` is in pytest `testpaths`.
* **S18 ids.** Solution, component and tracker ids are unique, and no id repeats inside one cell (a `solutions`,
  `port`, `implementations` or `path` list). (Listed here beside the rules it extends.)
* **S12 measured code.** Every `evaluation/components/*/impls/*.py` either imports or drives live
  `quest_ai_runner` code, or declares itself an alternative (`ALTERNATIVE = True`, or a docstring that opens
  with `ALTERNATIVE`, `BASELINE`, `ABLATION` or `FLOOR`). An implementation that does neither would put a
  re-implementation's number on the leaderboard, not the live code's. A solution's `selected` implementation, and the
  harness implementation its `metric_command` measures, must be live ones, or carry an `S12 <solution_id>:<impl>`
  waiver. (`context_selection`'s
  `hybrid_union` wraps the live `HybridContextAssembler`, `FileContextStore` and `VectorContextAssembler`;
  `vector_bge_small`, `blind_grep` and `idf_paths_only` are declared baselines.)
* **S23 selected is the table default.** For a solution with a selection-table entry, `selected` in `solutions.csv`
  is that entry's default implementation name, so the registry, the table and the live default cannot name three
  different things. (`call-a-model` selects `multi_provider`, the router every model call goes through; the vendors
  are the primaries it wraps.)
* **S21 evaluation folders.** Every `evaluation/components/<x>/` folder is named by some solution: a `metric_command`
  (`run.py <x>`), or an `evaluation/components/<x>` mention in a `metric` (a declared arm of that solution), or it is
  shared infrastructure listed in `SHARED_EVAL_DIRS` in `scripts/check_structure.py`. An orphan eval folder is deleted.
* **S22 numbers.** No score, percentage, dollar amount or case count in a solution's `metric`, `gap` or `action`, a
  component's `gap` or `action`, a waiver reason or an audit `finding` or `action`. A number there is stale the next
  time the code or the cases change. Numbers live only in `results.csv`, read through the leaderboard; the prose names
  what was measured and what is missing.
* **S14 selection table.** Selection is data. `quest_ai_runner/core/selection_table.py` maps each
  `solutions.csv` id that has a port to its implementations (`name -> "module:Class"`) and a default;
  `core/resolve.py` turns `resolve(solution_id, name)` into the class (name, then
  `RunnerConfig.selections[solution_id]`, then the default; a typo raises with the choices). The check fails
  when a port row has no table entry, when a default is not one of its implementations, or when an
  implementation's module is not in the row's `implementations`. Used today by the model provider
  (`cli_config._model_provider_from_env`), the search backends (`select_search_backend`), the vector store
  (`config_assembler`) and the deep runner (`resolve_deep_runner`). To switch one, set
  `[selections] "run-deep-work" = "acp"` in a lane's config file (or edit the table default); to add one, add a
  table line and list its file in `solutions.csv`. `resolve.py` reads no environment: the config layer decides
  the name and passes it in.
* **S16 provenance.** The latest `results.csv` row of every implementation must carry a `cases_sha`, or an
  `S16 <component>/<impl>` waiver says why it cannot be re-recorded. `leaderboard.py` marks rows without one
  `unverified` and never treats them as evidence for or against a verified row. The classification and
  quest_ops rows are waived: recorded before the provenance columns, not re-runnable without the claude CLI.
