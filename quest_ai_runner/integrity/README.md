# Integrity

Every part of QAR stays specified as a solution with alternatives and a metric, so the best implementation runs as the state of the art moves and no vendor is built into the brain.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `system-integrity:structure-library` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A repository that specifies every part as a solution with alternatives and a metric lets the best implementation run as models and tools change. That is a space beyond the engine the top intent describes: the structure check and the selector belong to the esoi library, but QAR measures whether its assistant behaves by the structure, scores how much of its wiring is configuration and not code, keeps its tests runnable offline and its public code free of secrets, and loads one configuration per lane.

## How the solutions fit together

Integrity keeps every part specified as a solution with measured alternatives. It finds what a change touches through Problem-linked tags that identify solutions. It measures whether the assistant acts by ESOI through Standing-instruction behaviour layer and Behaviour-grading judge. It wires the selected arms per lane through Layered lane configuration loader. It keeps the test suite and the public code trustworthy through Offline test collection and Public-repository hygiene scanner, and scores how much of QAR's wiring is configuration through @hub-of-hubs/score-hub-connections.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Standing-instruction behaviour layer | `behave-by-esoi` | no entry declared | Shipped deep-run prompt with standing instructions (`insitu_standing_instructions`) | Principle-following pass rate under standing instructions |
| Behaviour-grading judge | `judge-esoi-behavior` | no entry declared | Rubric-based model judge (`model_judge`) | Judge agreement with labelled reference replies |
| Offline test collection | `verify-offline` | no entry declared | Collection check by test file (`collect_by_file`) | Offline test collection |
| Public-repository hygiene scanner | `keep-secrets-out` | no entry declared | Planted-secret evaluation arm (`live_scanner`) | Secret detection and placeholder tolerance |
| Problem-linked tags that identify solutions | `tag-solutions` | no entry declared | Problem-linked tag list with a check rule and a lookup (`tag_list_with_check`) | Right solution missed without the tag |

## Problems and ideals

**Problems**

- A public repository grown from private code can leak keys, real machine paths, real ids or real addresses, and nobody notices until it is published. (`prob-public-repo-leaks`)
- AI collaborators produce changes without naming the problem, the ideal or a metric, duplicate what already exists, or add structure whose cost outweighs its benefit. (`prob-ai-skips-structure-thinking`)
- A grader for AI replies rewards the right words without the right behaviour, or was calibrated only against its own author's labels, so passing scores mean less than they appear to. (`prob-grader-fooled-by-vocabulary`)
- A person or a run about to build or change something does not identify the solutions that already cover it or constrain it. (`prob-solution-not-identified`)
- R1: Adding a new solution that duplicates an existing one. (`esoi-r1-duplicate-solution`)
- R2: Not leveraging the benefits of an existing solution, such as partial overlap of purpose. (`esoi-r2-overlap-not-leveraged`)
- R3: Not being aware of solutions that need to change definition or implementation due to something else changing. (`esoi-r3-co-change-missed`)
- R4: Using a solution when it should not be for the particular use case. (`esoi-r4-solution-misused`)

**Ideals**

- Clean Publication (`ideal-clean-publication`): Nothing secret or consumer specific is ever published.
- Structured Habit (`ideal-structured-habit`): An AI that works here names the problem, the ideal and the metric, reuses what exists, and adds only structure that earns its cost.
- Honest Grading (`ideal-honest-grading`): A grade means the reply did what the principle asks, whatever words it used.
- Constraining Solutions Identified (`ideal-risks-identified-by-tag`): Before something is built or changed, every solution that already covers it or constrains it is identified, through a short flat list of tags each of which names the problems it mitigates.

## Metrics and their leaders

- Principle-following pass rate under standing instructions (`metric-behave-by-esoi`): leader `standing_instructions`, 30 of 60 cases
- Judge agreement with labelled reference replies (`metric-judge-esoi-behavior`): leader `model_judge`, 29 of 30 cases
- Offline test collection (`metric-verify-offline`): leader `collect_by_file`, 29 of 29 cases
- Secret detection and placeholder tolerance (`metric-keep-secrets-out`): leader `live_scanner`, 30 of 35 cases
- Right solution missed without the tag (`metric-tag-solutions`): leader `tag_list_with_check`, 13 of 13 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- A public repository grown from private code can leak keys, real machine paths, real ids or real addresses, and nobody notices until it is published. (`prob-public-repo-leaks`)
- AI collaborators produce changes without naming the problem, the ideal or a metric, duplicate what already exists, or add structure whose cost outweighs its benefit. (`prob-ai-skips-structure-thinking`)
- A grader for AI replies rewards the right words without the right behaviour, or was calibrated only against its own author's labels, so passing scores mean less than they appear to. (`prob-grader-fooled-by-vocabulary`)
- A person or a run about to build or change something does not identify the solutions that already cover it or constrain it. (`prob-solution-not-identified`)
- R1: Adding a new solution that duplicates an existing one. (`esoi-r1-duplicate-solution`)
- R2: Not leveraging the benefits of an existing solution, such as partial overlap of purpose. (`esoi-r2-overlap-not-leveraged`)
- R3: Not being aware of solutions that need to change definition or implementation due to something else changing. (`esoi-r3-co-change-missed`)
- R4: Using a solution when it should not be for the particular use case. (`esoi-r4-solution-misused`)

