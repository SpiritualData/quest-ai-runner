# Grounded AI that advances people's goals

Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote, and every answer and action rests on the person's own goals, material and preferences, and never on another person's.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

Opportunities: Other products that need an AI which works from a person's own goals and material could reuse this engine, so the grounding, the standard for delegated work and the scope fences are built once and shared.

## How the solutions fit together

quest-ai-runner turns a person's request or queued task into grounded work in one loop, and do-grounded-work is that loop. It asks the model through call-a-model, works in the person's Quest account through access-quest-operations and takes in requests through take-in-the-world. It reasons step by step through reason-and-answer, recalls what a request needs through remember-and-recall and acts on the account through act-as-the-body. It holds its work to the metric that serves it through think-critically, has every run watched through watch-for-risk and keeps the long view across turns through hold-the-intents. It stays comparable by metric through keep-it-generic and answers questions about the world through know-the-world. remember-and-recall recalls the cards that match a request through card-store. call-a-model accounts for every call through justify-model-calls and track-usage-and-cost. reason-and-answer sends the prompt of each step to the model through call-a-model. watch-for-risk has the model oversee a run through call-a-model. run-queued-tasks claims, runs and reports a queued task, asking the model through call-a-model and working in the account through access-quest-operations. act-as-the-body reads and changes the quest through persist-cards, access-quest-operations and run-queued-tasks. hold-the-intents saves what it learned through persist-cards. take-in-the-world hears from a live channel through talk-over-a-channel.

## Diagram

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
    s15 -->|"A per-prompt table and the share of average model spend..."| s0
