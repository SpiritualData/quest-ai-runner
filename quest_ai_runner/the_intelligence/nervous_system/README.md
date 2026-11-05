# Nervous System

The AI system watches every run and brings in a person only where only a person can decide.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `nervous-system:overseer` of its parent intent [`quest-ai-runner`](../../../esoi/README.md). Opportunities: A run can be watched at a fraction of the cost of the work it watches: a small model reads a digest of the run and sends one signal (proceed, redirect, answer now, escalate to deep work, escalate to a person). That opens ground the parent solution does not cover: which risks deserve a response of their own instead of one uniform treatment, which questions about a run a plain check can answer without a model, and how a step only a person may take is put before the right person with the work held until they answer.

## How the solutions fit together

Nervous System asks the model through Tiered model provider layer to oversee a run and brings a step only a person may take to that person through Human decision escalation channel. Human decision escalation channel files each decision in the Quest account through Quest account access.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Quest Decision Requests | `quest-decision-requests` | no entry declared | Decision-request sink (`quest_decision`) | Escalation delivery and detail completeness |

## Solutions from a library

- [Judgment Calls](https://github.com/SpiritualData/ai-hub-of-hubs-structure/hubofhubs/the_consciousness/README.md) (`quest-decision-requests`): Decides, for one person, whether to ask, proceed or decline when the Nervous System raises a risk signal. Specializes the library `hub`.

## Problems and ideals

**Problems**

- Decisions Not Reaching The Person (`QP3`): A step only a person on the quest may decide can be taken without them, reach the wrong teammate, or be dropped before they answer.

**Ideals**

- Trusted Autonomy (`QI3`): The AI acts first on what it may decide and brings the person exactly the decisions that are theirs.

## Metrics and their leaders

- Escalation delivery and detail completeness (`metric-escalate-to-a-human`): leader `quest_decision_sink`, 15 of 15 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../../esoi/README.md)
- Repository: [top README](../../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- Decisions Not Reaching The Person (`QP3`): A step only a person on the quest may decide can be taken without them, reach the wrong teammate, or be dropped before they answer.

### Ideals

- Trusted Autonomy (`QI3`): The AI acts first on what it may decide and brings the person exactly the decisions that are theirs.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Quest Decision Requests | `quest-decision-requests` | Decision-request sink (`quest_decision`) | Escalation delivery and detail completeness |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Decision-request sink | `quest-decision-requests:quest_decision` | [quest_ai_runner/the_body/internal_context/quest/client/quest_client.py](../../the_body/internal_context/quest/client/quest_client.py) |  |
| Fake-backend delivery check | `quest-decision-requests:quest_decision_sink` | [evaluation/components/escalate_to_a_human/impls/quest_decision_sink.py](../../../evaluation/components/escalate_to_a_human/impls/quest_decision_sink.py) |  |

### Metrics

- Escalation delivery and detail completeness (`metric-escalate-to-a-human`): `esoi run escalate_to_a_human --impl quest_decision_sink`
