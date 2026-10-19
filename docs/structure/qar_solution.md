# QAR's top intent: why quest-ai-runner exists

Draft for Joshua's review, 2026-10-17 (restated for the intent layout 2026-10-18). This is the top intent of quest-ai-runner (`intent.csv`), the intent as a whole: why it
exists, what it should become, what it is defined to do, the alternatives to it, and how to tell
whether it succeeds. Everything practical in the repository (each solution in `solutions.csv`, each
module) implements a part of this. The general method and utilities for applying evolvable structure
come from the separate evolvable-structure library; this document is QAR's own application of it.

Every field is a field of the intent template in the evolvable-structure skill's `SKILL.md`.
Metric has only a name until the template defines its other fields. The other problems and ideals
in `problems.csv` and `ideals.csv` are aspects of the ones here; each names the one it elaborates
(`parent_problem_ids`, `parent_ideal_ids`) in the table at the end.

## Problems

### QP1 AI work that people hand off for their goals does not get done
- **What is the problem?** People can attach AI tasks to their goals in Quest and schedule them, but nothing runs them: Quest queues the work and has no worker. The same gap exists for anyone who wants AI to keep working on their goals between conversations.
- **Who or what type of person has the problem?** Quest users and teams who delegate research, drafting, checking and follow-up to AI, and the operators who would otherwise run those tasks by hand.
- **Why does this problem matter?** Delegated work that never happens stalls the goals it was meant to move, and people stop trusting the app to act on their behalf.
- **Why is the problem hard to resolve?** A useful worker has to understand each task in the context of its goal, use the person's own material, decide when to answer and when to do real work, stop at the right point, and report back, all safely and at a bearable cost.
- **What solutions or workarounds already attempt to address the problem?** Doing the task by hand in a general chat assistant; scheduled scripts; each organization writing its own private worker (four separate copies of the same lane driver were written before QAR held one).

### QP2 AI help is generic because it does not know the person's goals and material
- **What is the problem?** A general assistant answers from general knowledge. It does not know the person's quests, plans, documents, past conversations or preferences, so its help is plausible but not theirs, and sometimes states things about their account that are not true.
- **Who or what type of person has the problem?** Anyone asking AI about their own goals and work: Quest users in chat, teams with shared quests, people directing AI on a codebase or a corpus.
- **Why does this problem matter?** Generic help has to be checked and corrected, which costs the time it was meant to save, and confident but ungrounded statements erode trust.
- **Why is the problem hard to resolve?** The relevant material is spread across many sources, far larger than any prompt, changes constantly, differs per person and team, and must never leak from one scope to another.
- **What solutions or workarounds already attempt to address the problem?** Pasting context into a chat; custom assistants with uploaded files; retrieval products over a single document store; memory features in chat assistants.

### QP3 Autonomous AI either stalls on approval or acts alone when it should not
- **What is the problem?** An AI that may act on someone's behalf tends to one of two failures: it asks permission for everything, so nothing moves without a person, or it takes steps that only a person should decide (spending, sending, deleting, anything irreversible).
- **Who or what type of person has the problem?** People and teams who delegate to AI, and the person accountable for what the AI does.
- **Why does this problem matter?** Over-asking wastes the delegation; acting alone on the wrong things causes harm that cannot be undone.
- **Why is the problem hard to resolve?** Which steps belong to a person depends on the action, the person, the organization and the moment, and the model's own wording cannot be trusted as the gate.
- **What solutions or workarounds already attempt to address the problem?** Approve-every-step agent interfaces; fully manual workflows; fixed allow and deny lists of actions.

### QP4 Every team building AI help rebuilds the same plumbing privately
- **What is the problem?** Grounding, routing, deep work, escalation, a task executor, cost control: each product or team that wants AI help writes these again, privately, and the copies drift apart.
- **Who or what type of person has the problem?** Developers of Quest and of any other product that wants goal-grounded AI help, including teams outside the organization that built QAR.
- **Why does this problem matter?** Effort is spent many times over, improvements made in one copy never reach the others, and chat and scheduled tasks answered by different engines behave differently.
- **Why is the problem hard to resolve?** Each consumer has its own data, storage, people and rules, so shared code must stay ignorant of all of them while still being useful.
- **What solutions or workarounds already attempt to address the problem?** General agent frameworks (LangGraph, CrewAI, the OpenAI and Claude agent SDKs), which give building blocks but not a grounded, goal-aware engine with escalation and a task executor; copying code between repositories.

