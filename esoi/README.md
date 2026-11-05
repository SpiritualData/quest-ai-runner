# Grounded AI that advances people's goals

Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote, and every answer and action rests on the person's own goals, material and preferences, and never on another person's.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

Opportunities: Other products that need an AI which works from a person's own goals and material could reuse this engine, so the grounding, the standard for delegated work and the scope fences are built once and shared.

## How the solutions fit together

quest-ai-runner turns a person's request or queued task into grounded work in one loop, and the Grounded goal agent is that loop. It asks the model through the Tiered model provider layer, works in the person's Quest account through Quest account access and takes in requests through Request intake. It reasons step by step through Mind, recalls what a request needs through Subconscious and acts on the account through Body. It holds its work to the metric that serves it through Critical Thinking, has every run watched through Nervous System and keeps the long view across turns through Higher mind. It stays comparable by metric through Integrity, answers questions beyond the person's own material through Science and chooses what a planner reads through Context choice. Subconscious recalls the cards that match a request through Hub card store. Tiered model provider layer accounts for every call through Cost estimation, daily budget and host load guard. Mind starts each step on the model tier it picks through Cost-ordered model tier selector with outage and allowance recovery. Nervous System has the model oversee a run through Tiered model provider layer. Queued-task runner claims, runs and reports a queued task, asking the model through Tiered model provider layer and working in the account through Quest account access. It writes each prompt as a registered spec with an owner and a metric through Justified model calls. Body reads and changes the quest through Context card persistence backend, Quest account access and Queued-task runner. Higher mind saves what it learned through Context card persistence backend. Request intake hears from a live channel through Live messaging channel transport.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Tiered model provider layer<br/><i>vendor-model-caller</i>"]
    s1["Live messaging channel transport<br/><i>talk-over-a-channel</i>"]
    s2["Nervous System<br/><i>nervous-system</i>"]
    s3["Cost estimation, daily budget and host load guard<br/><i>track-usage-and-cost</i>"]
    s4["Integrity<br/><i>system-integrity</i>"]
    s5["Science<br/><i>the-science</i>"]
    s6["Grounded goal agent<br/><i>do-grounded-work</i>"]
    s7["Queued-task runner<br/><i>run-queued-tasks</i>"]
    s8["Mind<br/><i>the-mind</i>"]
    s9["Body<br/><i>the-body</i>"]
    s10["Higher mind<br/><i>hold-the-intents</i>"]
    s11["Critical Thinking<br/><i>critical-thinking</i>"]
    s12["Quest account access<br/><i>access-quest-operations</i>"]
    s13["Request intake<br/><i>take-in-the-world</i>"]
    s14["Context choice<br/><i>choose-the-context</i>"]
    s15["Subconscious<br/><i>the-subconscious</i>"]
    s16["Justified model calls<br/><i>justify-model-calls</i>"]
    s17["Cost-ordered model tier selector with outage and allowance recovery<br/><i>model-tier-chooser</i>"]
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s2
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s6
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s7
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s8
    s0 -->|"A raw decision dictionary for planning, answer text for answering, and a live list of available model identifiers; errors are formatted for humans"| s10
    s1 -->|"An inbound batch (messages with ids, chat references, text and attachments, a healthy flag and any error) and a send result with success and message id"| s13
    s2 -->|"For a flagged case: the risk category, the response applied and its signal (proceed, redirect with a hint, answer now, escalate to deep execution, escalate to a person), with a reason and a degraded flag"| s6
    s3 -->|"A price per million input tokens, a bootstrap cost estimate with the provider and model used, a running total with an over-limit flag and status line, and a paused or not verdict"| s0
    s4 -->|"Checked records, recorded metric results and the selected implementation for each solution"| s6
    s5 -->|"Findings, each with its source, an evidence-strength label and the passage that supports it"| s6
    s7 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s9
    s7 -->|"A reported status on each task (with result text or decision id), updated folder sync files, and logs"| s11
    s8 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s6
    s8 -->|"Each step's view of the request (a carried message list or a rebuilt prompt), with the step's decision added to it"| s9
    s9 -->|"A current state view for the request's scope with the time each part was read, and after a change the read-back objects"| s6
    s10 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s6
    s10 -->|"For a request the representative handles: its standing view across its assigned quests and the views of their ancestor intents, with the source of each item, fenced to those quests and ancestors"| s15
    s11 -->|"Per request: the metric applied with its reading before and after, or the reason none applies, or a proposed metric; and for a regression or a missing metric, the response taken"| s6
    s12 -->|"The result of one Quest operation"| s6
    s12 -->|"The result of one Quest operation"| s7
    s12 -->|"The result of one Quest operation"| s9
    s13 -->|"One routed turn with its context sources"| s6
    s14 -->|"The assembled context, the ids of the cards it drew on, a resolved reference, a ranked list of cards and records, or the cards nearest a query by meaning"| s6
    s15 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s6
    s15 -->|"The admitted items with a one-line reason each, and the dropped ones with theirs, for the explanation of how an answer was reached"| s8
    s16 -->|"A per-prompt table and the share of average model spend that is justified"| s7
    s17 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s2
    s17 -->|"A starting model and ladder, a concrete model id for a tier or family, the next lower tier, a vision-capability answer, an outage verdict, and a usage-limit record with its reset and resume times"| s8
