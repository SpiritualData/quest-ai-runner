# intent_evolution

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > intent_evolution

Part of [Quest Consciousness](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| History floor | `intent-evolution:history_floor` | Reads only the quest it is handed and asks no model: no intents or an experiment already running means no suggestion; otherwise a past history entry that worked is offered again with the metric it was checked by; otherwise one small experiment on the first intent, checked by the quest's first metric. | [`quest_ai_runner/the_consciousness/intent_evolution/history_floor.py`](history_floor.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
