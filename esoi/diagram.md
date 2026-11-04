These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Grounded AI that advances people's goals

quest-ai-runner turns a person's request or queued task into grounded work in one loop, and do-grounded-work is that loop. It asks the model through call-a-model, works in the person's Quest account through access-quest-operations and takes in requests through take-in-the-world. It reasons step by step through reason-and-answer, recalls what a request needs through remember-and-recall and acts on the account through act-as-the-body. It holds its work to the metric that serves it through think-critically, has every run watched through watch-for-risk and keeps the long view across turns through hold-the-intents. It stays comparable by metric through keep-it-generic and answers questions about the world through know-the-world. remember-and-recall recalls the cards that match a request through card-store. call-a-model accounts for every call through justify-model-calls and track-usage-and-cost. reason-and-answer sends the prompt of each step to the model through call-a-model. watch-for-risk has the model oversee a run through call-a-model. run-queued-tasks claims, runs and reports a queued task, asking the model through call-a-model and working in the account through access-quest-operations. act-as-the-body reads and changes the quest through persist-cards, access-quest-operations and run-queued-tasks. hold-the-intents saves what it learned through persist-cards. take-in-the-world hears from a live channel through talk-over-a-channel.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Grounded goal agent: the engine that...<br/>do-grounded-work"]
    s1["Tiered model provider layer<br/>call-a-model"]
    s2["Live messaging channel transport<br/>talk-over-a-channel"]
    s3["Overseer: risk management that raises...<br/>watch-for-risk"]
    s4["Queued-task runner lane<br/>run-queued-tasks"]
    s5["Cost estimation, daily budget and host...<br/>track-usage-and-cost"]
    s6["Evolve by metric<br/>keep-it-generic"]
    s7["Mind: one goal-focused working stream...<br/>reason-and-answer"]
    s8["Subconscious: the person's own...<br/>remember-and-recall"]
    s9["Science: evidence-weighted knowledge...<br/>know-the-world"]
    s10["Body: the host's data model as the...<br/>act-as-the-body"]
    s11["Higher mind: an AI representative's...<br/>hold-the-intents"]
    s12["Critical thinking: the enforcer of ESOI...<br/>think-critically"]
    s13["Justified model calls<br/>justify-model-calls"]
    s14["access-quest-operations<br/>access-quest-operations"]
    s15["take-in-the-world: the ways a request...<br/>take-in-the-world"]
    s1 -->|"A raw decision dictionary for planning, answer text for..."| s0
    s1 -->|"A raw decision dictionary for planning, answer text for..."| s3
    s1 -->|"A raw decision dictionary for planning, answer text for..."| s4
    s1 -->|"A raw decision dictionary for planning, answer text for..."| s7
    s1 -->|"A raw decision dictionary for planning, answer text for..."| s11
    s2 -->|"An inbound batch (messages with ids, chat references, text..."| s15
    s3 -->|"For a flagged case: the risk category, the response applied..."| s0
    s4 -->|"A reported status on each task (with result text or..."| s10
    s4 -->|"A reported status on each task (with result text or..."| s12
    s5 -->|"A price per million input tokens, a bootstrap cost estimate..."| s1
    s6 -->|"Checked records, recorded metric results and the selected..."| s0
    s7 -->|"Each step's view of the request (a carried message list or..."| s0
    s7 -->|"Each step's view of the request (a carried message list or..."| s10
    s8 -->|"The admitted items with a one-line reason each, and the..."| s0
    s8 -->|"The admitted items with a one-line reason each, and the..."| s7
    s9 -->|"Findings, each with its source, an evidence-strength label..."| s0
    s10 -->|"A current state view for the request's scope with the time..."| s0
    s11 -->|"For a request the representative handles: its standing view..."| s0
    s11 -->|"For a request the representative handles: its standing view..."| s8
    s12 -->|"Per request: the metric applied with its reading before and..."| s0
    s13 -->|"A per-prompt table and the share of average model spend..."| s1
    s14 -->|"The result of one Quest operation"| s0
    s14 -->|"The result of one Quest operation"| s4
    s14 -->|"The result of one Quest operation"| s10
    s15 -->|"One routed turn with its context sources"| s0
```
