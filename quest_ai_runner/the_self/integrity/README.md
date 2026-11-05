# Integrity

**Vision statement:** Every part of the runner is a specified solution whose best implementation runs as the state of the art moves.

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Self](../README.md) > Integrity

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
- Records: [intent.csv](intent.csv), [metrics.csv](metrics.csv)

---

## The full record

### Metrics

- Principle-following pass rate under standing instructions (`metric-behave-by-esoi`): `esoi run esoi_behavior --impl insitu_standing_instructions`
- Judge agreement with labelled reference replies (`metric-judge-esoi-behavior`): `esoi run esoi_judge --impl model_judge`
- Offline test collection (`metric-verify-offline`): `esoi run verify_offline --impl collect_by_file`
- Secret detection and placeholder tolerance (`metric-keep-secrets-out`): `esoi run keep_secrets_out --impl live_scanner`
- Right solution missed without the tag (`metric-tag-solutions`): `esoi run tag_solutions --impl tag_list_with_check`


*Generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*