```

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Tiered model provider layer | `vendor-model-caller` | `quest_ai_runner.run("vendor-model-caller", ...)` | Prefix-routing multi-provider (`multi_provider`) | Provider routing, fallback, retry and cost correctness |
| Live messaging channel transport | `talk-over-a-channel` | `quest_ai_runner.run("talk-over-a-channel")` | MCP gateway bridge transport (`openclaw`) | Channel transport conformance to the gateway wire contract |
| Nervous System | `nervous-system` | `quest_ai_runner.run("nervous-system", 'delete the old backup folder')` | Digest-driven overseer (`overseer`) | Overseer digest evidence and signal correctness on labelled scenarios |
| Cost estimation, daily budget and host load guard | `track-usage-and-cost` | no entry declared | Live usage evaluation arm (`qar_usage`) | Cost, budget and resource-guard correctness |
| Integrity | `system-integrity` | `quest_ai_runner.run("system-integrity", ['examplevendor'])` | Apply the evolvable-structure library (`structure-library`) | Parts selected by measurement, Correct selection decisions over the evolve-by-metric cases, Intents whose solutions one paragraph and one diagram explain as the code builds them, Hub of hubs |
| Science | `the-science` | `quest_ai_runner.run("the-science", 'meditation and attention')` | Web research with the answer's support check (today) (`web_with_support_check`) | Supported and calibrated answers to world questions |
| Grounded goal agent | `do-grounded-work` | `quest_ai_runner.run("do-grounded-work", 'What should I work on first this week?')` | quest-ai-runner library (`library`) | QAR overall score, Passing chat turns per dollar, Delegated tasks meeting their standard, Exact and consented changes, Data kept in scope, Engine reuse, Measured coverage |
| Queued-task runner | `run-queued-tasks` | `quest_ai_runner.run("run-queued-tasks", 'team-1')` | Offline lifecycle harness (`qar_runner`) | Task lifecycle correctness over offline cases |
| Mind | `the-mind` | `quest_ai_runner.run("the-mind", ...)` | Rebuilt prompt per step with a referenced context (today) (`rebuilt_per_step`) | Step continuity at cost over multi-step requests |
| Body | `the-body` | `quest_ai_runner.run("the-body", '')` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Higher mind | `hold-the-intents` | `quest_ai_runner.run("hold-the-intents", ...)` | Separate stores read at the start of a request (today) (`separate_stores`) | Representative long-view carry-through across assigned quests, with nothing outside its branch |
| Critical Thinking | `critical-thinking` | `quest_ai_runner.run("critical-thinking", ...)` | Code-enforced before and after on deep runs only (today) (`deep_runs_only`) | Requests held to their ESOI metric by code |
| Quest account access | `access-quest-operations` | `quest_ai_runner.run("access-quest-operations", 'quest-id')` | Quest HTTP client from the environment (`quest_client`) | Parity of Quest operations across access points |
| Request intake | `take-in-the-world` | `quest_ai_runner.run("take-in-the-world", 'Save a note that the launch moves to Friday.')` | Channels, terminal and package as ways in (`ways_in`) | Ways in that pass their own metric |
| Context choice | `choose-the-context` | `quest_ai_runner.run("choose-the-context", 'What should I work on first this week?')` | Per-lane assembly of the context members (today) (`per_lane_assembly`) | Context handed to the planner |
| Subconscious | `the-subconscious` | `quest_ai_runner.run("the-subconscious", 'rust release')` | Start-of-run card filter and a recency window (today) (`recency_window`) | Goal-relevant admission at a precision floor with nothing out of scope |
| Justified model calls | `justify-model-calls` | no entry declared | Registered prompt specs with an enforcement test (today) (`spec_registry`) | Justified share of model spend |
| Cost-ordered model tier selector with outage and allowance recovery | `model-tier-chooser` | `quest_ai_runner.run("model-tier-chooser", {'available': ['anthropic']})` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |

## Problems and ideals

**Problems**

- Models, prices, tools and techniques change every few months, so a router, a retrieval method or a model choice that was right becomes wrong, and nobody notices until quality drops or cost rises. (`QP5`)
- Operators learn what AI work cost, and whether it was any good, only after the fact: a runaway run, a large model on an easy question, or a quiet drop in answer quality. (`QP6`)
- A short chat message is sometimes handled the wrong way: a plain question starts expensive autonomous work, a request to change something only gets talked about, or a destructive request runs without anyone confirming it. (`prob-misrouted-message`)
- A run reports that it finished when the written goal was not met, or describes work that was never executed. (`prob-claimed-done-not-done`)
- Steps that only a person should decide (spending, sending, deleting, anything irreversible) are sometimes taken without them, or are surfaced without the detail needed to decide, or are dropped without anyone noticing. (`prob-human-only-steps-slip`)
- Operators learn what AI work cost only after it has been spent, and a runaway loop, a long prompt or an overloaded host can burn a daily allowance or fail other jobs. (`prob-spend-surprises`)
- Most turns are easy yet are served by the largest model, or the cheapest model is used for work it cannot do, so quality or cost is wrong for the task. (`prob-big-model-for-easy-work`)
- A task someone queued for the AI can be lost, run twice, run before its time, run by the wrong persona, or end with no report. (`prob-queued-task-falls-through`)
- A live two-way chat over an external channel can drop, duplicate or reorder messages, or break when a payload arrives in an unexpected shape. (`prob-channel-messages-misdelivered`)
- A question about the world rather than the person's own material (health, training, research, experiences others have reported) is answered from the model's general knowledge or one web passage, with no sign of how strong the evidence is, and research sources a host already has are not consulted. (`prob-world-claims-unweighed`)
- and 23 more: all 33 are in [problems](#problems) of the full record below

**Ideals**

- Metric-led evolution (`QI5`): Every part of QAR improves as the state of the art moves, with the best implementation chosen by its metric.
- Visible value (`QI6`): What AI work costs and how good it is are known before they surprise anyone.
- Proportionate Routing (`ideal-proportionate-routing`): Each message gets the cheapest handling that still gets the right outcome.
- Exact Change (`ideal-exact-change`): The final state matches the request: everything asked, nothing else, once.
- Verified Completion (`ideal-verified-completion`): A report of done is true.
- Informed Consent (`ideal-informed-consent`): A human-only step reaches a person with enough detail to decide, and the AI stops until they do.
- Predictable Spend (`ideal-predictable-spend`): Cost is known before it is spent and bounded while it is spent.
- Right-Sized Model (`ideal-right-sized-model`): The cheapest model that can do the task does it, and waiting beats failing.
- Dependable Delivery (`ideal-dependable-delivery`): What is handed to the system arrives once, in order, and is accounted for.
- Weighed World Knowledge (`ideal-weighed-knowledge`): A claim about the world comes with where it came from and how strong its evidence is, and the best evidence source available is consulted before the cheapest.
- and 27 more: all 37 are in [ideals](#ideals) of the full record below

## Metrics and their leaders

- Provider routing, fallback, retry and cost correctness (`metric-call-a-model`): leader `multi_provider`, 56 of 56 cases
- Channel transport conformance to the gateway wire contract (`metric-talk-over-a-channel`): leader `openclaw_bridge`, 20 of 20 cases
- Overseer digest evidence and signal correctness on labelled scenarios (`metric-watch-a-run`): leader `digest_probe`, 16 of 16 cases
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): leader `qar_usage`, 38 of 39 cases
- Parts selected by measurement (`QME7`): leader `structure_library`, 13 of 17 cases
- Supported and calibrated answers to world questions (`metric-know-the-world`): leader `web_with_support_check`, 9 of 9 cases
- Hub of hubs (`metric-hub-of-hubs`): leader `registry_scan`, 244 of 271 cases
- Hub of hubs (`QME9`): leader `registry_scan`, 244 of 271 cases
- Correct selection decisions over the evolve-by-metric cases (`metric-metric-earns-its-run`): leader `structure_library`, 13 of 17 cases
- Intents whose solutions one paragraph and one diagram explain as the code builds them (`metric-solution-coherence`)
- and 19 more: all 29 are in [metrics](#metrics) of the full record below

## Where to go next

- Sub-intent: [Body](../quest_ai_runner/body/README.md) (What the AI says about a person's account is the account as it is now, and what it changed in the account is read back after the change, so the person can trust both.)
- Sub-intent: [Critical Thinking](../quest_ai_runner/critical_thinking/README.md) (Everything the AI does for a person's goals is held to the metric that serves it, and what it says is held to what is known.)
- Sub-intent: [Holding the intents across quests](../quest_ai_runner/hold_the_intents/README.md) (Each AI representative starts every request from the standing view of the quests it is assigned and the intents above them, so a correction is made once and a thread is never restated.)
- Sub-intent: [Integrity](../quest_ai_runner/integrity/README.md) (Every part of QAR stays specified as a solution with alternatives and a metric, so the best implementation runs as the state of the art moves and no vendor is built into the brain.)
- Sub-intent: [Mind](../quest_ai_runner/mind/README.md) (Every step of a person's request acts on everything the request has established so far, so a reply rests on the work done for it and the person can follow that work, at the cost of the fast tier.)
- Sub-intent: [Nervous System](../quest_ai_runner/nervous_system/README.md) (Every run is watched through cheap checks, and a step only a person may take reaches that person with enough detail to decide.)
- Sub-intent: [Science](../quest_ai_runner/science/README.md) (What a request needs to know beyond the person's own material comes from the world with its source, so the person can weigh it.)
- Sub-intent: [Selecting the context a planner reads](../quest_ai_runner/select_context/README.md) (The material a planner reads before it plans, whether a card, a file, a conversation or a specification record, is the material the request concerns, however the request is worded.)
- Sub-intent: [Subconscious](../quest_ai_runner/subconscious/README.md) (A request recalls the part of the person's own knowledge its goal needs, from cards that are current with their sources, found by meaning and proportionate in size, and never from anyone else's.)
- Sub-intent: [Taking in requests from anywhere](../quest_ai_runner/take_in_the_world/README.md) (A person or a program reaches the runner from wherever they work, by a message, a terminal, a live channel or the package itself, and is understood the same way.)
- Repository: [top README](../README.md)
- Records beside the code: [quest_ai_runner](../quest_ai_runner/README.md), [quest_ai_runner/adapters](../quest_ai_runner/adapters/README.md), [quest_ai_runner/core](../quest_ai_runner/core/README.md), [quest_ai_runner/runner](../quest_ai_runner/runner/README.md), [evaluation/components/choose_a_model_tier/impls](../evaluation/components/choose_a_model_tier/impls/README.md), [evaluation/components/evolve_by_metric/impls](../evaluation/components/evolve_by_metric/impls/README.md), [evaluation/components/hub_of_hubs/impls](../evaluation/components/hub_of_hubs/impls/README.md), [evaluation/components/know_the_world/impls](../evaluation/components/know_the_world/impls/README.md), [evaluation/components/model_providers/impls](../evaluation/components/model_providers/impls/README.md), [evaluation/components/overseer_signals/impls](../evaluation/components/overseer_signals/impls/README.md), [evaluation/components/talk_over_a_channel/impls](../evaluation/components/talk_over_a_channel/impls/README.md), [evaluation/components/track_usage_and_cost/impls](../evaluation/components/track_usage_and_cost/impls/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- Models, prices, tools and techniques change every few months, so a router, a retrieval method or a model choice that was right becomes wrong, and nobody notices until quality drops or cost rises. (`QP5`)
- Operators learn what AI work cost, and whether it was any good, only after the fact: a runaway run, a large model on an easy question, or a quiet drop in answer quality. (`QP6`)
- A short chat message is sometimes handled the wrong way: a plain question starts expensive autonomous work, a request to change something only gets talked about, or a destructive request runs without anyone confirming it. (`prob-misrouted-message`)
- A run reports that it finished when the written goal was not met, or describes work that was never executed. (`prob-claimed-done-not-done`)
- Steps that only a person should decide (spending, sending, deleting, anything irreversible) are sometimes taken without them, or are surfaced without the detail needed to decide, or are dropped without anyone noticing. (`prob-human-only-steps-slip`)
- Operators learn what AI work cost only after it has been spent, and a runaway loop, a long prompt or an overloaded host can burn a daily allowance or fail other jobs. (`prob-spend-surprises`)
- Most turns are easy yet are served by the largest model, or the cheapest model is used for work it cannot do, so quality or cost is wrong for the task. (`prob-big-model-for-easy-work`)
- A task someone queued for the AI can be lost, run twice, run before its time, run by the wrong persona, or end with no report. (`prob-queued-task-falls-through`)
- A live two-way chat over an external channel can drop, duplicate or reorder messages, or break when a payload arrives in an unexpected shape. (`prob-channel-messages-misdelivered`)
- A question about the world rather than the person's own material (health, training, research, experiences others have reported) is answered from the model's general knowledge or one web passage, with no sign of how strong the evidence is, and research sources a host already has are not consulted. (`prob-world-claims-unweighed`)
- A run ends by handing the person steps the AI could have done itself, or is marked done with part of the original ask left undone, instead of continuing until the original ask is met. (`prob-work-handed-back`)
- Risky moments in a run (re-proposing a declined act, claiming work that did not run, handing back a step the AI could do, a wrong route, an irreversible act, a cross-scope read) are watched by one overseer that reviews a digest with one prompt on one tier, whatever the risk, and nothing shows which response works for which kind of risk. (`prob-risk-handled-uniformly`)
- A part is evaluated either only on offline fixtures, which may not hold on real inputs, or in full on real inputs through paid model calls, so the evidence arrives either too thin to trust or too late and too costly. (`prob-evaluation-not-staged-on-real-inputs`)
- QAR's search for evidence about the world is built apart from the research chat's search of science, so one science question can be searched twice by two routes that each gather and weigh evidence in their own way. (`prob-science-search-duplicates-research-chat`)
- People can attach AI tasks to their goals in Quest and schedule them, but nothing runs them: Quest queues the work and has no worker. (`QP1`)
- A general assistant answers from general knowledge. (`QP2`)
- An AI that may act on someone's behalf tends to one of two failures: it asks permission for everything, so nothing moves without a person, or it takes steps that only a person should decide (spending, sending, deleting, anything irreversible). (`QP3`)
- Grounding, routing, deep work, escalation, a task executor, cost control: each product or team that wants AI help writes these again, privately, and the copies drift apart. (`QP4`)
- Within one request, each planning step is a separate model call rebuilt from parts, not a continuation of the step before. (`prob-step-loses-the-request`)
- What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it reports the same state two ways, repeats a change it already made, or proposes again what it said was done. (`prob-account-state-contradicts`)
- What is known across requests (what a quest has decided, declined and learned, what the person corrected, where the work is heading) is kept in several stores with their own keys, budgets and retrieval: past conversations, recent context, the feedback ledger and its guidance, the prediction store, the host's reflections and the quest cards. (`prob-long-view-scattered`)
- Questions about how to use the app itself (how to do something, what a feature is for) should be answered from the product's own documentation, but that path is untested, the AI sometimes says it cannot do what it can, and many team members do not know what the AI can do for them. (`prob-app-help-unreliable`)
- Most people have no external environment connected, so their delegated tasks can use only the app's own operations, its documentation, a review of their own data and web search, and that path has not been tested end to end. (`prob-no-environment-tasks-untested`)
- Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose or find an experiment that would settle it, though the app has an Experiments feature built for that. (`prob-experiments-not-proposed`)
- The before-and-after reading of the relevant ESOI metric is enforced by code only on deep runs. (`prob-metric-enforced-on-deep-runs-only`)
- A representative's high-level view legitimately spans its assigned quests and the intents above them, but the library's fence compares flat tag sets: an item is visible to a turn only when their tags intersect, or everywhere when it has none. (`prob-long-view-scope-not-hierarchical`)
- People ask Quest AI why a Quest feature works as it does (why creating a quest asks many questions, what a daily reflection is for) and get a description of the feature, or a generic answer, instead of the reason it exists and how it helps their own goal. (`prob-quest-feature-why-unexplained`)
- A run can be judged against its goal condition by a reply that restates the condition as if it were met, so the done-standard is satisfied in words and no evidence shows the work was done. (`prob-goal-condition-restated-not-evidence`)
- A goal that covers a set (every entry, all records) can be reported as done after only part of the set was processed, because the reply reads well and does not admit the gap. (`prob-partial-coverage-reported-as-complete`)
- A long run shows nothing of itself until it ends, so the person cannot tell whether it is working, stuck or finished, and cannot steer it. (`prob-long-run-silent-until-the-end`)
- Quest's operations can be reached in the same process as Quest and over its HTTP API, and two access points to the same operations can drift apart, so the same request may behave or return differently depending on how it arrived. (`prob-two-access-points-to-quest-diverge`)
- Running a coding-agent harness on the Claude Code subscription for each step of a request is costly in tokens and slow to start, while a cheap fast model alone lacks the context discipline and reliability of a long coding-agent session. (`prob-harness-dear-and-slow-on-the-subscription`)
- What of the person's own material enters a step's view is decided by position and size, not by what the request needs: cards are chosen once from the request text at the start, the newest few observations are shown in full and older ones collapsed, and a lookup's result is admitted whether or not it is what was asked for. (`prob-admission-by-recency-not-relevance`)

### Ideals

- Metric-led evolution (`QI5`): Every part of QAR improves as the state of the art moves, with the best implementation chosen by its metric.
- Visible value (`QI6`): What AI work costs and how good it is are known before they surprise anyone.
- Proportionate Routing (`ideal-proportionate-routing`): Each message gets the cheapest handling that still gets the right outcome.
- Exact Change (`ideal-exact-change`): The final state matches the request: everything asked, nothing else, once.
- Verified Completion (`ideal-verified-completion`): A report of done is true.
- Informed Consent (`ideal-informed-consent`): A human-only step reaches a person with enough detail to decide, and the AI stops until they do.
- Predictable Spend (`ideal-predictable-spend`): Cost is known before it is spent and bounded while it is spent.
- Right-Sized Model (`ideal-right-sized-model`): The cheapest model that can do the task does it, and waiting beats failing.
- Dependable Delivery (`ideal-dependable-delivery`): What is handed to the system arrives once, in order, and is accounted for.
- Weighed World Knowledge (`ideal-weighed-knowledge`): A claim about the world comes with where it came from and how strong its evidence is, and the best evidence source available is consulted before the cheapest.
- Risk Handled Where It Is (`ideal-risk-handled-where-it-is`): Each high-risk case gets the response proven best for its kind of risk, and a low-risk case pays nothing for review.
- Staged Real-Input Evaluation (`ideal-staged-evaluation-on-real-inputs`): Evaluation starts free and offline, then moves to small samples of real inputs, and a larger paid run happens only when a decision still depends on it.
- One Science Search (`ideal-science-through-one-search`): A science question is answered through one search path shared by the chat and the research interface, so evidence is gathered and weighed once.
- Self-advancing goals (`QI1`): Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote.
- Personally grounded help (`QI2`): Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- Trusted autonomy (`QI3`): The AI acts first on what it may decide and brings the person exactly the decisions that are theirs.
- One shared engine (`QI4`): One domain-free engine serves every consumer, every lane and both chat and scheduled work, and differs only in configuration.
- Grounded Reply (`ideal-grounded-reply`): A reply says only what the person's own account and its sources support, and says what it found.
- Remembered Work (`ideal-remembered-work`): Work already worked out once is not worked out again, and what was learned is kept.
- Reliable Verdict (`ideal-reliable-verdict`): A narrow judgment changes a turn only when it is usable.
- Checked Improvement per Run (`ideal-checked-improvement`): Every run leaves, on the record it served, what it measured, what it moved and what it changed in the record and why, so improvement is checked per run instead of hoped for.
- Continuous Mind (`ideal-continuous-mind`): Every step of a request acts with everything the request has established so far: what the person said, refused and corrected, what the request was resolved to mean, and what earlier steps found.
- Known Reach (`ideal-known-reach`): The AI says it can do only what it can, says it cannot only after checking, and claims an outside act only with the receipt of the act.
- Consistent Account State (`ideal-consistent-account-state`): What the AI says about the person's own account is true of the account when it says it, every time in a session.
- Standing Perspective (`ideal-standing-perspective`): Each quest carries one view across requests of what was decided, declined, learned and where it is heading, and every request on it starts from that view.
- Questions Only Where Theirs (`ideal-asks-only-what-is-theirs`): The person is asked only what is theirs to decide, and a question they waved off is not asked again.
- Self-Explaining App (`ideal-self-explaining-app`): Any question about how to use the app gets a correct answer from its own documentation, and the AI knows what it can do.
- Most-Likely Path (`ideal-most-likely-path`): Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.
- Hierarchy-Contained Scope (`ideal-hierarchy-contained-scope`): What a representative learns reaches its own assigned quests and the intents above them, and never another representative's quests, a sibling intent or another person.
- Right Representative (`ideal-right-rep`): Each request is handled by the representative whose role and assigned quests fit it, and work spawned from a thread keeps that thread's representative unless the person chooses another.
- Explained Feature Purpose (`ideal-quest-feature-why-explained`): When a person asks why a Quest feature works as it does, the answer gives the reason the feature exists and how it serves their goal, not only what it is.
- Evidenced Completion (`ideal-done-standard-evidenced`): Work is reported as done only when evidence shows its written done-standard was met, and the evidence is not a restatement of the standard.
- Stated Coverage (`ideal-coverage-met-or-gap-stated`): When a goal covers a set (every entry, all records), either the whole set was processed or the report states exactly how much was and what remains.
- Visible Progress (`ideal-incremental-output-while-working`): While work is running, the person can see that it is still working and what it has done so far, at a level they choose.
- Shared Quest Operations (`ideal-one-implementation-two-access-points`): Quest's operations are implemented once, and each way of reaching them (in the same process as Quest, or over its HTTP API) gives the same results.
- Lean Context Harness (`ideal-cheap-fast-harness-with-ideal-context`): The harness that runs Quest AI's steps is as capable as a coding agent but costs and takes about what the fast tier does, because every step sees exactly the context its goal needs.
- Goal-Relevant Attention (`ideal-goal-relevant-attention`): Only what bears on the request's goal enters the working stream, and nothing from outside the request's scope ever does.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Tiered model provider layer | `vendor-model-caller` | Prefix-routing multi-provider (`multi_provider`) | Provider routing, fallback, retry and cost correctness |
| Live messaging channel transport | `talk-over-a-channel` | MCP gateway bridge transport (`openclaw`) | Channel transport conformance to the gateway wire contract |
| Nervous System | `nervous-system` | Digest-driven overseer (`overseer`) | Overseer digest evidence and signal correctness on labelled scenarios |
| Cost estimation, daily budget and host load guard | `track-usage-and-cost` | Live usage evaluation arm (`qar_usage`) | Cost, budget and resource-guard correctness |
| Integrity | `system-integrity` | Apply the evolvable-structure library (`structure-library`) | Parts selected by measurement, Correct selection decisions over the evolve-by-metric cases, Intents whose solutions one paragraph and one diagram explain as the code builds them, Hub of hubs |
| Science | `the-science` | Web research with the answer's support check (today) (`web_with_support_check`) | Supported and calibrated answers to world questions |
| Grounded goal agent | `do-grounded-work` | quest-ai-runner library (`library`) | QAR overall score, Passing chat turns per dollar, Delegated tasks meeting their standard, Exact and consented changes, Data kept in scope, Engine reuse, Measured coverage |
| Queued-task runner | `run-queued-tasks` | Offline lifecycle harness (`qar_runner`) | Task lifecycle correctness over offline cases |
| Mind | `the-mind` | Rebuilt prompt per step with a referenced context (today) (`rebuilt_per_step`) | Step continuity at cost over multi-step requests |
| Body | `the-body` | Managed quest card plus optional live reads (today) (`card_and_reads`) | Account-state consistency across scripted sessions |
| Higher mind | `hold-the-intents` | Separate stores read at the start of a request (today) (`separate_stores`) | Representative long-view carry-through across assigned quests, with nothing outside its branch |
| Critical Thinking | `critical-thinking` | Code-enforced before and after on deep runs only (today) (`deep_runs_only`) | Requests held to their ESOI metric by code |
| Quest account access | `access-quest-operations` | Quest HTTP client from the environment (`quest_client`) | Parity of Quest operations across access points |
| Request intake | `take-in-the-world` | Channels, terminal and package as ways in (`ways_in`) | Ways in that pass their own metric |
| Context choice | `choose-the-context` | Per-lane assembly of the context members (today) (`per_lane_assembly`) | Context handed to the planner |
| Subconscious | `the-subconscious` | Start-of-run card filter and a recency window (today) (`recency_window`) | Goal-relevant admission at a precision floor with nothing out of scope |
| Justified model calls | `justify-model-calls` | Registered prompt specs with an enforcement test (today) (`spec_registry`) | Justified share of model spend |
| Cost-ordered model tier selector with outage and allowance recovery | `model-tier-chooser` | Live tier registry (`model_registry`) | Model tier selection and limit-recognition correctness |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Per-consumer worker | `do-grounded-work:per-consumer-worker` |  |  |
| Agent framework with Quest glue | `do-grounded-work:agent-framework-with-quest-glue` |  |  |
| Deep runner only | `do-grounded-work:deep-runner-only` |  |  |
| Carried message stream per request (proposed) | `the-mind:append_only_stream` |  |  |
| Carried stream with compaction (proposed) | `the-mind:stream_with_compaction` |  |  |
| Small-model judge of every candidate (proposed) | `the-subconscious:goal_judge` |  |  |
| Score thresholds per source (proposed) | `the-subconscious:score_thresholds` |  |  |
| Thresholds with a judge on the borderline (proposed) | `the-subconscious:thresholds_then_judge` |  |  |
| Host research sources first, then the web (proposed) | `the-science:research_sources_first` |  |  |
| Findings weighed by a host's evidence-estimation service (proposed) | `the-science:evidence_weighted` |  |  |
| Read back what a change touched (proposed) | `the-body:read_after_write` |  |  |
| A live read behind every state claim (proposed) | `the-body:live_read_only` |  |  |
| Maintained view per quest only (proposed) | `hold-the-intents:per_quest_note` |  |  |
| Periodic reflection on the best tier (proposed) | `hold-the-intents:periodic_reflection` |  |  |
| Outcome precedents surfaced at admission (proposed) | `the-subconscious:outcome_precedents` |  |  |
| Overseer that asks questions of fact (proposed) | `nervous-system:fact_questions` |  |  |
| Every request measured by code (proposed) | `critical-thinking:every_request_measured` |  |  |
| Measured, proposed and answered (proposed) | `critical-thinking:measured_proposed_and_answered` |  |  |
| Per-risk response chosen by metric (proposed) | `nervous-system:per_risk_response` |  |  |
| One view per intent, cascading down (proposed) | `hold-the-intents:per_intent_cascade` |  |  |
| One view per representative across its assigned quests, placed in the hierarchy (proposed, recommended) | `hold-the-intents:per_rep_assigned_view` |  |  |
| Registry plus per-call ablation against the owner metric (proposed) | `justify-model-calls:spec_registry_ablation` |  |  |
| quest-ai-runner library | `do-grounded-work:library` | [quest_ai_runner/goal_agent.py](../quest_ai_runner/goal_agent.py) |  |
| Prefix-routing multi-provider | `vendor-model-caller:multi_provider` | [quest_ai_runner/providers.py](../quest_ai_runner/providers.py) |  |
| Digest-driven overseer | `nervous-system:overseer` | [quest_ai_runner/nervous_system/area.py](../quest_ai_runner/nervous_system/area.py) | [nervous_system](nervous_system/README.md) |
| Offline lifecycle harness | `run-queued-tasks:qar_runner` | [quest_ai_runner/area_body.py](../quest_ai_runner/area_body.py) |  |
| Static price table lookup | `track-usage-and-cost:pricing` | [quest_ai_runner/pricing.py](../quest_ai_runner/pricing.py) |  |
| Persisted daily token tracker | `track-usage-and-cost:usage` | [quest_ai_runner/usage.py](../quest_ai_runner/usage.py) |  |
| Hysteresis resource guard | `track-usage-and-cost:resources` | [quest_ai_runner/resources.py](../quest_ai_runner/resources.py) |  |
| Apply the evolvable-structure library | `system-integrity:structure-library` | [quest_ai_runner/integrity/area.py](../quest_ai_runner/integrity/area.py) | [integrity](integrity/README.md) |
| Rebuilt prompt per step with a referenced context (today) | `the-mind:rebuilt_per_step` | [quest_ai_runner/area_mind.py](../quest_ai_runner/area_mind.py) | [mind](mind/README.md) |
| Web research with the answer's support check (today) | `the-science:web_with_support_check` | [quest_ai_runner/science/area.py](../quest_ai_runner/science/area.py) | [science](science/README.md) |
| Managed quest card plus optional live reads (today) | `the-body:card_and_reads` | [quest_ai_runner/area_body.py](../quest_ai_runner/area_body.py) | [body](body/README.md) |
| Separate stores read at the start of a request (today) | `hold-the-intents:separate_stores` | [quest_ai_runner/hold_the_intents/area.py](../quest_ai_runner/hold_the_intents/area.py) | [hold_the_intents](hold_the_intents/README.md) |
| Code-enforced before and after on deep runs only (today) | `critical-thinking:deep_runs_only` | [quest_ai_runner/critical_thinking/area.py](../quest_ai_runner/critical_thinking/area.py) | [critical_thinking](critical_thinking/README.md) |
| Channels, terminal and package as ways in | `take-in-the-world:ways_in` | [quest_ai_runner/area_take_in_the_world.py](../quest_ai_runner/area_take_in_the_world.py) | [take_in_the_world](take_in_the_world/README.md) |
| Quest HTTP client from the environment | `access-quest-operations:quest_client` | [quest_ai_runner/area_body.py](../quest_ai_runner/area_body.py) |  |
| Per-lane assembly of the context members (today) | `choose-the-context:per_lane_assembly` | [quest_ai_runner/select_context/area.py](../quest_ai_runner/select_context/area.py) | [select_context](select_context/README.md) |
| Keyless command-line provider | `vendor-model-caller:claude_cli` | [quest_ai_runner/adapters/claude_cli_provider.py](../quest_ai_runner/adapters/claude_cli_provider.py) |  |
| MCP gateway bridge transport | `talk-over-a-channel:openclaw` | [quest_ai_runner/adapters/openclaw_channel.py](../quest_ai_runner/adapters/openclaw_channel.py) |  |
| Rebuilt prompt per step with the context re-sent from cache | `the-mind:rebuilt_full_context` | [quest_ai_runner/core/orchestrator_planning.py](../quest_ai_runner/core/orchestrator_planning.py) |  |
| Start-of-run card filter and a recency window (today) | `the-subconscious:recency_window` | [quest_ai_runner/core/planner_context.py](../quest_ai_runner/core/planner_context.py) | [subconscious](subconscious/README.md) |
| No overseer (configuration off) | `nervous-system:no_overseer` | [quest_ai_runner/core/orchestrator_types.py](../quest_ai_runner/core/orchestrator_types.py) |  |
| Standing instruction alone (floor) | `critical-thinking:instruction_only` | [quest_ai_runner/core/standing_instructions.py](../quest_ai_runner/core/standing_instructions.py) |  |
| Registered prompt specs with an enforcement test (today) | `justify-model-calls:spec_registry` | [quest_ai_runner/core/prompt_specs.py](../quest_ai_runner/core/prompt_specs.py) |  |
| Difficulty-to-rung starter | `model-tier-chooser:deep_model_selection` | [quest_ai_runner/core/deep_model_selection.py](../quest_ai_runner/core/deep_model_selection.py) |  |
| Live tier registry | `model-tier-chooser:model_registry` | [quest_ai_runner/core/model_registry.py](../quest_ai_runner/core/model_registry.py) |  |
| Subscription allowance limit detector | `model-tier-chooser:usage_limit` | [quest_ai_runner/core/usage_limit.py](../quest_ai_runner/core/usage_limit.py) |  |
| Task poller and claimer | `run-queued-tasks:poller` | [quest_ai_runner/runner/poller.py](../quest_ai_runner/runner/poller.py) |  |
| Single-task executor | `run-queued-tasks:executor` | [quest_ai_runner/runner/executor.py](../quest_ai_runner/runner/executor.py) |  |
| Configured persona resolver | `run-queued-tasks:personas` | [quest_ai_runner/runner/personas.py](../quest_ai_runner/runner/personas.py) |  |
| Work-folder sync | `run-queued-tasks:quest_folder_sync` | [quest_ai_runner/runner/quest_folder_sync.py](../quest_ai_runner/runner/quest_folder_sync.py) |  |
| Shared lane entry point | `run-queued-tasks:lane` | [quest_ai_runner/runner/lane.py](../quest_ai_runner/runner/lane.py) |  |
| Live selection evaluation arm | `model-tier-chooser:qar_selection` | [evaluation/components/choose_a_model_tier/impls/qar_selection.py](../evaluation/components/choose_a_model_tier/impls/qar_selection.py) |  |
| Evolvable-structure library decides (live) | `system-integrity:structure_library` | [evaluation/components/evolve_by_metric/impls/structure_library.py](../evaluation/components/evolve_by_metric/impls/structure_library.py) |  |
| Keep the selection as written (floor) | `system-integrity:keep_current` | [evaluation/components/evolve_by_metric/impls/keep_current.py](../evaluation/components/evolve_by_metric/impls/keep_current.py) |  |
| Registry read together with the code (today) | `system-integrity:registry_scan` | [evaluation/components/hub_of_hubs/impls/registry_scan.py](../evaluation/components/hub_of_hubs/impls/registry_scan.py) |  |
| Memory only, no research (floor) | `the-science:memory_only` | [evaluation/components/know_the_world/impls/memory_only.py](../evaluation/components/know_the_world/impls/memory_only.py) |  |
| Single flat provider floor | `vendor-model-caller:single_provider` | [evaluation/components/model_providers/impls/single_provider.py](../evaluation/components/model_providers/impls/single_provider.py) |  |
| No-judgement digest probe | `nervous-system:digest_probe` | [evaluation/components/overseer_signals/impls/digest_probe.py](../evaluation/components/overseer_signals/impls/digest_probe.py) |  |
| Live overseer model arm | `nervous-system:live_model` | [evaluation/components/overseer_signals/impls/live_model.py](../evaluation/components/overseer_signals/impls/live_model.py) |  |
| Fake-gateway conformance arm | `talk-over-a-channel:openclaw_bridge` | [evaluation/components/talk_over_a_channel/impls/openclaw_bridge.py](../evaluation/components/talk_over_a_channel/impls/openclaw_bridge.py) |  |
| Live usage evaluation arm | `track-usage-and-cost:qar_usage` | [evaluation/components/track_usage_and_cost/impls/qar_usage.py](../evaluation/components/track_usage_and_cost/impls/qar_usage.py) |  |

### Metrics

- Provider routing, fallback, retry and cost correctness (`metric-call-a-model`): `esoi run model_providers --impl multi_provider`
- Channel transport conformance to the gateway wire contract (`metric-talk-over-a-channel`): `esoi run talk_over_a_channel --impl openclaw_bridge`
- Overseer digest evidence and signal correctness on labelled scenarios (`metric-watch-a-run`): `esoi run overseer_signals --impl live_model; esoi run overseer_signals --impl digest_probe`
- Cost, budget and resource-guard correctness (`metric-track-usage-and-cost`): `esoi run track_usage_and_cost --impl qar_usage`
- Parts selected by measurement (`QME7`): `esoi run evolve_by_metric --impl structure_library`
- Supported and calibrated answers to world questions (`metric-know-the-world`): `esoi run know_the_world --impl web_with_support_check`
- Hub of hubs (`metric-hub-of-hubs`): `esoi run hub_of_hubs --impl registry_scan`
- Hub of hubs (`QME9`): `esoi run hub_of_hubs --impl registry_scan`
- Correct selection decisions over the evolve-by-metric cases (`metric-metric-earns-its-run`): `esoi run evolve_by_metric --impl structure_library`
- Intents whose solutions one paragraph and one diagram explain as the code builds them (`metric-solution-coherence`): `python3 -m esoi.coherence --gate`
- Passing chat turns per dollar (`QME1`): `esoi run chat_quality --impl dev_quest_chat --sample 10 --max-calls 300`
- Delegated tasks meeting their standard (`QME2`): `none`
- Exact and consented changes (`QME3`): `none`
- Data kept in scope (`QME4`): `none`
- Engine reuse (`QME5`): `none`
- Measured coverage (`QME6`): `none`
- QAR overall score (`metric-qar-overall`): `esoi run qar_overall --impl recorded_rows`
- Task lifecycle correctness over offline cases (`metric-run-queued-tasks`): `esoi run run_queued_tasks --impl qar_runner`
- Step continuity at cost over multi-step requests (`metric-keep-a-working-stream`): `esoi run keep_a_working_stream --impl rebuilt_per_step --sample 3 --max-calls 40`
- Account-state consistency across scripted sessions (`metric-act-as-the-body`): `esoi run act_as_the_body --impl card_and_reads`
- Representative long-view carry-through across assigned quests, with nothing outside its branch (`metric-hold-the-long-view`): `esoi run hold_the_long_view --impl separate_stores`
- Requests held to their ESOI metric by code (`metric-think-critically`): `esoi run think_critically --impl deep_runs_only`
- Parity of Quest operations across access points (`metric-quest-operations-parity`): `none`
- Ways in that pass their own metric (`metric-take-in-the-world`): `none`
- Context handed to the planner (`metric-choose-the-context`): `esoi run context_choice --impl per_lane_assembly`
- Goal-relevant admission at a precision floor with nothing out of scope (`metric-admit-to-the-mind`): `esoi run admit_to_the_mind --impl recency_window`
- Justified share of model spend (`metric-justified-model-spend`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Justified model spend (`QME8`): `esoi run justified_model_spend --impl spec_registry_ablation`
- Model tier selection and limit-recognition correctness (`metric-choose-a-model-tier`): `esoi run choose_a_model_tier --impl qar_selection`