### QP5 What is best keeps changing, and fixed designs fall behind
- **What is the problem?** Models, prices, tools and techniques change every few months, so a router, a retrieval method or a model choice that was right becomes wrong, and nobody notices until quality drops or cost rises.
- **Who or what type of person has the problem?** The maintainers and operators of QAR, and Joshua steering its quality.
- **Why does this problem matter?** An engine that cannot follow the state of the art either degrades or needs rewrites, and quality decisions get made by habit.
- **Why is the problem hard to resolve?** Comparing alternatives needs good cases, honest measurement and a way to switch, and language model output is noisy and costly to measure.
- **What solutions or workarounds already attempt to address the problem?** Periodic rewrites; following public benchmarks; ad hoc evaluation scripts.

### QP6 Cost and quality of AI work are invisible until something goes wrong
- **What is the problem?** Operators learn what AI work cost, and whether it was any good, only after the fact: a runaway run, a large model on an easy question, or a quiet drop in answer quality.
- **Who or what type of person has the problem?** Operators paying for AI work, and people relying on its output.
- **Why does this problem matter?** Spend surprises and silent quality loss both end the willingness to delegate.
- **Why is the problem hard to resolve?** Quality is hard to measure automatically, cost depends on caching and vendor pricing, and both vary per task.
- **What solutions or workarounds already attempt to address the problem?** Vendor billing dashboards, spend caps, spot checks by hand.

## Ideals

### QI1 Self-advancing goals
- **Name:** Self-advancing goals
- **Principle:** Goals keep moving between a person's sessions, because the work they delegated gets done to the standard they wrote.
- **State Description:** You schedule an AI task the way you schedule a goal, and at that time it happens: grounded, finished to its written standard, reported against the goal, with your attention asked for only where it is needed.
- **Hints of Measurement:** Delegated tasks finish to their written standard without rework.
- **Opposes:** QP1

### QI2 Personally grounded help
- **Name:** Personally grounded help
- **Principle:** Every answer and action rests on the person's own goals, material and preferences, and never on another person's.
- **State Description:** Asking Quest AI feels like asking a colleague who has read everything relevant to you, says only what your material supports, and never mentions anyone else's.
- **Hints of Measurement:** Replies state the facts the account holds, state nothing it does not, and nothing crosses scope.
- **Opposes:** QP2

### QI3 Trusted autonomy
- **Name:** Trusted autonomy
- **Principle:** The AI acts first on what it may decide and brings the person exactly the decisions that are theirs.
- **State Description:** Work moves without you, and when a question reaches you it is one only you should answer, with what you need to answer it.
- **Hints of Measurement:** No human-only step is taken alone, and few asks turn out to be unnecessary.
- **Opposes:** QP3

### QI4 One shared engine
- **Name:** One shared engine
- **Principle:** One domain-free engine serves every consumer, every lane and both chat and scheduled work, and differs only in configuration.
- **State Description:** A new product or team gets grounded, escalating AI help by supplying configuration and adapters, and every improvement reaches everyone who uses it.
- **Hints of Measurement:** Consumers add little glue of their own and copy none of the engine.
- **Opposes:** QP4

### QI5 Metric-led evolution
- **Name:** Metric-led evolution
- **Principle:** Every part of QAR improves as the state of the art moves, with the best implementation chosen by its metric.
- **State Description:** When a better model, method or tool appears, it is measured against the current one on the same cases and becomes the default if it wins, without a rewrite.
- **Hints of Measurement:** Every part has a current measured result, and selections follow the measurements.
- **Opposes:** QP5

### QI6 Visible value
- **Name:** Visible value
- **Principle:** What AI work costs and how good it is are known before they surprise anyone.
- **State Description:** Operators see cost per result and quality per part as they go, budgets hold, and a drop in quality shows up in a metric before it shows up in a complaint.
- **Hints of Measurement:** Every run reports its cost, and every part has a quality metric with a recent result.
- **Opposes:** QP6

## Solution

