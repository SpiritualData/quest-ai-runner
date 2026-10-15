# Adaptable structure in quest-ai-runner

This folder is the source of truth for how the repo is organised and why. It applies the
adaptable-structure philosophy (structure that is stable where it should be and changeable where
change is expected) to QAR's architecture, not only to individual modules. `CLAUDE.md` is the
pre-flight checklist; `ARCHITECTURE_STANDARDS.md` is the reasoning for the code rules; this folder is
the inventory and the automated check.

| File | What it holds | Edited by |
|---|---|---|
| `components.csv` | One row per functional component: path globs, role, interface, metric, docs, tests, ideals met, gap, action, status | people and AI, by hand |
| `solutions.csv` | One row per swappable solution: the problem, the ideal, the port, every implementation, the selected one and how it is selected, the metric and its command | people and AI, by hand |
| `waivers.csv` | Known, justified exceptions to a structure rule. Each must still apply, so the list only shrinks | people and AI, by hand |
| `COMPONENTS.md` | A readable table of `components.csv` | generated: `python scripts/check_structure.py --write-docs` |

The check is `python scripts/check_structure.py` (no network, no model calls). It also runs as
`tests/test_structure_registry.py`, so a normal test run fails when the structure drifts.

## The idea in QAR terms

A **problem** is something QAR must do for a user (route a message, gather evidence, call a model, run
deep work). A **solution** is a named port plus the implementations behind it. The network between
problems and solutions is `solutions.csv`. A **component** is a place in the tree that owns a role;
`components.csv` is where each one is defined, found and measured.

| Ideal | How QAR meets it | Where it is enforced |
|---|---|---|
| 1 Ontology of solutions | Ports (`Protocol`/`ABC`) in `core/adapters.py`; one solution row per problem whose "best" differs | S7 (every port has a row) |
| 2 An ideal and a metric per solution | `ideal` and `metric_command` columns; a missing metric must state its gap and action | S7 |
| 3 Best implementation used automatically | `selected` and `selected_by` name the one live choice. Today it is set by config or by hand; the evaluation harness produces the number that decides it | partial (see gaps in `COMPONENTS.md`) |
| 4 Principles applied proactively | This folder plus the S rules apply the philosophy to the architecture itself | the check |
| 5 One source of truth | `solutions.csv` (which implementation) and `components.csv` (where) | S1, S7 |
| 6 Structure improves over time | `status`, `gap`, `action` columns; waivers can only shrink | S2, S9 |
| 7 Minimalism | Configuration enters through `RunnerConfig` only; a lane is a config file | CLAUDE.md hard rule 2 |
| 8 Leverage existing structure | Reference adapters, vendored code listed, providers behind `MultiProvider` | S1 (`vendor` is a component) |
| 9 Humility | Metrics decide, not opinion; unmeasured solutions are flagged rather than assumed good | S7 |
| 10 Clarity | Module names state a role; every module opens with a docstring; each component lists its docs | S5, S2 |
| 11 No duplication | Role mixins and helper modules instead of copies; a port is defined once | S1 |
| 12 Keep only what is useful | One registry, iterated in place; no per-session files; scratch lives in `scratch/` | S3 |
| 13 Long-term outcomes down to tasks | Real behaviour is measured by the evaluation harness, not only unit tests | `evaluation/components/` |

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
* **S6 direction.** Dependencies point inward. `core/` imports nothing from `adapters/`, `runner/`,
  `config`, the CLI or the tools package. `adapters/` imports nothing from `runner/`, `config` or the
  CLI. Lazy imports count.
* **S7 solutions.** Every port in `core/adapters.py` (and `CardRepository`) has a row; the selected
  implementation is one of the listed ones; implementation paths and metric scripts exist; a solution
  without a metric states its gap and action.
* **S8 generated docs.** `COMPONENTS.md` matches `components.csv`.
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
* **S10 selection.** The selected implementation of a measured solution must be the leaderboard winner
  (`evaluation/components/leaderboard.py`), and must have a recorded row in `results.csv`. A challenger
  that wins, or an unrecorded selection, needs a waiver naming the pending decision and its owner, so
  a better implementation cannot sit unnoticed (ideal 3).
* **S11 metric kind.** Every component row has `metric_kind`: `eval` (a `components/run.py` command with a
  recorded result), `invariant` (pinned by named tests), `exempt` (metric text starts `exempt:` with the
  reason) or `none` (never `done`; state gap and action). `solutions` links a component to its
  `solutions.csv` ids, which must exist. A harness command in `solutions.csv` must name a component with
  `score.py` and an `impls/<impl>.py`.
* **Ports.** S7 finds every `Protocol` class under `quest_ai_runner/` (not only `core/adapters*.py`);
  each needs a `solutions.csv` row.
* **Layers (S6).** `core` imports nothing outward; `adapters` imports no runner, config or UI; `runner`
  imports no CLI or UI. A forbidden name also bans modules that start with it (`config_*`).
* **Results are comparable.** `results.csv` records `git_sha` and `cases_sha` with each run. Compare
  rows only when `cases_sha` matches. `evaluation/components` is in pytest `testpaths`.
