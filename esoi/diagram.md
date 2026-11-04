These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Grounded AI that advances people's goals

quest-ai-runner turns a person's request or queued task into grounded work in one loop, and do-grounded-work is that loop. It asks the model through call-a-model, works in the person's Quest account through access-quest-operations and takes in requests through take-in-the-world. It reasons step by step through reason-and-answer, recalls what a request needs through remember-and-recall and acts on the account through act-as-the-body. It holds its work to the metric that serves it through think-critically, has every run watched through watch-for-risk and keeps the long view across turns through hold-the-intents. It stays comparable by metric through keep-it-generic and answers questions about the world through know-the-world. remember-and-recall recalls the cards that match a request through card-store. call-a-model accounts for every call through track-usage-and-cost. reason-and-answer sends the prompt of each step to the model through call-a-model. watch-for-risk has the model oversee a run through call-a-model. run-queued-tasks claims, runs and reports a queued task, asking the model through call-a-model and working in the account through access-quest-operations. It writes each prompt as a registered spec with an owner and a metric through justify-model-calls. act-as-the-body reads and changes the quest through persist-cards, access-quest-operations and run-queued-tasks. hold-the-intents saves what it learned through persist-cards. take-in-the-world hears from a live channel through talk-over-a-channel.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Tiered model provider layer<br/>call-a-model"]
    s1["Live messaging channel transport<br/>talk-over-a-channel"]
    s2["Overseer: risk management that raises...<br/>watch-for-risk"]
    s3["Cost estimation, daily budget and host...<br/>track-usage-and-cost"]
    s4["Evolve by metric<br/>keep-it-generic"]
    s5["Science: evidence-weighted knowledge...<br/>know-the-world"]
    s6["Grounded goal agent: the engine that...<br/>do-grounded-work"]
    s7["Queued-task runner lane<br/>run-queued-tasks"]
    s8["Mind: one goal-focused working stream...<br/>reason-and-answer"]
    s9["Body: the host's data model as the...<br/>act-as-the-body"]
    s10["Higher mind: an AI representative's...<br/>hold-the-intents"]
    s11["Critical thinking: the enforcer of ESOI...<br/>think-critically"]
    s12["access-quest-operations<br/>access-quest-operations"]
    s13["take-in-the-world: the ways a request...<br/>take-in-the-world"]
    s14["Subconscious: the person's own...<br/>remember-and-recall"]
    s15["Justified model calls<br/>justify-model-calls"]
    s0 -->|"A raw decision dictionary for planning, answer text for..."| s2
    s0 -->|"A raw decision dictionary for planning, answer text for..."| s6
    s0 -->|"A raw decision dictionary for planning, answer text for..."| s7
    s0 -->|"A raw decision dictionary for planning, answer text for..."| s8
    s0 -->|"A raw decision dictionary for planning, answer text for..."| s10
    s1 -->|"An inbound batch (messages with ids, chat references, text..."| s13
    s2 -->|"For a flagged case: the risk category, the response applied..."| s6
    s3 -->|"A price per million input tokens, a bootstrap cost estimate..."| s0
    s4 -->|"Checked records, recorded metric results and the selected..."| s6
    s5 -->|"Findings, each with its source, an evidence-strength label..."| s6
    s7 -->|"A reported status on each task (with result text or..."| s9
    s7 -->|"A reported status on each task (with result text or..."| s11
    s8 -->|"Each step's view of the request (a carried message list or..."| s6
    s8 -->|"Each step's view of the request (a carried message list or..."| s9
    s9 -->|"A current state view for the request's scope with the time..."| s6
    s10 -->|"For a request the representative handles: its standing view..."| s6
    s10 -->|"For a request the representative handles: its standing view..."| s14
    s11 -->|"Per request: the metric applied with its reading before and..."| s6
    s12 -->|"The result of one Quest operation"| s6
    s12 -->|"The result of one Quest operation"| s7
    s12 -->|"The result of one Quest operation"| s9
    s13 -->|"One routed turn with its context sources"| s6
    s14 -->|"The admitted items with a one-line reason each, and the..."| s6
    s14 -->|"The admitted items with a one-line reason each, and the..."| s8
    s15 -->|"A per-prompt table and the share of average model spend..."| s7
```