### QS1 Grounded goal agent
- **Name:** Grounded goal agent: the engine that answers and works on a person's goals from their own material, and the executor that runs their queued AI tasks
- **Scoped Purpose:** Use for answering chat turns about a person's or team's goals, carrying out requested changes, running queued and recurring AI tasks to a written standard, doing long autonomous work through a deep runner, and escalating decisions to the right person. Intentionally avoids: being a chat interface (consumers render it), owning goal data or storage (the consumer's adapters do), holding any one organization's names, rules or data, being a hosted service, and doing work a person should decide.
- **Goals:** Chat turns that pass their rubric (grounded facts, right handling, exact changes) at a cost per passing turn the operator accepts; queued tasks that meet their written goal; no unrequested writes and no human-only step taken alone; no data across scope; every part measured and selected by its metric.
- **Knowledge:** Agent loop research (reason and act, plan and execute, verification against a written goal); retrieval-augmented generation and context selection; evaluation of language-model systems; Quest's model of quests, goals, notes, asks and AI tasks; the operating principle "AI acts first, humans confirm"; Joshua's evolvable structure that optimizes intent.
- **Measures of Success:** People's goals move through delegated work; tasks and replies need no correction; operators accept the cost; Joshua can steer by reviewing metrics without needing to read code.
- **Parameters:** One configuration per lane: the consumer's adapters (retrieval, cards, conversations, escalation, deep runner), model tiers and providers, personas, budgets and schedules, and the implementation selected for each swappable solution.
- **Inputs:** A person's message or a queued task, the quest and goal it concerns, and the sources the adapters reach.
- **Outputs:** A reply, a task result reported against its goal, a decision-request for a person, or files and records changed as requested.
- **Effects (other than outputs):** Writes to Quest and other systems through their APIs and tools; model and search spend; context cards and conversation memory kept for later turns.
- **Other information on when and how to use the solution:** Two entry points share one brain: the importable core (`Orchestrator`) for chat and other hosts, and the runner (a lane that polls, claims, runs, escalates and reports) for queued work. A terminal chat drives the same brain. Every specific comes through configuration; nothing about a consumer is built in.
- **Addresses:** QP1, QP2, QP3, QP4, QP5, QP6
- **Pursues:** QI1, QI2, QI3, QI4, QI5, QI6

What the solution is made of. Each area groups solutions in `solutions.csv`; an area is not a
solution of its own unless its "best" differs from its parts.
1. **Understand the request:** route-a-message, resolve-references, receive-input-mid-run, recall-recent-context, remember-conversations.
2. **Ground in the person's material:** gather-evidence, search-the-web, select-context, persist-cards, index-by-meaning, detect-stale-cards, cache-cards, feed-context-sources, assemble-a-prompt, persist-predictions.
3. **Answer and act exactly:** answer-a-turn, operate-a-quest, write-files, write-through-mcp, replay-known-operations, guard-a-reply.
4. **Work autonomously to a standard:** run-deep-work, watch-a-run, detect-a-stalled-run, judge-a-decision.
5. **Keep people in the loop:** escalate-to-a-human, show-progress, talk-over-a-channel.
6. **Learn from people:** guide-by-feedback, store-feedback, learn-by-example, behave-by-esoi, judge-esoi-behavior.
7. **Run on schedule:** run-queued-tasks, gate-an-autopilot-pass, configure-a-lane.
8. **Spend proportionately:** call-a-model, choose-a-model-tier, track-usage-and-cost.
9. **Be adoptable and safe in the open:** expose-the-public-api, explain-qar, operate-from-a-terminal, keep-secrets-out, verify-offline.
10. **Evolve by metric:** evolve-by-metric (the evolvable structure QAR applies, a solution inside this intent whose implementation uses the `@esoi/...` solutions), select-an-implementation. The general machinery lives in the evolvable-structure library; QAR keeps its records and configuration.

## Implementations

### QM1 quest-ai-runner (selected)
- **Name:** quest-ai-runner library
- **Description:** A domain-free Python library: a bounded plan, gather, re-plan, answer loop that escalates to a deep runner when real work is needed (the core), a task executor that polls Quest, claims, runs, escalates and reports (the runner), and reference adapters for retrieval, model providers, cards, vectors, web research and deep runners, all wired by one configuration per lane and each swappable part chosen by metric.
- **Advantages:** One engine for chat and scheduled work; grounded in the consumer's sources; escalation built in; open source and reusable outside Quest; every part measured and swappable.
- **Disadvantages:** A large codebase to maintain; chat answer quality is currently weak on the cheapest model; some parts are measured only offline; consumers must keep adapters in step with the library.
- **Implements:** QS1

### QM2 A private worker per product
- **Name:** Per-consumer worker
- **Description:** Each product writes its own worker and chat backend around a model vendor's API.
- **Advantages:** Fits its one product exactly; no shared-library coordination.
- **Disadvantages:** The plumbing is rebuilt and drifts per product (the history that led to QAR); improvements do not travel; chat and tasks diverge.
- **Implements:** QS1

### QM3 A general agent framework plus glue
- **Name:** Agent framework with Quest glue
- **Description:** Build the engine from a general framework (LangGraph, CrewAI, an agent SDK) and add Quest grounding, escalation and the executor as glue.
- **Advantages:** Mature building blocks, community and tooling; less code of our own for the loop.
- **Disadvantages:** Grounding, scope containment, escalation, the executor and measured selection still have to be built; framework churn becomes a dependency risk.
- **Implements:** QS1

### QM4 A coding agent run per task
- **Name:** Deep runner only
- **Description:** Hand every task straight to a coding agent such as Claude Code with the person's material available, with no routing or answering loop in front.
- **Advantages:** Strong at real multi-step work; very little engine code.
- **Disadvantages:** Slow and costly for simple questions; weaker grounding in Quest's own state; escalation and scope still need building; one vendor dependency.
- **Implements:** QS1

## Metrics

### QME1 Passing chat turns per dollar
- **Name:** Passing chat turns per dollar: share of realistic chat turns that pass their rubric (grounded facts, right handling, exact changes), and cost per passing turn
- **Solution:** QS1 (measured through answer-a-turn)
- **Ideals:** QI2, QI6

### QME2 Delegated tasks meeting their standard
- **Name:** Delegated tasks meeting their standard: share of queued tasks that meet their written goal without human rework, and cost per task
- **Solution:** QS1 (measured through run-queued-tasks and run-deep-work)
- **Ideals:** QI1, QI6

### QME3 Exact and consented changes
- **Name:** Exact and consented changes: unrequested writes, missing requested writes, and human-only steps taken without a person
- **Solution:** QS1 (measured through operate-a-quest, escalate-to-a-human and answer-a-turn)
- **Ideals:** QI3

### QME4 Data kept in scope
- **Name:** Data kept in scope: items from another user, team or quest reaching a prompt or store
- **Solution:** QS1
- **Ideals:** QI2

### QME5 Engine reuse
- **Name:** Engine reuse: glue each consumer writes, and engine code copied into a consumer
- **Solution:** QS1 (measured through the consumer check)
- **Ideals:** QI4

### QME6 Measured coverage
- **Name:** Measured coverage: share of QAR's solutions with a current measured result whose selection follows it
- **Solution:** QS1 (measured through the structure check and the leaderboard)
- **Ideals:** QI5, QI6

## How the other records serve this

| Problem here | Detailed problems (problems.csv) |
|---|---|
| QP1 | prob-queued-task-falls-through, prob-autopilot-works-when-unwanted, prob-deep-work-wanders, prob-claimed-done-not-done |
| QP2 | prob-reply-misses-the-account, prob-follow-up-loses-thread, prob-context-drifts-from-its-source, prob-relevant-card-not-found, prob-prompt-overflow-and-duplication, prob-corrections-repeat, prob-private-data-crosses-scope, prob-web-answers-unsafe-or-costly |
| QP3 | prob-human-only-steps-slip, prob-unrequested-or-partial-changes, prob-progress-unreadable, prob-misrouted-message |
| QP4 | prob-integration-is-trial-and-error, prob-public-repo-leaks, prob-channel-messages-misdelivered |
| QP5 | prob-ai-skips-structure-thinking, prob-grader-fooled-by-vocabulary, prob-narrow-verdicts-unreliable, prob-vendor-outage-halts-work |
| QP6 | prob-spend-surprises, prob-big-model-for-easy-work, prob-same-ask-repeats-cost |

## Open questions for review

1. Is "Grounded goal agent" the right definition of QAR, or should QAR be narrower (only the executor and brain for Quest) or broader (a general grounded agent that Quest is one consumer of)?
2. Should the ten areas become solutions with their own metrics, or stay as groupings? They are groupings here because no area has a "best" that differs from its parts.
3. Are the top metrics the right authoritative ones? QME1 and QME2 are the two outcome measures; the rest guard against harm.
