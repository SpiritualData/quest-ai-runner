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
| Metric | `docs/structure/metrics.csv` | one record per metric: the template fields plus `solution_ids` and `command` (an `evaluation/components/run.py <component> --impl <impl>` command per arm) |
| Evaluation | `evaluation/components/<solution_id with underscores>/` | `interface.py` (one function with a fixed signature), `cases.json`, `score.py`, `impls/<impl>.py` driving LIVE code (S12) |
| Result | `evaluation/components/results.csv` | one row per `run.py <component> --impl <impl> --record`, with `git_sha` and `cases_sha` (S16) |
| Decision | `evaluation/components/leaderboard.py` | the winner on the same cases, and a verdict per solution: WIN (the metric chose it), TIE (inside the noise: exact pass counts and a 10% spend tolerance when no model was called; a 10 point pass-rate margin and a 15% spend tolerance when one was, because sampled, model-judged runs move that much between re-runs, overridable per component in `noise.json`; kept for the reason in `selected_by`, not by the number), LOSS or UNMEASURED (no rival compared); ablation and baseline arms are tagged; S10 fails when the selected implementation loses without a waiver |
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
   `evaluation/components/run.py summarize_a_thread --impl rule_cut`. Link both ways: a problem lists its
   `solution_ids` and `ideal_ids`, and the solution and ideal list the problem back.
2. **Port and table.** If callers will swap them, add a `Protocol` in `core/adapters*.py` and a line in
   `core/selection_table.py`; list each implementation file in the row.
3. **Evaluation.** Create `evaluation/components/summarize_a_thread/`: `interface.py` with
   `summarize(case) -> str`, `cases.json` (synthetic threads, a share held out), `score.py`, and
   `impls/rule_cut.py` and `impls/model_call.py` that call the live code. Add a `BASELINE` impl only for a floor
   someone would plausibly ship; if "best" cannot vary, mark the row `invariant` instead and put any broken
   baseline in `broken/`.
4. **Record.** `python evaluation/components/run.py summarize_a_thread --impl rule_cut --record`, once per
   implementation. Commit `results.csv` after the code, so `git_sha` is clean.
5. **Place.** Put the new module under the component that owns its role in `components.csv` (add a row
   only for a new role) and list the solution id in that row's `solutions` column.
6. **Check.** `python scripts/check_structure.py --write-docs` then `python scripts/check_structure.py`.
   If the model call wins but live switching needs a human, a waiver names the owner; otherwise change the
   `selected` cell and the table default. Humans review the leaderboard, not the implementations.

## Adopting it in another repo

Copy `docs/structure/`, `scripts/sync_esoi_template.py`, `quest_ai_runner/structure/records.py`, `scripts/check_structure.py`, `scripts/env_vars.py` and
`evaluation/components/{common,run,leaderboard,expect}.py`. Write `components.csv` first (every file in
one component), then the intent records (problems first, then the solutions that address them) with a gap noted where no metric exists yet, and
turn the gaps into components one at a time. Keep the rules that fit (S1, S10, S19 are the core) and
record any exception in `waivers.csv`, which only shrinks. `scripts/build_tracker.py` is optional: it
produces the Sheet view of the registry and nothing depends on it except check S17. The AI behaviour half
is `evaluation/components/esoi_behavior/`, which scores whether an AI follows the principles.

## Record fields (generated)

## After the template changes

The template is the skill's intent template definitions; it is refined over time. To refresh the records after an edit:
run `python scripts/sync_esoi_template.py` (it rewrites `esoi_template.csv` and the generated section below and
reports what it parsed, skipped or had to guess; `--check` fails when they are stale), then run
`python scripts/check_structure.py`. Rule S26 names every record that is missing a new required field (S27 says
when the committed template is stale against the skill, and is skipped where the skill is not reachable). Fill those
fields with real content, run `--write-docs`, and commit. Rows of a solution whose records are still being written
are listed in `waivers.csv` under S26 and can only shrink.

<!-- esoi-template:start (generated by scripts/sync_esoi_template.py; do not edit) -->

Generated from the skill's intent template by `python scripts/sync_esoi_template.py`; the
records in this folder carry exactly these columns (a `Specify:` field is required, an interface field is
optional and meant for a solution that is technology).

**Problem**: A description of what motivated the intent. A challenge, difficulty, or description of how an ideal is not reached.

| Column | Required | Question |
|---|---|---|
| `problem` | yes | What is the problem? |
| `who_has_it` | yes | Who or what type of person has the problem? |
| `why_it_matters` | yes | Why does this problem matter? |
| `why_hard` | yes | Why is the problem hard to resolve? |
| `existing_attempts` | yes | What solutions or workarounds already attempt to address the prompt? |

What it is not: A decision, task, recommendation, or component, but all of these can *have* problems.

Connects to: Ideals that oppose at least one aspect of the problem, Solutions that would solve at least one aspect of the problem

**Solution**: A definition of how one or more problems can be addressed to fulfill the intent.

| Column | Required | Question |
|---|---|---|
| `name` | yes | What's a name that describes the type of solution it is? |
| `scoped_purpose` | yes | What types of use cases could the solution be used for? What type of use cases is the solution intentionally avoiding? |
| `goals` | yes | What are the solution's measureable goals? |
| `knowledge` | yes | What knowledge informs the solution's definition or implementations? |
| `measures_of_success` | yes | What would indicate the solution is successful? |
| `parameters` | no | Parameters |
| `inputs` | no | Inputs |
| `outputs` | no | Outputs |
| `effects` | no | Effects (other than outputs) |
| `usage_notes` | no | Other information on when and how to use the solution |

What it is not: Fully functional code or solution, but it can be an abstract class, template, or routing which a separate structure solution network defines.

Connects to: Implementations that build what the solution defines, Metrics that define and implement the solution's measures of success, Problems the solution addresses,

**Implementation**: The built structure which carries out its associated solution; also known as an alternative.

| Column | Required | Question |
|---|---|---|
| `name` | yes | What's a name that distinguishes this implementation from other alternatives? |
| `description` | yes | How does the implementation achieve the goals of its solution's definition? |
| `advantages` | yes | Advantages |
| `disadvantages` | yes | Disadvantages |

What it is not: What endusers of the relevant solution directly interact with. The solution provides an interface on top of the implementation. A structure solution defines how solution's will define interfaces.

**Ideal**: A definition of the future state implied by the intent, in a particular dimension or aspect.

| Column | Required | Question |
|---|---|---|
| `name` | yes | What's a combined noun with adjective that represents the ideal's direction of interest? |
| `principle` | yes | What does this ideal ask for? |
| `state_description` | yes | Describe what it would be like to experience this ideal. |
| `hints_of_measurement` | yes | How would you know this ideal is reached? |

What it is not: A practical metric or solution, or technology.

Connects to: Problems the ideal opposes.

**Metric**: A practical way to measure the distance between the implementations of a solution and an ideal which the solution was defined to achieve. Alternative implementations are compared via metrics. Note: Metrics can also be solutions, as metrics themselves may be refined over time with alternative implementations (including dataset versions).

| Column | Required | Question |
|---|---|---|
| `name` | yes | What's a name that clarifies what is being measured? |
| `scoped_purpose` | yes | What does this metric attempt to measure? |
| `record` | yes | What is recorded as the metric to compare implementations? |

<!-- esoi-template:end -->
