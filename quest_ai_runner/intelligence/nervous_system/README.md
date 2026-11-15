# nervous_system

Breadcrumb: [Top](../../../esoi/README.md) > [Intelligence](../README.md) > nervous_system

Part of [Intelligence](../README.md)

### Top implementations of Nervous System

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | No-judgement digest probe (`digest_probe`) | 100% (16 of 16 cases) | free |  |
| 2 | Live overseer model arm (`live_model`) | 88% (14 of 16 cases) | $0.0010 |  |
| 3 | No overseer (configuration off) (`no_overseer`) | 38% (6 of 16 cases) | free |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Overseer digest evidence and signal correctness on labelled scenarios. digest evidence on hand-labelled scenarios (contrast pairs), offline; signal match in a live arm; signal parsing and degrade-to-proceed in judges The signal match itself needs a model and is not seen offline.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Digest-driven overseer | `nervous-system:overseer` | Builds a capped compact digest of the run, makes one structured model call with a fixed prompt and tool schema, validates the answer against the five signals, and returns proceed on any error, non-object response or unknown signal. | [`quest_ai_runner/intelligence/nervous_system/area.py`](area.py) | selected |

## Metrics

- Escalation delivery and detail completeness (`metric-escalate-to-a-human`): leader `quest_decision_sink`, 15 of 15 cases

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Intelligence](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
