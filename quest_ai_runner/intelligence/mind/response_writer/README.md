# response_writer

Breadcrumb: [Top](../../../../esoi/README.md) > [Intelligence](../../README.md) > [Mind](../README.md) > response_writer

Part of [Mind](../README.md)

### Top implementations of Quest Work Shown

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | One model call writes the explanation from the trace (today) (`model_explanation`) | score 0.79 (9 of 10 cases) | $0.0095 |  |
| 2 | Template over the trace, no model call (floor, proposed) (`trace_template`) | score 0.72 (7 of 10 cases) | $0.0080 |  |

The one in use is chosen at the deciding stage of the cascade (see the leaderboard), which can differ from the latest run ranked here.

**How it is scored:** Faithful explanations per unit of cost and time. offline cases of a turn's trace with the explanation a model wrote: maximise the share of explanations whose every step is in the trace and which omit no step that changed the answer (value), at a cost per explanation below a stated share of the turn's cost and with no time added before the reply.
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

### Top implementations of Reply In Steps

| # | Implementation | Result | Cost per pass | In use |
|---|---|---|---|---|
| 1 | The structural gate of core/interim_policy.py (`gated`) | 100% (24 of 24 cases) | free | selected |
| 2 | Behaviour before the build (no interim for an unmet attempt) (`today`) | 58% (14 of 24 cases) | free |  |
| 3 | An interim on every unmet attempt (spam baseline) (`every_attempt`) | 8% (2 of 24 cases) | free |  |

**How it is scored:** Interim messages and asks that are true, not repeated and only where a person is needed. from a turn's record (events, ledger, decision rows), never from reply wording: every interim is followed by another attempt and a later message (more-coming truth), every turn ends with exactly one final or one ask (no dangling end), an ask exists exactly when the case is blocked on a person (precision and recall), no two consecutive messages are alike and count and spacing stay within bounds (delta and bounds); met turns must not get worse (no harm).
Result is the share of the metric's cases an implementation passes (its balanced score when it has one); cost per pass is dollars per passing case.

## Implementations

| Implementation | Handle | Purpose | Code | Selected |
|---|---|---|---|---|
| One cheap model call per turn writes the beat (today) | `quest-work-shown:model_beat` | The narrator asks the turn's model for one short line from the events so far; a refusal or error is dropped and never fails the turn. | [`quest_ai_runner/intelligence/mind/response_writer/orchestrator_turn.py`](orchestrator_turn.py) |  |
| The Trail (live line, one summary line, expandable groups) | `quest-work-shown:trail` | A live line (the newest active kind) while working, one summary line after (records first, then sites, then papers, at most three clauses), and groups behind a tap with five rows each; the same reduced form for every viewer of a shared thread. | [`quest_ai_runner/intelligence/mind/response_writer/work_trace.py`](work_trace.py) | selected |
| The structural gate of core/interim_policy.py | `reply-in-steps:gated` | Sends only when the verdict is more work, the loop runs again, no ask is open, there is a reason and something to show, something new since the last message, and the cap and spacing allow. The same arm files the one ask a person-only block needs (core/ask_policy.py): it asks when the verdict needs a person with a question, or a tool fails on an access refusal, resolves the teammate or falls back to the asker, and keeps one key per turn and gap. It also sends the status-first message of an act request (act_status.py): when the planner's user_intent is act and the plan changes something or escalates to deep work, one interim message says where things stand (the milestone's measure and its current value, or not recorded yet) and what happens now; the closing reply ends with the metric before and after, read again by code, and with anything already in place. | [`quest_ai_runner/intelligence/mind/response_writer/interim_policy.py`](interim_policy.py) | selected |
| First message in plain words, read from goal and milestone records only | `reply-in-steps:plain-status-from-records` | act_status.py: readings_in takes only a goal or milestone record line (never a Done when inside a task brief or a recalled message), and status_texts says the milestone, when it is done and where it stands now in plain words; the stored labels are never shown. | [`quest_ai_runner/intelligence/mind/response_writer/act_status.py`](act_status.py) |  |

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Mind](../README.md)
- Repository: [top README](../../../../README.md)

*Generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*
