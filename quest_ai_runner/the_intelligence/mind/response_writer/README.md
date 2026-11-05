# response_writer

Breadcrumb: [Top](../../../../esoi/README.md) > [Intelligence](../../README.md) > [Mind](../README.md) > response_writer

Part of [Mind](../README.md)

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| One cheap model call per turn writes the beat (today) | `quest-work-shown:model_beat` | The narrator asks the turn's model for one short line from the events so far; a refusal or error is dropped and never fails the turn. | [`quest_ai_runner/the_intelligence/mind/response_writer/orchestrator_turn.py`](orchestrator_turn.py) |  |
| The Trail (live line, one summary line, expandable groups) | `quest-work-shown:trail` | A live line (the newest active kind) while working, one summary line after (records first, then sites, then papers, at most three clauses), and groups behind a tap with five rows each; the same reduced form for every viewer of a shared thread. | [`quest_ai_runner/the_intelligence/mind/response_writer/work_trace.py`](work_trace.py) | selected |
| The structural gate of core/interim_policy.py | `reply-in-steps:gated` | Sends only when the verdict is more work, the loop runs again, no ask is open, there is a reason and something to show, something new since the last message, and the cap and spacing allow. The same arm files the one ask a person-only block needs (core/ask_policy.py): it asks when the verdict needs a person with a question, or a tool fails on an access refusal, resolves the teammate or falls back to the asker, and keeps one key per turn and gap. | [`quest_ai_runner/the_intelligence/mind/response_writer/interim_policy.py`](interim_policy.py) | selected |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Mind](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