### Ideals

- Clean Publication (`ideal-clean-publication`): Nothing secret or consumer specific is ever published.
- Structured Habit (`ideal-structured-habit`): An AI that works here names the problem, the ideal and the metric, reuses what exists, and adds only structure that earns its cost.
- Honest Grading (`ideal-honest-grading`): A grade means the reply did what the principle asks, whatever words it used.
- Constraining Solutions Identified (`ideal-risks-identified-by-tag`): Before something is built or changed, every solution that already covers it or constrains it is identified, through a short flat list of tags each of which names the problems it mitigates.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Standing-instruction behaviour layer | `behave-by-esoi` | Shipped deep-run prompt with standing instructions (`insitu_standing_instructions`) | Principle-following pass rate under standing instructions |
| Behaviour-grading judge | `judge-esoi-behavior` | Rubric-based model judge (`model_judge`) | Judge agreement with labelled reference replies |
| Offline test collection | `verify-offline` | Collection check by test file (`collect_by_file`) | Offline test collection |
| Public-repository hygiene scanner | `keep-secrets-out` | Planted-secret evaluation arm (`live_scanner`) | Secret detection and placeholder tolerance |
| Problem-linked tags that identify solutions | `tag-solutions` | Problem-linked tag list with a check rule and a lookup (`tag_list_with_check`) | Right solution missed without the tag |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Shipped deep-run prompt with standing instructions | `behave-by-esoi:insitu_standing_instructions` | [evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py](../../evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py) |  |
| In-situ prompt without the instruction layer | `behave-by-esoi:insitu_plain` | [evaluation/components/esoi_behavior/impls/insitu_plain.py](../../evaluation/components/esoi_behavior/impls/insitu_plain.py) |  |
| Chat-completion ablation with instructions | `behave-by-esoi:standing_instructions` | [evaluation/components/esoi_behavior/impls/standing_instructions.py](../../evaluation/components/esoi_behavior/impls/standing_instructions.py) |  |
| Chat-completion baseline without instructions | `behave-by-esoi:plain` | [evaluation/components/esoi_behavior/impls/plain.py](../../evaluation/components/esoi_behavior/impls/plain.py) |  |
| Rubric-based model judge | `judge-esoi-behavior:model_judge` | [evaluation/components/esoi_judge/impls/model_judge.py](../../evaluation/components/esoi_judge/impls/model_judge.py) |  |
| Regex-check floor | `judge-esoi-behavior:regex_only` | [evaluation/components/esoi_judge/impls/regex_only.py](../../evaluation/components/esoi_judge/impls/regex_only.py) |  |
| Offline suite configuration | `verify-offline:conftest` | [tests/conftest.py](../../tests/conftest.py) |  |
| Collection check by test file | `verify-offline:collect_by_file` | [evaluation/components/verify_offline/impls/collect_by_file.py](../../evaluation/components/verify_offline/impls/collect_by_file.py) |  |
| Planted-secret evaluation arm | `keep-secrets-out:live_scanner` | [evaluation/components/keep_secrets_out/impls/live_scanner.py](../../evaluation/components/keep_secrets_out/impls/live_scanner.py) |  |
| Problem-linked tag list with a check rule and a lookup | `tag-solutions:tag_list_with_check` | [scripts/esoi_tags.py](../../scripts/esoi_tags.py) |  |

### Metrics

- Principle-following pass rate under standing instructions (`metric-behave-by-esoi`): `esoi run esoi_behavior --impl insitu_standing_instructions`
- Judge agreement with labelled reference replies (`metric-judge-esoi-behavior`): `esoi run esoi_judge --impl model_judge`
- Offline test collection (`metric-verify-offline`): `esoi run verify_offline --impl collect_by_file`
- Secret detection and placeholder tolerance (`metric-keep-secrets-out`): `esoi run keep_secrets_out --impl live_scanner`
- Right solution missed without the tag (`metric-tag-solutions`): `esoi run tag_solutions --impl tag_list_with_check`
