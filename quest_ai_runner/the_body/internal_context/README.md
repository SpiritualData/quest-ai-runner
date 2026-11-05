# Body

What the AI says about a person's account is the account as it is now, and what it changed in the account is read back after the change, so the person can trust both.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `the-body:card_and_reads` of its parent intent [`quest-ai-runner`](../../../esoi/README.md). Opportunities: The Quest account is the AI's own consistent state, which opens questions beyond the parent solution: how the AI states what it can reach (inside the host, outside it, in the world now, or not reachable) and is held to that claim, how heavy work is handed to a deep runner within a fixed turn and cost bound and its result checked, how a file change is kept bounded to one folder and reversible, how one named change on an outside tool server is made only when an allowlist permits it, and who a new goal belongs to and whether an AI representative can take it over.

## How the solutions fit together

Body keeps what the AI says about a person's account true to the account and to what it changed in it. It changes the quest as the person asks through Quest change executor and writes files inside one folder through Bounded reversible file writer. It reads from the sources the person's work lives in through Multi-source evidence retriever, and hands a new goal to the right owner through Goal assignee and doability judge. It claims only the reach that World grants. Heavy work is the job of Bounded autonomous work runner. One named change on an outside tool server is the job of Allowlisted operation writer.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Body<br/><i>the-body</i>"]
    s1["Quest account access<br/><i>access-quest-operations</i>"]
    s1 -->|"The result of one Quest operation"| s0
```

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Body | `the-body` | `quest_ai_runner.run("the-body", '')` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Quest account access | `access-quest-operations` | `quest_ai_runner.run("access-quest-operations", 'quest-id')` | Quest HTTP client from the environment (`quest_client`) | Parity of Quest operations across access points |

## Problems and ideals

**Problems**

- What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it reports the same state two ways, repeats a change it already made, or proposes again what it said was done. (`prob-account-state-contradicts`)
- Quest's operations can be reached in the same process as Quest and over its HTTP API, and two access points to the same operations can drift apart, so the same request may behave or return differently depending on how it arrived. (`prob-two-access-points-to-quest-diverge`)

**Ideals**

- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Shared Quest Operations (`ideal-one-implementation-two-access-points`): Quest's operations are implemented once, and each way of reaching them (in the same process as Quest, or over its HTTP API) gives the same results.

## Metrics and their leaders

- Account-state consistency across scripted sessions (`metric-act-as-the-body`): leader `card_and_reads`, 8 of 9 cases
- Parity of Quest operations across access points (`metric-quest-operations-parity`)

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../../esoi/README.md)
- Repository: [top README](../../../README.md)
- Records beside the code: [quest_ai_runner/the_body/internal_context/quest](quest/README.md)
- Records: [intent.csv](intent.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it reports the same state two ways, repeats a change it already made, or proposes again what it said was done. (`prob-account-state-contradicts`)
- Quest's operations can be reached in the same process as Quest and over its HTTP API, and two access points to the same operations can drift apart, so the same request may behave or return differently depending on how it arrived. (`prob-two-access-points-to-quest-diverge`)

### Ideals

- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Shared Quest Operations (`ideal-one-implementation-two-access-points`): Quest's operations are implemented once, and each way of reaching them (in the same process as Quest, or over its HTTP API) gives the same results.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Body | `the-body` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Quest account access | `access-quest-operations` | Quest HTTP client from the environment (`quest_client`) | Parity of Quest operations across access points |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Offline lifecycle harness | `run-queued-tasks:qar_runner` | [quest_ai_runner/the_body/area_body.py](../area_body.py) |  |
| Managed quest card plus optional live reads (today) | `the-body:card_and_reads` | [quest_ai_runner/the_body/area_body.py](../area_body.py) | [internal_context](internal_context/README.md) |
| Quest HTTP client from the environment | `access-quest-operations:quest_client` | [quest_ai_runner/the_body/area_body.py](../area_body.py) |  |
| Planner-only orchestration | `operate-a-quest:qar_orchestrator` | [quest_ai_runner/the_body/area_body.py](../area_body.py) |  |
| Cached live-database reader | `gather-evidence:cached_db` | [quest_ai_runner/the_body/internal_context/quest/cached_db_adapter.py](quest/cached_db_adapter.py) |  |
| Quest API context reader | `gather-evidence:quest` | [quest_ai_runner/the_body/internal_context/quest/quest_retrieval_adapter.py](quest/quest_retrieval_adapter.py) |  |
| Coding agent, reach described in the prompt (today) | `claim-reach:coding_agent_prompt_reach` | [quest_ai_runner/the_body/area_body.py](../area_body.py) |  |
| Two small model verdicts when a goal is created (today) | `hand-off-a-goal:model_judge` | [quest_ai_runner/the_body/area_body.py](../area_body.py) |  |

### Metrics

- Account-state consistency across scripted sessions (`metric-act-as-the-body`): `esoi run act_as_the_body --impl card_and_reads`
- Parity of Quest operations across access points (`metric-quest-operations-parity`): `none`
