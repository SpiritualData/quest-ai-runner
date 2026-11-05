# Quest Consciousness

Each Quest user grows with an AI aligned to them.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `quest-consciousness:composition` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: Aligning an AI to a person is half of helping them; the other half is helping the person's own intents move, which no part of the hub's Consciousness does for them.

## How the solutions fit together

Quest Consciousness is where the assistant adapts to the person it works for and helps them move toward their intents. Quest Requests takes in what the person asks, from every Quest surface, once. Quest Representatives gives each request to the representative that fits it. Quest Decision Requests carries a decision that only a person can make to the person it belongs to. Feedback Learning keeps what the person corrected so it guides later work. Intent Evolution then suggests the option most likely to work for the person's intents, or an experiment when the best option is not yet known.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Quest Consciousness | `quest-consciousness` | no entry declared | none selected | Quest Consciousness on Quest cases |
| Intent Evolution | `intent-evolution` | `quest_ai_runner.run("intent-evolution", ...)` | none selected | Intent evolution: the most likely option or a checkable experiment |

## Solutions from a library

- [Consciousness](https://github.com/SpiritualData/ai-hub-of-hubs-structure/esoi/README.md) (`quest-consciousness`): The humans who interact with the system: AI Representatives act for a person, Prioritization turns the person's priorities into ESOI, Judgment Calls decide when to ask that person, and Feedback Learning applies the person's feedback to specs and metrics. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Advice Without A Way To Check It (`prob-experiments-not-proposed`): Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose an experiment that would settle it, though the app has an Experiments feature built for that.

**Ideals**

- Most-Likely Path (`ideal-most-likely-path`): Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.

## Metrics and their leaders

- Intent evolution: the most likely option or a checkable experiment (`metric-intent-evolution`)
- Quest Consciousness on Quest cases (`metric-quest-consciousness`)

## Where to go next

- Sub-intent: [Quest Requests](request_intake/README.md) (Every request made in Quest is taken in once by the same AI.)
- Parent intent: [Quest AI Runner](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records beside the code: [quest_ai_runner/the_consciousness/representatives](representatives/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Advice Without A Way To Check It (`prob-experiments-not-proposed`): Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose an experiment that would settle it, though the app has an Experiments feature built for that.

### Ideals

- Most-Likely Path (`ideal-most-likely-path`): Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Quest Consciousness | `quest-consciousness` | none selected | Quest Consciousness on Quest cases |
| Intent Evolution | `intent-evolution` | none selected | Intent evolution: the most likely option or a checkable experiment |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Task poller and claimer | `quest-requests:poller` | [quest_ai_runner/the_consciousness/request_intake/poller.py](request_intake/poller.py) |  |
| Single-task executor | `quest-requests:executor` | [quest_ai_runner/the_consciousness/request_intake/executor.py](request_intake/executor.py) |  |
| Shared lane entry point | `quest-requests:lane` | [quest_ai_runner/the_consciousness/request_intake/lane.py](request_intake/lane.py) |  |
| Configured persona resolver | `quest-requests:personas` | [quest_ai_runner/the_consciousness/representatives/personas.py](representatives/personas.py) |  |

### Metrics

- Intent evolution: the most likely option or a checkable experiment (`metric-intent-evolution`): `none`
- Quest Consciousness on Quest cases (`metric-quest-consciousness`): `none`
