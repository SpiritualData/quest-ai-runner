# Nervous System

Every run is watched through cheap checks, and a step only a person may take reaches that person with enough detail to decide.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `nervous-system:overseer` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: A run can be watched at a fraction of the cost of the work it watches: a small model reads a digest of the run and sends one signal (proceed, redirect, answer now, escalate to deep work, escalate to a person). That opens ground the parent solution does not cover: which risks deserve a response of their own instead of one uniform treatment, which questions about a run a plain check can answer without a model, and how a step only a person may take is put before the right person with the work held until they answer.

## How the solutions fit together

Nervous System asks the model through Tiered model provider layer to oversee a run and brings a step only a person may take to that person through Human decision escalation channel. Human decision escalation channel files each decision in the Quest account through Quest account access.

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Human decision escalation channel | `escalate-to-a-human` | `quest_ai_runner.run("escalate-to-a-human")` | Decision-request sink (`quest_decision`) | Escalation delivery and detail completeness |

## Problems and ideals

**Problems**

- The AI asks the person about implementation details they neither care about nor understand, and when told it should not have asked, does not learn which kinds of question to stop asking. (`prob-asks-what-is-not-theirs`)

## Metrics and their leaders

- Escalation delivery and detail completeness (`metric-escalate-to-a-human`): leader `quest_decision_sink`, 15 of 15 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)

---

## The full record

### Problems

- The AI asks the person about implementation details they neither care about nor understand, and when told it should not have asked, does not learn which kinds of question to stop asking. (`prob-asks-what-is-not-theirs`)

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Human decision escalation channel | `escalate-to-a-human` | Decision-request sink (`quest_decision`) | Escalation delivery and detail completeness |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Decision-request sink | `escalate-to-a-human:quest_decision` | [quest_ai_runner/runner/quest_client.py](../runner/quest_client.py) |  |
| Fake-backend delivery check | `escalate-to-a-human:quest_decision_sink` | [evaluation/components/escalate_to_a_human/impls/quest_decision_sink.py](../../evaluation/components/escalate_to_a_human/impls/quest_decision_sink.py) |  |

### Metrics

- Escalation delivery and detail completeness (`metric-escalate-to-a-human`): `esoi run escalate_to_a_human --impl quest_decision_sink`
