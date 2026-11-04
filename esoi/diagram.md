These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Grounded AI that advances people's goals

quest-ai-runner turns a person's request or queued task into grounded work in one loop, and the Grounded goal agent is that loop. It asks the model through the Tiered model provider layer, works in the person's Quest account through Quest account access and takes in requests through Request intake. It reasons step by step through Mind, recalls what a request needs through Subconscious and acts on the account through Body. It holds its work to the metric that serves it through Critical thinking, has every run watched through Overseer and keeps the long view across turns through Higher mind. It stays comparable by metric through Evolve by metric and answers questions beyond the person's own material through Science. Subconscious recalls the cards that match a request through Hub card store. Tiered model provider layer accounts for every call through Cost estimation, daily budget and host load guard. Mind sends the prompt of each step to the model through Tiered model provider layer. Overseer has the model oversee a run through Tiered model provider layer. Queued-task runner claims, runs and reports a queued task, asking the model through Tiered model provider layer and working in the account through Quest account access. It writes each prompt as a registered spec with an owner and a metric through Justified model calls. Body reads and changes the quest through Context card persistence backend, Quest account access and Queued-task runner. Higher mind saves what it learned through Context card persistence backend. Request intake hears from a live channel through Live messaging channel transport.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Tiered model provider layer<br/><i>call-a-model</i>"]
    s1["Live messaging channel transport<br/><i>talk-over-a-channel</i>"]
    s2["Overseer<br/><i>watch-for-risk</i>"]
    s3["Cost estimation, daily budget and host...<br/><i>track-usage-and-cost</i>"]
    s4["Evolve by metric<br/><i>keep-it-generic</i>"]
    s5["Science<br/><i>know-the-world</i>"]
    s6["Grounded goal agent<br/><i>do-grounded-work</i>"]
    s7["Queued-task runner<br/><i>run-queued-tasks</i>"]
    s8["Mind<br/><i>reason-and-answer</i>"]
    s9["Body<br/><i>act-as-the-body</i>"]
    s10["Higher mind<br/><i>hold-the-intents</i>"]
    s11["Critical thinking<br/><i>think-critically</i>"]
    s12["Quest account access<br/><i>access-quest-operations</i>"]
    s13["Request intake<br/><i>take-in-the-world</i>"]
    s14["Subconscious<br/><i>remember-and-recall</i>"]
    s15["Justified model calls<br/><i>justify-model-calls</i>"]
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