```

## Solutions at a glance

| Solution | Entry call | Selected implementation | Metric |
|---|---|---|---|
| `call-a-model` | `quest_ai_runner.run("call-a-model", ...)` | `multi_provider` | Provider routing, fallback, retry and cost correctness |
| `talk-over-a-channel` | `quest_ai_runner.run("talk-over-a-channel")` | `openclaw` | Channel transport conformance to the gateway wire contract |
| `watch-for-risk` | `quest_ai_runner.run("watch-for-risk", 'delete the old backup folder')` | `overseer` | Overseer digest evidence and signal correctness on labelled scenarios |
| `track-usage-and-cost` | no entry declared | `evaluation/components/track_usage_and_cost/impls/qar_usage.py` | Cost, budget and resource-guard correctness |
| `keep-it-generic` | `quest_ai_runner.run("keep-it-generic", ['examplevendor'])` | `structure-library` | Parts selected by measurement, Intents whose solutions one paragraph and one diagram explain as the code builds them, Hub of hubs: coverage, integration by configuration, research freshness, code structure |
| `know-the-world` | `quest_ai_runner.run("know-the-world", 'meditation and attention')` | `web_with_support_check` | Supported and calibrated answers to world questions |
| `do-grounded-work` | `quest_ai_runner.run("do-grounded-work", 'What should I work on first this week?')` | `library` | QAR overall: do-grounded-work's sub-metrics weighted by its ideals, with floor and ceiling, Passing chat turns per dollar, Delegated tasks meeting their standard, Exact and consented changes, Data kept in scope, Engine reuse, Measured coverage |
| `run-queued-tasks` | `quest_ai_runner.run("run-queued-tasks", 'team-1')` | `qar_runner` | Task lifecycle correctness over offline cases |
| `reason-and-answer` | `quest_ai_runner.run("reason-and-answer", ...)` | `rebuilt_per_step` | Step continuity at cost over multi-step requests |
| `act-as-the-body` | `quest_ai_runner.run("act-as-the-body", 'NOTES.md')` | `card_and_reads` | Account-state consistency across scripted sessions |
| `hold-the-intents` | `quest_ai_runner.run("hold-the-intents", ...)` | `separate_stores` | Representative long-view carry-through across assigned quests, with nothing outside its branch |
| `think-critically` | `quest_ai_runner.run("think-critically", ...)` | `deep_runs_only` | Requests held to their ESOI metric by code |
| `access-quest-operations` | `quest_ai_runner.run("access-quest-operations", 'quest-id')` | `quest_client` | none yet |
| `take-in-the-world` | `quest_ai_runner.run("take-in-the-world", 'Save a note that the launch moves to Friday.')` | `ways_in` | Ways in that pass their own metric |
| `remember-and-recall` | `quest_ai_runner.run("remember-and-recall", 'rust release')` | `recency_window` | Goal-relevant admission at a precision floor with nothing out of scope |
| `justify-model-calls` | no entry declared | `spec_registry` | Justified share of model spend |

## Problems and ideals

**Problems**

- `QP5`: Models, prices, tools and techniques change every few months, so a router, a retrieval method or a model choice that was right becomes wrong, and nobody notices until quality drops or cost rises.
- `QP6`: Operators learn what AI work cost, and whether it was any good, only after the fact: a runaway run, a large model on an easy question, or a quiet drop in answer quality.
- `prob-misrouted-message`: A short chat message is sometimes handled the wrong way: a plain question starts expensive autonomous work, a request to change something only gets talked about, or a destructive request runs without...
- `prob-claimed-done-not-done`: A run reports that it finished when the written goal was not met, or describes work that was never executed.
- `prob-human-only-steps-slip`: Steps that only a person should decide (spending, sending, deleting, anything irreversible) are sometimes taken without them, or are surfaced without the detail needed to decide, or are dropped...
- `prob-spend-surprises`: Operators learn what AI work cost only after it has been spent, and a runaway loop, a long prompt or an overloaded host can burn a daily allowance or fail other jobs.
- `prob-big-model-for-easy-work`: Most turns are easy yet are served by the largest model, or the cheapest model is used for work it cannot do, so quality or cost is wrong for the task.
- `prob-queued-task-falls-through`: A task someone queued for the AI can be lost, run twice, run before its time, run by the wrong persona, or end with no report.
- `prob-channel-messages-misdelivered`: A live two-way chat over an external channel can drop, duplicate or reorder messages, or break when a payload arrives in an unexpected shape.
- `prob-world-claims-unweighed`: A question about the world rather than the person's own material (health, training, research, experiences others have reported) is answered from the model's general knowledge or one web passage, with...
- and 23 more: all 33 are in [problems](#problems) of the full record below

**Ideals**

- `QI5`, Metric-led evolution: Every part of QAR improves as the state of the art moves, with the best implementation chosen by its metric.
- `QI6`, Visible value: What AI work costs and how good it is are known before they surprise anyone.
- `ideal-proportionate-routing`, Proportionate Routing: Each message gets the cheapest handling that still gets the right outcome.
- `ideal-exact-change`, Exact Change: The final state matches the request: everything asked, nothing else, once.
- `ideal-verified-completion`, Verified Completion: A report of done is true.
- `ideal-informed-consent`, Informed Consent: A human-only step reaches a person with enough detail to decide, and the AI stops until they do.
- `ideal-predictable-spend`, Predictable Spend: Cost is known before it is spent and bounded while it is spent.
- `ideal-right-sized-model`, Right-Sized Model: The cheapest model that can do the task does it, and waiting beats failing.
- `ideal-dependable-delivery`, Dependable Delivery: What is handed to the system arrives once, in order, and is accounted for.
- `ideal-weighed-knowledge`, Weighed World Knowledge: A claim about the world comes with where it came from and how strong its evidence is, and the best evidence source available is consulted before the cheapest.
- and 27 more: all 37 are in [ideals](#ideals) of the full record below

## Metrics and their leaders

- `metric-call-a-model`, Provider routing, fallback, retry and cost correctness: leader `multi_provider`, 56 of 56 cases
- `metric-talk-over-a-channel`, Channel transport conformance to the gateway wire contract: leader `openclaw_bridge`, 20 of 20 cases
- `metric-watch-a-run`, Overseer digest evidence and signal correctness on labelled scenarios: leader `digest_probe`, 16 of 16 cases
- `metric-track-usage-and-cost`, Cost, budget and resource-guard correctness: leader `qar_usage`, 38 of 39 cases
- `QME7`, Parts selected by measurement: leader `structure_library`, 13 of 17 cases
- `metric-know-the-world`, Supported and calibrated answers to world questions: leader `web_with_support_check`, 9 of 9 cases
- `metric-hub-of-hubs`, Hub of hubs: coverage, integration by configuration, research freshness, code structure: leader `registry_scan`, 244 of 271 cases
- `QME9`, Hub of hubs: leader `registry_scan`, 244 of 271 cases
- `metric-metric-earns-its-run`, : leader `structure_library`, 13 of 17 cases
- `metric-solution-coherence`, Intents whose solutions one paragraph and one diagram explain as the code builds them
- and 17 more: all 27 are in [metrics](#metrics) of the full record below

## Where to go next

- Sub-intent: [Acting in the person's account as the AI's own body](../quest_ai_runner/act_as_the_body/README.md) (What the AI says about a person's account is the account as it is now, and what it changed in the account is read back after the change, so the person can trust both.)
- Sub-intent: [Holding the intents across quests](../quest_ai_runner/hold_the_intents/README.md) (Each AI representative starts every request from the standing view of the quests it is assigned and the intents above them, so a correction is made once and a thread is never restated.)
- Sub-intent: [Keeping every part generic and measured](../quest_ai_runner/keep_it_generic/README.md) (Every part of QAR stays specified as a solution with alternatives and a metric, so the best implementation runs as the state of the art moves and no vendor is built into the brain.)
- Sub-intent: [Knowing the world with its source](../quest_ai_runner/know_the_world/README.md) (What a request needs to know beyond the person's own material comes from the world with its source, so the person can weigh it.)
- Sub-intent: [Reasoning and answering each step of a request](../quest_ai_runner/reason_and_answer/README.md) (Every step of a person's request acts on everything the request has established so far, so a reply rests on the work done for it and the person can follow that work, at the cost of the fast tier.)
- Sub-intent: [Remembering and recalling the person's own knowledge](../quest_ai_runner/remember_and_recall/README.md) (A request recalls the part of the person's own knowledge its goal needs, from cards that are current with their sources, found by meaning and proportionate in size, and never from anyone else's.)
- Sub-intent: [Selecting the context a planner reads](../quest_ai_runner/select_context/README.md) (The material a planner reads before it plans, whether a card, a file, a conversation or a specification record, is the material the request concerns, however the request is worded.)
- Sub-intent: [Taking in requests from anywhere](../quest_ai_runner/take_in_the_world/README.md) (A person or a program reaches the runner from wherever they work, by a message, a terminal, a live channel or the package itself, and is understood the same way.)
- Sub-intent: [Thinking critically by metric](../quest_ai_runner/think_critically/README.md) (Everything the AI does for a person's goals is held to the metric that serves it, and what it says is held to what is known.)
- Sub-intent: [Watching every run for risk](../quest_ai_runner/watch_for_risk/README.md) (Every run is watched through cheap checks, and a step only a person may take reaches that person with enough detail to decide.)
- Repository: [top README](../README.md)
- Records beside the code: [quest_ai_runner](../quest_ai_runner/README.md), [quest_ai_runner/adapters](../quest_ai_runner/adapters/README.md), [quest_ai_runner/core](../quest_ai_runner/core/README.md), [quest_ai_runner/runner](../quest_ai_runner/runner/README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- `QP5`: Models, prices, tools and techniques change every few months, so a router, a retrieval method or a model choice that was right becomes wrong, and nobody notices until quality drops or cost rises.
- `QP6`: Operators learn what AI work cost, and whether it was any good, only after the fact: a runaway run, a large model on an easy question, or a quiet drop in answer quality.
- `prob-misrouted-message`: A short chat message is sometimes handled the wrong way: a plain question starts expensive autonomous work, a request to change something only gets talked about, or a destructive request runs without anyone confirming it. A clear instruction can also get a clarifying question back instead of being done.
- `prob-claimed-done-not-done`: A run reports that it finished when the written goal was not met, or describes work that was never executed.
- `prob-human-only-steps-slip`: Steps that only a person should decide (spending, sending, deleting, anything irreversible) are sometimes taken without them, or are surfaced without the detail needed to decide, or are dropped without anyone noticing.
- `prob-spend-surprises`: Operators learn what AI work cost only after it has been spent, and a runaway loop, a long prompt or an overloaded host can burn a daily allowance or fail other jobs.
- `prob-big-model-for-easy-work`: Most turns are easy yet are served by the largest model, or the cheapest model is used for work it cannot do, so quality or cost is wrong for the task.
- `prob-queued-task-falls-through`: A task someone queued for the AI can be lost, run twice, run before its time, run by the wrong persona, or end with no report.
- `prob-channel-messages-misdelivered`: A live two-way chat over an external channel can drop, duplicate or reorder messages, or break when a payload arrives in an unexpected shape.
- `prob-world-claims-unweighed`: A question about the world rather than the person's own material (health, training, research, experiences others have reported) is answered from the model's general knowledge or one web passage, with no sign of how strong the evidence is, and research sources a host already has are not consulted.
- `prob-work-handed-back`: A run ends by handing the person steps the AI could have done itself, or is marked done with part of the original ask left undone, instead of continuing until the original ask is met.
- `prob-risk-handled-uniformly`: Risky moments in a run (re-proposing a declined act, claiming work that did not run, handing back a step the AI could do, a wrong route, an irreversible act, a cross-scope read) are watched by one overseer that reviews a digest with one prompt on one tier, whatever the risk, and nothing shows which response works for which kind of risk.
- `prob-evaluation-not-staged-on-real-inputs`: 
- `prob-science-search-duplicates-research-chat`: 
- `QP1`: People can attach AI tasks to their goals in Quest and schedule them, but nothing runs them: Quest queues the work and has no worker. The same gap exists for anyone who wants AI to keep working on their goals between conversations.
- `QP2`: A general assistant answers from general knowledge. It does not know the person's quests, plans, documents, past conversations or preferences, so its help is plausible but not theirs, and sometimes states things about their account that are not true.
- `QP3`: An AI that may act on someone's behalf tends to one of two failures: it asks permission for everything, so nothing moves without a person, or it takes steps that only a person should decide (spending, sending, deleting, anything irreversible).
- `QP4`: Grounding, routing, deep work, escalation, a task executor, cost control: each product or team that wants AI help writes these again, privately, and the copies drift apart.
- `prob-step-loses-the-request`: Within one request, each planning step is a separate model call rebuilt from parts, not a continuation of the step before. From the second step on, the transcript and the whole context block (the resolved request, declined proposals, standing corrections, recent context and cards) are replaced by a note saying they were provided at step one, which a separate call never saw, and older observations are collapsed to one line, so a later step decides from the raw message and the newest reads alone.
- `prob-account-state-contradicts`: What the AI says about the person's own account (which quests exist, what changed, whether a change ran) is re-derived each turn from whatever slice it read, and a change it made is not read back, so within one session it reports the same state two ways, repeats a change it already made, or proposes again what it said was done.
- `prob-long-view-scattered`: What is known across requests (what a quest has decided, declined and learned, what the person corrected, where the work is heading) is kept in several stores with their own keys, budgets and retrieval: past conversations, recent context, the feedback ledger and its guidance, the prediction store, the host's reflections and the quest cards. None of them holds the quest's standing view, so each request rebuilds it from fragments, background work can lack what the chat already knew, and each store needs its own scope fence.
- `prob-app-help-unreliable`: Questions about how to use the app itself (how to do something, what a feature is for) should be answered from the product's own documentation, but that path is untested, the AI sometimes says it cannot do what it can, and many team members do not know what the AI can do for them.
- `prob-no-environment-tasks-untested`: Most people have no external environment connected, so their delegated tasks can use only the app's own operations, its documentation, a review of their own data and web search, and that path has not been tested end to end.
- `prob-experiments-not-proposed`: Advice and plans are given as one answer, not as the option most likely to work for this person with a metric to check it, and when the better option is unknown the AI does not propose or find an experiment that would settle it, though the app has an Experiments feature built for that.
- `prob-metric-enforced-on-deep-runs-only`: The before-and-after reading of the relevant ESOI metric is enforced by code only on deep runs. Chat turns and other in-process work find no slice and take no reading; a request whose records hold no slice or no metric ends with nothing measured and nothing proposed; a reading that got worse is recorded and shown, but nothing acts on it; and a run that ends with no metric on record is marked and then forgotten.
- `prob-long-view-scope-not-hierarchical`: A representative's high-level view legitimately spans its assigned quests and the intents above them, but the library's fence compares flat tag sets: an item is visible to a turn only when their tags intersect, or everywhere when it has none. Nothing can say visible to this representative's assigned quests and their ancestor intents, never to another representative's quests, a sibling intent or another person; and a representative's learned notes and context preferences carry no scope at all, so what it learned on one quest reaches every other.
- `prob-quest-feature-why-unexplained`: 
- `prob-goal-condition-restated-not-evidence`: 
- `prob-partial-coverage-reported-as-complete`: 
- `prob-long-run-silent-until-the-end`: 
- `prob-two-access-points-to-quest-diverge`: 
- `prob-harness-dear-and-slow-on-the-subscription`: 
- `prob-admission-by-recency-not-relevance`: What of the person's own material enters a step's view is decided by position and size, not by what the request needs: cards are chosen once from the request text at the start, the newest few observations are shown in full and older ones collapsed, and a lookup's result is admitted whether or not it is what was asked for. A finding the goal turns on can be collapsed out while an irrelevant read stays in full, and a near miss can be treated as the thing requested.

### Ideals

- `QI5`, Metric-led evolution: Every part of QAR improves as the state of the art moves, with the best implementation chosen by its metric.
- `QI6`, Visible value: What AI work costs and how good it is are known before they surprise anyone.
- `ideal-proportionate-routing`, Proportionate Routing: Each message gets the cheapest handling that still gets the right outcome.
- `ideal-exact-change`, Exact Change: The final state matches the request: everything asked, nothing else, once.
- `ideal-verified-completion`, Verified Completion: A report of done is true.
- `ideal-informed-consent`, Informed Consent: A human-only step reaches a person with enough detail to decide, and the AI stops until they do.
- `ideal-predictable-spend`, Predictable Spend: Cost is known before it is spent and bounded while it is spent.
- `ideal-right-sized-model`, Right-Sized Model: The cheapest model that can do the task does it, and waiting beats failing.
- `ideal-dependable-delivery`, Dependable Delivery: What is handed to the system arrives once, in order, and is accounted for.
- `ideal-weighed-knowledge`, Weighed World Knowledge: A claim about the world comes with where it came from and how strong its evidence is, and the best evidence source available is consulted before the cheapest.
- `ideal-risk-handled-where-it-is`, Risk Handled Where It Is: Each high-risk case gets the response proven best for its kind of risk, and a low-risk case pays nothing for review.
- `ideal-staged-evaluation-on-real-inputs`, : 
- `ideal-science-through-one-search`, : 
- `QI1`, Self-advancing goals: Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote.
- `QI2`, Personally grounded help: Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- `QI3`, Trusted autonomy: The AI acts first on what it may decide and brings the person exactly the decisions that are theirs.
- `QI4`, One shared engine: One domain-free engine serves every consumer, every lane and both chat and scheduled work, and differs only in configuration.
- `ideal-grounded-reply`, Grounded Reply: A reply says only what the person's own account and its sources support, and says what it found.
- `ideal-remembered-work`, Remembered Work: Work already worked out once is not worked out again, and what was learned is kept.
- `ideal-reliable-verdict`, Reliable Verdict: A narrow judgment changes a turn only when it is usable.
- `ideal-checked-improvement`, Checked Improvement per Run: Every run leaves, on the record it served, what it measured, what it moved and what it changed in the record and why, so improvement is checked per run instead of hoped for.
- `ideal-continuous-mind`, Continuous Mind: Every step of a request acts with everything the request has established so far: what the person said, refused and corrected, what the request was resolved to mean, and what earlier steps found.
- `ideal-known-reach`, Known Reach: The AI says it can do only what it can, says it cannot only after checking, and claims an outside act only with the receipt of the act.
- `ideal-consistent-account-state`, Consistent Account State: What the AI says about the person's own account is true of the account when it says it, every time in a session.
- `ideal-standing-perspective`, Standing Perspective: Each quest carries one view across requests of what was decided, declined, learned and where it is heading, and every request on it starts from that view.
- `ideal-asks-only-what-is-theirs`, Questions Only Where Theirs: The person is asked only what is theirs to decide, and a question they waved off is not asked again.
- `ideal-self-explaining-app`, Self-Explaining App: Any question about how to use the app gets a correct answer from its own documentation, and the AI knows what it can do.
- `ideal-most-likely-path`, Most-Likely Path: Every recommendation is the option most likely to move the person's own metric toward their intent, and what is not known becomes an experiment.
- `ideal-hierarchy-contained-scope`, Hierarchy-Contained Scope: What a representative learns reaches its own assigned quests and the intents above them, and never another representative's quests, a sibling intent or another person.
- `ideal-right-rep`, Right Representative: Each request is handled by the representative whose role and assigned quests fit it, and work spawned from a thread keeps that thread's representative unless the person chooses another.
- `ideal-quest-feature-why-explained`, : 
- `ideal-done-standard-evidenced`, : 
- `ideal-coverage-met-or-gap-stated`, : 
- `ideal-incremental-output-while-working`, : 
- `ideal-one-implementation-two-access-points`, : 
- `ideal-cheap-fast-harness-with-ideal-context`, : 
- `ideal-goal-relevant-attention`, Goal-Relevant Attention: Only what bears on the request's goal enters the working stream, and nothing from outside the request's scope ever does.

### Solutions

| Solution | Name | Selected | Metric |
|---|---|---|---|
| `call-a-model` | Tiered model provider layer | multi_provider | Provider routing, fallback, retry and cost correctness |
| `talk-over-a-channel` | Live messaging channel transport | openclaw | Channel transport conformance to the gateway wire contract |
| `watch-for-risk` | Overseer: risk management that raises the overall metric | overseer | Overseer digest evidence and signal correctness on labelled scenarios |
| `track-usage-and-cost` | Cost estimation, daily budget and host load guard | evaluation/components/track_usage_and_cost/impls/qar_usage.py | Cost, budget and resource-guard correctness |
| `keep-it-generic` | Evolve by metric | structure-library | Parts selected by measurement, Intents whose solutions one paragraph and one diagram explain as the code builds them, Hub of hubs: coverage, integration by configuration, research freshness, code structure |
| `know-the-world` | Science: evidence-weighted knowledge about the world | web_with_support_check | Supported and calibrated answers to world questions |
| `do-grounded-work` | Grounded goal agent: the engine that answers and works on a person's goals from their own material, and the executor that runs their queued AI tasks | library | QAR overall: do-grounded-work's sub-metrics weighted by its ideals, with floor and ceiling, Passing chat turns per dollar, Delegated tasks meeting their standard, Exact and consented changes, Data kept in scope, Engine reuse, Measured coverage |
| `run-queued-tasks` | Queued-task runner lane | qar_runner | Task lifecycle correctness over offline cases |
| `reason-and-answer` | Mind: one goal-focused working stream per request | rebuilt_per_step | Step continuity at cost over multi-step requests |
| `act-as-the-body` | Body: the host's data model as the agent's own consistent state | card_and_reads | Account-state consistency across scripted sessions |
| `hold-the-intents` | Higher mind: an AI representative's standing view across its assigned quests, placed in the intent hierarchy | separate_stores | Representative long-view carry-through across assigned quests, with nothing outside its branch |
| `think-critically` | Critical thinking: the enforcer of ESOI metrics on every request | deep_runs_only | Requests held to their ESOI metric by code |
| `access-quest-operations` | access-quest-operations | quest_client | none yet |
| `take-in-the-world` | take-in-the-world: the ways a request reaches QAR | ways_in | Ways in that pass their own metric |
| `remember-and-recall` | Subconscious: the person's own knowledge, admitted to the working stream by the goal | recency_window | Goal-relevant admission at a precision floor with nothing out of scope |
| `justify-model-calls` | Justified model calls | spec_registry | Justified share of model spend |

### Implementations

| Implementation | Name | Where | Sub-intent |
|---|---|---|---|
| `do-grounded-work:per-consumer-worker` | Per-consumer worker |  |  |
| `do-grounded-work:agent-framework-with-quest-glue` | Agent framework with Quest glue |  |  |
| `do-grounded-work:deep-runner-only` | Deep runner only |  |  |
| `call-a-model:single_provider` | Single flat provider floor | [evaluation/components/model_providers/impls/single_provider.py](../evaluation/components/model_providers/impls/single_provider.py) |  |
| `talk-over-a-channel:openclaw_bridge` | Fake-gateway conformance arm | [evaluation/components/talk_over_a_channel/impls/openclaw_bridge.py](../evaluation/components/talk_over_a_channel/impls/openclaw_bridge.py) |  |
| `watch-for-risk:digest_probe` | No-judgement digest probe | [evaluation/components/overseer_signals/impls/digest_probe.py](../evaluation/components/overseer_signals/impls/digest_probe.py) |  |
| `watch-for-risk:live_model` | Live overseer model arm | [evaluation/components/overseer_signals/impls/live_model.py](../evaluation/components/overseer_signals/impls/live_model.py) |  |
| `track-usage-and-cost:qar_usage` | Live usage evaluation arm | [evaluation/components/track_usage_and_cost/impls/qar_usage.py](../evaluation/components/track_usage_and_cost/impls/qar_usage.py) |  |
| `reason-and-answer:append_only_stream` | Carried message stream per request (proposed) |  |  |
| `reason-and-answer:stream_with_compaction` | Carried stream with compaction (proposed) |  |  |
| `remember-and-recall:goal_judge` | Small-model judge of every candidate (proposed) |  |  |
| `remember-and-recall:score_thresholds` | Score thresholds per source (proposed) |  | [select_context](select_context/README.md) |
| `remember-and-recall:thresholds_then_judge` | Thresholds with a judge on the borderline (proposed) |  |  |
| `know-the-world:research_sources_first` | Host research sources first, then the web (proposed) |  |  |
| `know-the-world:evidence_weighted` | Findings weighed by a host's evidence-estimation service (proposed) |  |  |
| `act-as-the-body:read_after_write` | Read back what a change touched (proposed) |  |  |
| `act-as-the-body:live_read_only` | A live read behind every state claim (proposed) |  |  |
| `hold-the-intents:per_quest_note` | Maintained view per quest only (proposed) |  |  |
| `hold-the-intents:periodic_reflection` | Periodic reflection on the best tier (proposed) |  |  |
| `remember-and-recall:outcome_precedents` | Intuition: outcome precedents surfaced at admission (proposed) |  |  |
| `watch-for-risk:fact_questions` | Overseer that asks questions of fact (proposed) |  |  |
| `think-critically:every_request_measured` | Every request measured by code (proposed) |  |  |
| `think-critically:measured_proposed_and_answered` | Measured, proposed and answered (proposed) |  |  |
| `watch-for-risk:per_risk_response` | Per-risk response chosen by metric (proposed) |  |  |
| `hold-the-intents:per_intent_cascade` | One view per intent, cascading down (proposed) |  |  |
| `hold-the-intents:per_rep_assigned_view` | One view per representative across its assigned quests, placed in the hierarchy (proposed, recommended) |  |  |
| `justify-model-calls:spec_registry_ablation` | Registry plus per-call ablation against the owner metric (proposed) |  |  |
| `keep-it-generic:registry_scan` | Registry read together with the code (today) | [evaluation/components/hub_of_hubs/impls/registry_scan.py](../evaluation/components/hub_of_hubs/impls/registry_scan.py) |  |
| `keep-it-generic:structure_library` |  | [evaluation/components/evolve_by_metric/impls/structure_library.py](../evaluation/components/evolve_by_metric/impls/structure_library.py) |  |
| `keep-it-generic:keep_current` |  | [evaluation/components/evolve_by_metric/impls/keep_current.py](../evaluation/components/evolve_by_metric/impls/keep_current.py) |  |
| `know-the-world:memory_only` |  | [evaluation/components/know_the_world/impls/memory_only.py](../evaluation/components/know_the_world/impls/memory_only.py) |  |
| `do-grounded-work:library` | quest-ai-runner library | [quest_ai_runner/goal_agent.py](../quest_ai_runner/goal_agent.py) |  |
| `call-a-model:multi_provider` | Prefix-routing multi-provider | [quest_ai_runner/providers.py](../quest_ai_runner/providers.py) |  |
| `watch-for-risk:overseer` | Digest-driven overseer | [quest_ai_runner/watch_for_risk/area.py](../quest_ai_runner/watch_for_risk/area.py) | [watch_for_risk](watch_for_risk/README.md) |
| `run-queued-tasks:qar_runner` | Offline lifecycle harness | [quest_ai_runner/area_act_as_the_body.py](../quest_ai_runner/area_act_as_the_body.py) |  |
| `track-usage-and-cost:pricing` | Static price table lookup | [quest_ai_runner/pricing.py](../quest_ai_runner/pricing.py) |  |
| `track-usage-and-cost:usage` | Persisted daily token tracker | [quest_ai_runner/usage.py](../quest_ai_runner/usage.py) |  |
| `track-usage-and-cost:resources` | Hysteresis resource guard | [quest_ai_runner/resources.py](../quest_ai_runner/resources.py) |  |
| `keep-it-generic:structure-library` | Apply the evolvable-structure library | [quest_ai_runner/keep_it_generic/area.py](../quest_ai_runner/keep_it_generic/area.py) | [keep_it_generic](keep_it_generic/README.md) |
| `reason-and-answer:rebuilt_per_step` | Rebuilt prompt per step with a referenced context (today) | [quest_ai_runner/area_reason_and_answer.py](../quest_ai_runner/area_reason_and_answer.py) | [reason_and_answer](reason_and_answer/README.md) |
| `know-the-world:web_with_support_check` | Web research with the answer's support check (today) | [quest_ai_runner/know_the_world/area.py](../quest_ai_runner/know_the_world/area.py) | [know_the_world](know_the_world/README.md) |
| `act-as-the-body:card_and_reads` | Managed quest card plus optional live reads (today) | [quest_ai_runner/area_act_as_the_body.py](../quest_ai_runner/area_act_as_the_body.py) | [act_as_the_body](act_as_the_body/README.md) |
| `hold-the-intents:separate_stores` | Separate stores read at the start of a request (today) | [quest_ai_runner/hold_the_intents/area.py](../quest_ai_runner/hold_the_intents/area.py) | [hold_the_intents](hold_the_intents/README.md) |
| `think-critically:deep_runs_only` | Code-enforced before and after on deep runs only (today) | [quest_ai_runner/think_critically/area.py](../quest_ai_runner/think_critically/area.py) | [think_critically](think_critically/README.md) |
| `take-in-the-world:ways_in` | Channels, terminal and package as ways in | [quest_ai_runner/area_take_in_the_world.py](../quest_ai_runner/area_take_in_the_world.py) | [take_in_the_world](take_in_the_world/README.md) |
| `access-quest-operations:quest_client` | Quest HTTP client from the environment | [quest_ai_runner/area_act_as_the_body.py](../quest_ai_runner/area_act_as_the_body.py) |  |
| `call-a-model:claude_cli` | Keyless command-line provider | [quest_ai_runner/adapters/claude_cli_provider.py](../quest_ai_runner/adapters/claude_cli_provider.py) |  |
| `talk-over-a-channel:openclaw` | MCP gateway bridge transport | [quest_ai_runner/adapters/openclaw_channel.py](../quest_ai_runner/adapters/openclaw_channel.py) |  |
| `reason-and-answer:rebuilt_full_context` | Rebuilt prompt per step with the context re-sent from cache | [quest_ai_runner/core/orchestrator_planning.py](../quest_ai_runner/core/orchestrator_planning.py) |  |
| `remember-and-recall:recency_window` | Start-of-run card filter and a recency window (today) | [quest_ai_runner/core/planner_context.py](../quest_ai_runner/core/planner_context.py) | [remember_and_recall](remember_and_recall/README.md) |
| `watch-for-risk:no_overseer` | No overseer (configuration off) | [quest_ai_runner/core/orchestrator_types.py](../quest_ai_runner/core/orchestrator_types.py) |  |
| `think-critically:instruction_only` | Standing instruction alone (floor) | [quest_ai_runner/core/standing_instructions.py](../quest_ai_runner/core/standing_instructions.py) |  |
| `justify-model-calls:spec_registry` | Registered prompt specs with an enforcement test (today) | [quest_ai_runner/core/prompt_specs.py](../quest_ai_runner/core/prompt_specs.py) |  |
| `run-queued-tasks:poller` | Task poller and claimer | [quest_ai_runner/runner/poller.py](../quest_ai_runner/runner/poller.py) |  |
| `run-queued-tasks:executor` | Single-task executor | [quest_ai_runner/runner/executor.py](../quest_ai_runner/runner/executor.py) |  |
| `run-queued-tasks:personas` | Configured persona resolver | [quest_ai_runner/runner/personas.py](../quest_ai_runner/runner/personas.py) |  |
| `run-queued-tasks:quest_folder_sync` | Work-folder sync | [quest_ai_runner/runner/quest_folder_sync.py](../quest_ai_runner/runner/quest_folder_sync.py) |  |
| `run-queued-tasks:lane` | Shared lane entry point | [quest_ai_runner/runner/lane.py](../quest_ai_runner/runner/lane.py) |  |

### Metrics

- `metric-call-a-model`, Provider routing, fallback, retry and cost correctness: `esoi run model_providers --impl multi_provider`
- `metric-talk-over-a-channel`, Channel transport conformance to the gateway wire contract: `esoi run talk_over_a_channel --impl openclaw_bridge`
- `metric-watch-a-run`, Overseer digest evidence and signal correctness on labelled scenarios: `esoi run overseer_signals --impl live_model; esoi run overseer_signals --impl digest_probe`
- `metric-track-usage-and-cost`, Cost, budget and resource-guard correctness: `esoi run track_usage_and_cost --impl qar_usage`
- `QME7`, Parts selected by measurement: `esoi run evolve_by_metric --impl structure_library`
- `metric-know-the-world`, Supported and calibrated answers to world questions: `esoi run know_the_world --impl web_with_support_check`
- `metric-hub-of-hubs`, Hub of hubs: coverage, integration by configuration, research freshness, code structure: `esoi run hub_of_hubs --impl registry_scan`
- `QME9`, Hub of hubs: `esoi run hub_of_hubs --impl registry_scan`
- `metric-metric-earns-its-run`, : `esoi run evolve_by_metric --impl structure_library`
- `metric-solution-coherence`, Intents whose solutions one paragraph and one diagram explain as the code builds them: `python3 -m esoi.coherence --gate`
- `QME1`, Passing chat turns per dollar: `esoi run chat_quality --impl dev_quest_chat --sample 10 --max-calls 300`
- `QME2`, Delegated tasks meeting their standard: `none`
- `QME3`, Exact and consented changes: `none`
- `QME4`, Data kept in scope: `none`
- `QME5`, Engine reuse: `none`
- `QME6`, Measured coverage: `none`
- `metric-qar-overall`, QAR overall: do-grounded-work's sub-metrics weighted by its ideals, with floor and ceiling: `esoi run qar_overall --impl recorded_rows`
- `metric-run-queued-tasks`, Task lifecycle correctness over offline cases: `esoi run run_queued_tasks --impl qar_runner`
- `metric-keep-a-working-stream`, Step continuity at cost over multi-step requests: `esoi run keep_a_working_stream --impl rebuilt_per_step --sample 3 --max-calls 40`
- `metric-act-as-the-body`, Account-state consistency across scripted sessions: `esoi run act_as_the_body --impl card_and_reads`
- `metric-hold-the-long-view`, Representative long-view carry-through across assigned quests, with nothing outside its branch: `esoi run hold_the_long_view --impl separate_stores`
- `metric-think-critically`, Requests held to their ESOI metric by code: `esoi run think_critically --impl deep_runs_only`
- `metric-quest-operations-parity`, : `none`
- `metric-take-in-the-world`, Ways in that pass their own metric: `none`
- `metric-admit-to-the-mind`, Goal-relevant admission at a precision floor with nothing out of scope: `esoi run admit_to_the_mind --impl recency_window`
- `metric-justified-model-spend`, Justified share of model spend: `esoi run justified_model_spend --impl spec_registry_ablation`
- `QME8`, Justified model spend: `esoi run justified_model_spend --impl spec_registry_ablation`
