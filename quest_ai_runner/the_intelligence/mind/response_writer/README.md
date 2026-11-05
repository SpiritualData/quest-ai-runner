# response_writer

*ESOI records kept beside the code in `quest_ai_runner/the_intelligence/mind/response_writer`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| One cheap model call per turn writes the beat (today) | `quest-work-shown:model_beat` | [`quest_ai_runner/the_intelligence/mind/response_writer/orchestrator_turn.py`](orchestrator_turn.py) |  |
| The Trail (live line, one summary line, expandable groups) | `quest-work-shown:trail` | [`quest_ai_runner/the_intelligence/mind/response_writer/work_trace.py`](work_trace.py) | selected |
| The structural gate of core/interim_policy.py | `quest-work-shown:gated` | [`quest_ai_runner/the_intelligence/mind/response_writer/interim_policy.py`](interim_policy.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Mind](../README.md)
- Repository: [top README](../../../../README.md)
