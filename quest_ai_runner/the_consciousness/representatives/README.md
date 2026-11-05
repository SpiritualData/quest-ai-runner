# representatives

Breadcrumb: [Top](../../../esoi/README.md) > [Quest Consciousness](../README.md) > representatives

Part of [Quest Consciousness](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| Configured persona resolver | `quest-requests:personas` | Chooses who runs a task from configuration, in order: a structured assignment field matching the registry, an optional model-judged explicit ask, then domain dominance, with optional auto-registration of unknown ids. | [`quest_ai_runner/the_consciousness/representatives/personas.py`](personas.py) |  |
| Persona resolver precedence for queued tasks (today) | `quest-representatives:persona_resolver_precedence` | A structured assignment field first, then a model-judged explicit ask, then domain-card dominance with names excluded, then a structural fallback; a bare mention never selects. | [`quest_ai_runner/the_consciousness/representatives/personas.py`](personas.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../README.md)
- Intent: [Quest Consciousness](../README.md)
- Repository: [top README](../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
