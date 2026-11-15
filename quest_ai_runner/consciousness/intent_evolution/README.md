# intent_evolution

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > intent_evolution

Part of [Quest Consciousness](../README.md)

### Top implementations of Intent Evolution

No implementation has a recorded result yet, so the choice is not backed by a measurement.

**How it is scored:** Intent evolution: the most likely option or a checkable experiment. One balanced number (accuracy, cost and time, weights fixed in esoi.toml before any arm is scored) over Quest quests with experiments and outcomes: the reply proposes the option most likely to work with a metric that checks it, or an experiment when the best option is unknown, without nagging.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| History floor | `intent-evolution:history_floor` | Reads only the quest it is handed and asks no model: no intents or an experiment already running means no suggestion; otherwise a past history entry that worked is offered again with the metric it was checked by; otherwise one small experiment on the first intent, checked by the quest's first metric. | [`quest_ai_runner/consciousness/intent_evolution/history_floor.py`](history_floor.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
