# Integrity

Every part of QAR is a specified solution whose best implementation runs as the state of the art moves.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `system-integrity:structure-library` of its parent intent [`the_self`](../README.md). Opportunities: A repository that specifies every part as a solution with alternatives and a metric lets the best implementation run as models and tools change. That is a space beyond the engine the top intent describes: the structure check and the selector belong to the esoi library, but QAR measures whether its assistant behaves by the structure, scores how much of its wiring is configuration and not code, keeps its tests runnable offline and its public code free of secrets, and loads one configuration per lane.

## How the solutions fit together

Integrity keeps every part of the runner a specified solution whose best implementation runs, so the package, its front door, its configuration and its offline checks stay one measured whole.

## Metrics and their leaders

- Principle-following pass rate under standing instructions (`metric-behave-by-esoi`): leader `standing_instructions`, 30 of 60 cases
- Judge agreement with labelled reference replies (`metric-judge-esoi-behavior`): leader `model_judge`, 29 of 30 cases
- Offline test collection (`metric-verify-offline`): leader `collect_by_file`, 29 of 29 cases
- Secret detection and placeholder tolerance (`metric-keep-secrets-out`): leader `live_scanner`, 30 of 35 cases
- Right solution missed without the tag (`metric-tag-solutions`): leader `tag_list_with_check`, 13 of 13 cases

## Where to go next

- Parent intent: [Quest Self](../README.md)
- Repository: [top README](../../../README.md)
- Records: [intent.csv](intent.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Shipped deep-run prompt with standing instructions | `esoi-application:insitu_standing_instructions` | [evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py](../../../evaluation/components/esoi_behavior/impls/insitu_standing_instructions.py) |  |
| In-situ prompt without the instruction layer | `esoi-application:insitu_plain` | [evaluation/components/esoi_behavior/impls/insitu_plain.py](../../../evaluation/components/esoi_behavior/impls/insitu_plain.py) |  |
| Chat-completion ablation with instructions | `esoi-application:standing_instructions` | [evaluation/components/esoi_behavior/impls/standing_instructions.py](../../../evaluation/components/esoi_behavior/impls/standing_instructions.py) |  |
| Chat-completion baseline without instructions | `esoi-application:plain` | [evaluation/components/esoi_behavior/impls/plain.py](../../../evaluation/components/esoi_behavior/impls/plain.py) |  |
| Rubric-based model judge | `response-quality:model_judge` | [evaluation/components/esoi_judge/impls/model_judge.py](../../../evaluation/components/esoi_judge/impls/model_judge.py) |  |
| Regex-check floor | `response-quality:regex_only` | [evaluation/components/esoi_judge/impls/regex_only.py](../../../evaluation/components/esoi_judge/impls/regex_only.py) |  |
| Offline suite configuration | `system-integrity:conftest` | [tests/conftest.py](../../../tests/conftest.py) |  |
| Collection check by test file | `system-integrity:collect_by_file` | [evaluation/components/verify_offline/impls/collect_by_file.py](../../../evaluation/components/verify_offline/impls/collect_by_file.py) |  |
| Planted-secret evaluation arm | `system-integrity:live_scanner` | [evaluation/components/keep_secrets_out/impls/live_scanner.py](../../../evaluation/components/keep_secrets_out/impls/live_scanner.py) |  |
| Problem-linked tag list with a check rule and a lookup | `system-integrity:tag_list_with_check` | [scripts/esoi_tags.py](../../../scripts/esoi_tags.py) |  |

### Metrics

- Principle-following pass rate under standing instructions (`metric-behave-by-esoi`): `esoi run esoi_behavior --impl insitu_standing_instructions`
- Judge agreement with labelled reference replies (`metric-judge-esoi-behavior`): `esoi run esoi_judge --impl model_judge`
- Offline test collection (`metric-verify-offline`): `esoi run verify_offline --impl collect_by_file`
- Secret detection and placeholder tolerance (`metric-keep-secrets-out`): `esoi run keep_secrets_out --impl live_scanner`
- Right solution missed without the tag (`metric-tag-solutions`): `esoi run tag_solutions --impl tag_list_with_check`
