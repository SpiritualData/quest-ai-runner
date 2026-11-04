# Keeping every part generic and measured

Every part of QAR stays specified as a solution with alternatives and a metric, so the best implementation runs as the state of the art moves and no vendor is built into the brain.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `keep-it-generic:structure-library` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A repository that specifies every part as a solution with alternatives and a metric lets the best implementation run as models and tools change. That is a space beyond the engine the top intent describes: the structure check and the selector belong to the esoi library, but QAR measures whether its assistant behaves by the structure, scores how much of its wiring is configuration and not code, keeps its tests runnable offline and its public code free of secrets, and loads one configuration per lane.

## How the solutions fit together

keep-it-generic keeps every part specified as a solution with measured alternatives. It finds what a change touches through tag-solutions. It measures whether the assistant acts by ESOI through behave-by-esoi and judge-esoi-behavior. It wires the selected arms per lane through configure-a-lane. It keeps the test suite and the public code trustworthy through verify-offline and keep-secrets-out, and scores how much of QAR's wiring is configuration through @hub-of-hubs/score-hub-connections.

## Solutions at a glance

| Solution | Entry call | Selected implementation | Metric |
|---|---|---|---|
| `behave-by-esoi` | no entry declared | `insitu_standing_instructions` | Principle-following pass rate under standing instructions |
| `judge-esoi-behavior` | no entry declared | `model_judge` | Judge agreement with labelled reference replies |
| `verify-offline` | no entry declared | `collect_by_file` | none yet |
| `keep-secrets-out` | no entry declared | `live_scanner` | Secret detection and placeholder tolerance |
| `tag-solutions` | no entry declared | `tag_list_with_check` | Right solution missed without the tag |

## Problems and ideals

**Problems**

- `prob-public-repo-leaks`: A public repository grown from private code can leak keys, real machine paths, real ids or real addresses, and nobody notices until it is published.
- `prob-ai-skips-structure-thinking`: AI collaborators produce changes without naming the problem, the ideal or a metric, duplicate what already exists, or add structure whose cost outweighs its benefit.
- `prob-grader-fooled-by-vocabulary`: A grader for AI replies rewards the right words without the right behaviour, or was calibrated only against its own author's labels, so passing scores mean less than they appear to.
- `prob-solution-not-identified`: A person or a run about to build or change something does not identify the solutions that already cover it or constrain it.
- `esoi-r1-duplicate-solution`: R1: Adding a new solution that duplicates an existing one.
- `esoi-r2-overlap-not-leveraged`: R2: Not leveraging the benefits of an existing solution, such as partial overlap of purpose.
- `esoi-r3-co-change-missed`: R3: Not being aware of solutions that need to change definition or implementation due to something else changing.
- `esoi-r4-solution-misused`: R4: Using a solution when it should not be for the particular use case.

**Ideals**

- `ideal-clean-publication`, Clean Publication: Nothing secret or consumer specific is ever published.
- `ideal-structured-habit`, Structured Habit: An AI that works here names the problem, the ideal and the metric, reuses what exists, and adds only structure that earns its cost.
- `ideal-honest-grading`, Honest Grading: A grade means the reply did what the principle asks, whatever words it used.
- `ideal-risks-identified-by-tag`, Constraining Solutions Identified: Before something is built or changed, every solution that already covers it or constrains it is identified, through a short flat list of tags each of which names the problems it mitigates.

## Metrics and their leaders

- `metric-behave-by-esoi`, Principle-following pass rate under standing instructions: leader `standing_instructions`, 30 of 60 cases
- `metric-judge-esoi-behavior`, Judge agreement with labelled reference replies: leader `model_judge`, 29 of 30 cases
- `metric-verify-offline`, : leader `collect_by_file`, 29 of 29 cases
- `metric-keep-secrets-out`, Secret detection and placeholder tolerance: leader `live_scanner`, 30 of 35 cases
- `metric-tag-solutions`, Right solution missed without the tag: leader `tag_list_with_check`, 13 of 13 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- `prob-public-repo-leaks`: A public repository grown from private code can leak keys, real machine paths, real ids or real addresses, and nobody notices until it is published.
- `prob-ai-skips-structure-thinking`: AI collaborators produce changes without naming the problem, the ideal or a metric, duplicate what already exists, or add structure whose cost outweighs its benefit.
- `prob-grader-fooled-by-vocabulary`: A grader for AI replies rewards the right words without the right behaviour, or was calibrated only against its own author's labels, so passing scores mean less than they appear to.
- `prob-solution-not-identified`: A person or a run about to build or change something does not identify the solutions that already cover it or constrain it. The evolvable-structure skill names four structural risks of this kind, recorded here as its sub-problems R1 to R4: a solution that duplicates an existing one (esoi-r1-duplicate-solution), an existing solution's benefits not leveraged (esoi-r2-overlap-not-leveraged), solutions that must change with something else not noticed (esoi-r3-co-change-missed), and a solution used where it should not be (esoi-r4-solution-misused). The former area column grouped solutions under numbered names that closed none of these risks.
- `esoi-r1-duplicate-solution`: R1: Adding a new solution that duplicates an existing one.
- `esoi-r2-overlap-not-leveraged`: R2: Not leveraging the benefits of an existing solution, such as partial overlap of purpose.
- `esoi-r3-co-change-missed`: R3: Not being aware of solutions that need to change definition or implementation due to something else changing.
- `esoi-r4-solution-misused`: R4: Using a solution when it should not be for the particular use case.

