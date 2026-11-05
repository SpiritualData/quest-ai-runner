# nervous_system

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > nervous_system

Part of [Intelligence](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Digest-driven overseer | `nervous-system:overseer` | Builds a capped compact digest of the run, makes one structured model call with a fixed prompt and tool schema, validates the answer against the five signals, and returns proceed on any error, non-object response or unknown signal. | [`quest_ai_runner/the_intelligence/nervous_system/area.py`](area.py) | selected |

## Metrics

- Escalation delivery and detail completeness (`metric-escalate-to-a-human`): leader `quest_decision_sink`, 15 of 15 cases

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