### Ideals

- `ideal-clean-publication`, Clean Publication: Nothing secret or consumer specific is ever published.
- `ideal-structured-habit`, Structured Habit: An AI that works here names the problem, the ideal and the metric, reuses what exists, and adds only structure that earns its cost.
- `ideal-honest-grading`, Honest Grading: A grade means the reply did what the principle asks, whatever words it used.
- `ideal-risks-identified-by-tag`, Constraining Solutions Identified: Before something is built or changed, every solution that already covers it or constrains it is identified, through a short flat list of tags each of which names the problems it mitigates.

### Solutions

| Solution | Name | Selected | Metric |
|---|---|---|---|
| `behave-by-esoi` | Standing-instruction behaviour layer | insitu_standing_instructions | Principle-following pass rate under standing instructions |
| `judge-esoi-behavior` | Behaviour-grading judge | model_judge | Judge agreement with labelled reference replies |
| `verify-offline` |  | collect_by_file | none yet |
| `keep-secrets-out` | Public-repository hygiene scanner | live_scanner | Secret detection and placeholder tolerance |
| `tag-solutions` | Problem-linked tags that identify solutions | tag_list_with_check | Right solution missed without the tag |

### Implementations

| Implementation | Name | Where | Sub-intent |
|---|---|---|---|
| `behave-by-esoi:insitu_standing_instructions` | Shipped deep-run prompt with standing instructions | [evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py](../../evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py) |  |
| `behave-by-esoi:insitu_plain` | In-situ prompt without the instruction layer | [evaluation/components/esoi_behavior/impls/insitu_plain.py](../../evaluation/components/esoi_behavior/impls/insitu_plain.py) |  |
| `behave-by-esoi:standing_instructions` | Chat-completion ablation with instructions | [evaluation/components/esoi_behavior/impls/standing_instructions.py](../../evaluation/components/esoi_behavior/impls/standing_instructions.py) |  |
| `behave-by-esoi:plain` | Chat-completion baseline without instructions | [evaluation/components/esoi_behavior/impls/plain.py](../../evaluation/components/esoi_behavior/impls/plain.py) |  |
| `judge-esoi-behavior:model_judge` | Rubric-based model judge | [evaluation/components/esoi_judge/impls/model_judge.py](../../evaluation/components/esoi_judge/impls/model_judge.py) |  |
| `judge-esoi-behavior:regex_only` | Regex-check floor | [evaluation/components/esoi_judge/impls/regex_only.py](../../evaluation/components/esoi_judge/impls/regex_only.py) |  |
| `verify-offline:conftest` |  | [tests/conftest.py](../../tests/conftest.py) |  |
| `verify-offline:collect_by_file` |  | [evaluation/components/verify_offline/impls/collect_by_file.py](../../evaluation/components/verify_offline/impls/collect_by_file.py) |  |
| `keep-secrets-out:live_scanner` | Planted-secret evaluation arm | [evaluation/components/keep_secrets_out/impls/live_scanner.py](../../evaluation/components/keep_secrets_out/impls/live_scanner.py) |  |
| `tag-solutions:tag_list_with_check` | Problem-linked tag list with a check rule and a lookup | [scripts/esoi_tags.py](../../scripts/esoi_tags.py) |  |

### Metrics

- `metric-behave-by-esoi`, Principle-following pass rate under standing instructions: `esoi run esoi_behavior --impl insitu_standing_instructions`
- `metric-judge-esoi-behavior`, Judge agreement with labelled reference replies: `esoi run esoi_judge --impl model_judge`
- `metric-verify-offline`, : `esoi run verify_offline --impl collect_by_file`
- `metric-keep-secrets-out`, Secret detection and placeholder tolerance: `esoi run keep_secrets_out --impl live_scanner`
- `metric-tag-solutions`, Right solution missed without the tag: `esoi run tag_solutions --impl tag_list_with_check`
