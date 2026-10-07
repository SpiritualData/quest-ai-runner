# Running routing reliably on a cheap model

A planner call is almost entirely INPUT. It carries thousands of input tokens and returns about a
hundred, so what a routing decision costs is set by the prompt, not the answer. That makes two
goals one goal: give each decision less text and give it the RIGHT text.

This page is the measured account of how far that goes, what worked, and what did not. Everything
below was measured on one fixed set of 765 labelled messages, split in half by a stable hash of the
message and stratified by group, with all iteration on the dev half and one single run on the held
out half per recommended configuration. The harness lives in the consumer that owns the labels
(quest-backend's `scripts/checks/check_deep_run_routing.py`), because the labels are facts about a
product, not about this library.

## The result (settled 2026-10-06)

Re-measured after three fixes found by closing the first round's open items (the reach summary,
the compact hand-off field, and the judge's latency; all below). Every configuration was run once
on the held-out half, after all iteration on the dev half was finished. "Cheapest" is
gemini-2.5-flash-lite on the planner tiers and "stronger" is gemini-3.1-flash-lite; the reach judge
runs on the stronger model in both.

| configuration | TEST accuracy (Wilson 95%) | input tokens / request | cost / 1,000 requests | per-decision p50 / p95 |
|---|---|---|---|---|
| cheapest, old prompt | 85.6% [81.7, 88.8] | 12,349 | $1.270 | 0.6s / 1.4s |
| **cheapest, compact + judge** | **91.2% [87.9, 93.7]** | **7,495** | **$0.975** | 1.3s / 2.1s * |
| cheapest, full + judge | 91.2% [87.9, 93.7] | 13,483 | $1.573 | 1.3s / 2.3s * |
| stronger, old prompt | 90.4% [87.0, 93.0] | 12,588 | $3.309 | 1.0s / 2.1s |
| **stronger, compact + judge** | **98.1% [96.2, 99.1]** | **7,561** | **$2.036** | 1.5s / 2.4s * |

\* The harness calls the planner directly, so its per-decision time still includes the judge
serially. In a real turn the judge now overlaps context assembly (see below).

Paired on the same 375 cases: the cheapest model gains 5.6 points (bootstrap 95% CI +1.9 to +9.3,
McNemar p=0.004, 35 fixed and 14 broken), the stronger one 7.7 points (+5.1 to +10.7, p<0.001, 30
fixed and 1 broken). On the cheap model, compact and full score the same on TEST (32 discordant,
16 each way) at 56 percent of the input. The noise floor was measured on the same day: the dev
half run twice per configuration moved by 1.1 to 1.9 points between the two runs of the same
configuration, against a 6.1 point gain for compact + judge over the old prompt on dev (bootstrap
+2.1 to +10.1, McNemar p=0.003). Prices are list prices from secondary sources (gemini-3.1-flash-lite
$0.25 in / $1.50 out, gemini-2.5-flash-lite $0.10 / $0.40 per million), not verified with the
vendor, so read ratios.

**Routing accuracy is not end-to-end quality, and this is the result that matters most.** Six live
turns per configuration through Quest's real chat function on dev showed that the cheapest model
routes well and then does the REST of the turn badly: it re-issued the same read up to five times,
its code-generation path gave up after eight attempts on two ordinary questions ("what should I
focus on today", "what goals do I have this week"), and two replies stated the wrong date. That
happened with the full profile too, so it is the model, not the compact prompt. The stronger model
with compact + judge answered all six cleanly. So: compact + judge is ready on the stronger model,
and the cheapest model is ready for the ROUTING DECISION only. Putting the cheapest model on a
whole turn needs a multi-step eval (reads after the first step, the answer, code generation) that
this harness does not have.

### The first round (2026-10-05), kept for the record

One run on the held-out half per recommended configuration, after all iteration was finished.

| configuration | accuracy (Wilson 95%) | input tokens / request | cost / 1,000 requests |
|---|---|---|---|
| cheapest model, old prompt | 80.8% [76.5, 84.5] | 12,459 | $1.288 |
| stronger model, old prompt | 88.5% [84.9, 91.4] | 12,758 | $3.382 |
| **cheapest model, this work** | **91.2% [87.9, 93.7]** | **7,113** | **$0.908** |
| stronger model, this work | 97.1% [94.8, 98.4] | 7,268 | $1.959 |

Paired on the same cases, the cheap model gains 10.4 percentage points (bootstrap 95 percent CI
+6.4 to +14.4, McNemar p below 0.001), and the stronger model gains 8.5, so nothing was traded away
to make the cheap one work. Exactly one group of fifteen cases regresses for either model, by one
case more than its noise. Prices are the unverified list prices in the consumer's price table, so
read the ratio rather than the absolute figures.

The point worth taking: the cheap configuration is both more accurate AND 73 percent cheaper than
the stronger model was on the old prompt.

## Where the tokens go

Measured with a tokenizer, no model involved, on one real step-0 planner call (cl100k, which
undercounts what Gemini bills by roughly a quarter):

| Section | Tokens | Share |
|---|---|---|
| `_PLANNER_HEAD` (intent gate, reach doctrine) | 2,789 | 28% |
| `_PLANNER_ACTIONS` (read / answer / deep / confirm) | 2,289 | 23% |
| the grounding data the consumer supplies | 1,772 | 18% |
| the decide schema, rendered into the prompt | 1,897 | 19% |
| `SPECIFICITY_GATE` | 457 | 5% |
| tail scaffolding | 372 | 4% |
| `SUFFICIENCY_GATE` | 189 | 2% |
| `CACHED_HINT_GATE` | 135 | 1% |
| `MODEL_TIER_GATE` | 95 | 1% |
| the request itself | 12 | 0% |

Two things stand out. The request is a rounding error, so nothing about cost scales with what the
user asked. And half the prompt is doctrine, most of which a PLANNER does not use: `SPECIFICITY` and
the cached-hint rule govern how an ANSWER is grounded, and the answer path applies both again on its
own.

## What worked

**An ordered decision procedure, stated first.** `PLANNER_DECISION_RUBRIC` is six rules with a stop
at the first one that applies, in 411 tokens. The rules it states were all already in the prompt;
their PRIORITY was not, and the order a reader meets them in is the order a cheap model applies
them in. "Read real content before answering" sat ahead of "reads only reach the listed sources",
so a small model read first. The rubric also names the case the old doctrine had no branch for: a
question about the world right now is neither a read of your own sources nor work for a machine.

**The compact planner profile** (`OrchestratorConfig.planner_prompt_profile = "compact"`). The same
doctrine with the answer-grounding gates dropped and the actions block condensed, plus the decide
schema with its field descriptions stripped (`decide_tool_for(compact=True)`). The planner body
falls from 7,132 tokens to 2,053 and the schema from 1,897 to 583.

This one is a TRADE and the ablation says so plainly. Holding the rubric and the reach judge
constant on the dev half, the full profile scores 94.1 percent at 13,275 input tokens per request
and the compact profile 92.0 percent at 6,975. Half the tokens for two points. Both clear 90, so
take compact when cost is the constraint and full when it is not. The first version of this page
claimed the compact profile cost nothing, which was an inference from a bundled run, not a
measurement. Run the ablation before repeating a claim like that.

**The reach judge** (`planner_reach_judge`, `core/reach_judge.py`). One small question, answered by a
STRONGER tier before planning: does what this request needs live inside the readable sources,
outside them, or in the world right now. Roughly 400 input tokens. Its verdict is stamped into the
planner prompt as settled fact, and an "inside" verdict adds nothing at all. This is the single
largest accuracy win, and it needs one piece of consumer configuration (`read_reach_summary`),
because only the consumer knows what its own reads reach.

## What did not work, and is worth not repeating

**The rubric last.** This repo learned that the TOOLS block belongs after the planner body, because
a planner that met it first filled `tool_calls` correctly and still chose `answer`. The same
placement for the rubric is WORSE: 86.4 percent with it first against 84.6 percent with it last, on
376 scored rows. A tool block is an option the model has to notice while deciding; a rubric is the
frame it decides inside.

**More prohibition.** Strengthening the out-of-reach rule with a longer, firmer paragraph (do not
read, do not invent a scope, whatever the verb) did not help. The model acknowledged the rule in its
own rationale and issued the forbidden read anyway. Three rewrites all landed inside the noise
floor, which is how a wording problem is told apart from a capability problem.

**Self-reported confidence as an escalation signal.** With the field REQUIRED (optional, a planner
left it null on 381 of 384 decisions), a cheap planner answered "high" on 84 of 86 decisions,
including 36 it got wrong: 57 percent of its "high" decisions were correct. A model that cannot tell
a reachable request from an unreachable one cannot tell that it cannot tell.

**Reviewing the decision afterwards.** The cascade in `core/planner_cascade.py` works as specified
and still made things worse: escalating every `read` to a stronger model sent 44 percent of
decisions for review and scored 52 percent against 60 percent with no review at all, because a
reviewer handed a decision argues with it, while the thing actually missing was a fact. The code is
kept, off by default, documented here as measured-not-working on the models tried. Asking the
stronger model the reach question on its own, with no decision to defend, is 94.7 percent accurate on
the same material.

## Closed on 2026-10-06

- **The regressed group was the judge, not noise.** Both TEST runs lost two of fifteen AI-task
  questions. The cases were "did the weekly summary task run on schedule?", "cancel all my queued
  tasks" and "abort the overnight task": the judge read an AI task as a job on a machine and
  handed it off. The consumer's `read_reach_summary` had said only "tasks". It now names
  everything the product holds or runs for the person, says its AI tasks are records there
  wherever they execute, and carves out the product's own code and bugs. Judge alone, on 1,162
  labelled judgments: 94.4% to 99.2%, with the AI-task group 43/58 to 58/58 and scheduling 39/60 to
  58/60. On TEST the AI-task group no longer moves at all (0 discordant cases against the old
  prompt). The lesson is general: **the judge is only as good as the reach summary, and an
  incomplete list of what IS readable reads as "outside" for everything it leaves out.**
- **The compact schema had lost the hand-off.** Stripping every field description also stripped
  the one on `deferred_deep`, and a cheap planner then wrote "this needs the dev server" as its
  answer with the field empty, so nothing ran: 14 of 38 such decisions on dev. The compact schema
  now keeps that one description (`COMPACT_SCHEMA_KEPT_DESCRIPTIONS`) and the compact actions block
  says a hand-off is the field, not the words. Hand-off groups on dev: 24/38 to 32/38 with the
  environment attached, 14/38 to 36/38 after an empty read. A firmer sentence in the judge's
  verdict line was also tried and measured neutral (153 against 154 of 172), so it was reverted.
- **The judge no longer costs wall clock.** `run()` starts it at the top of the turn
  (`prefetch_reach_verdict`) so it overlaps request understanding, context assembly and guidance,
  and the first plan only collects it, bounded by `planner_reach_judge_timeout_seconds`. Live turns
  on dev (stronger model, compact + judge): the first planning step fell from 1.63s to 0.96s p50
  and 1.70s to 1.35s p95, which is the judge's ~0.7s leaving the critical path, and turn start to
  first decision fell from 4.74s to 4.19s p50.
- **Live turns ran**, six per configuration through the consumer's real chat function: see "Routing
  accuracy is not end-to-end quality" above.

## Haiku through the Claude subscription (claude_cli)

The same harness, with every planner and judge call answered by haiku through `ClaudeCliProvider`
(the way a subscription lane runs it), on a stratified sample of 100 dev cases (4 per group and
scenario). Haiku's input count is the planner prompt itself; the subscription bills no money, so
the cost is usage-limit headroom and wall clock.

| haiku configuration | accuracy (Wilson 95%) | input tokens / request | output tokens / decision | p50 / p95 per call |
|---|---|---|---|---|
| full, no judge, thinking on (CLI default) | 94.0% [87.5, 97.2] | 12,581 | ~4,040 | 36.9s / 94.6s |
| full, no judge, thinking off | 93.0% [86.3, 96.6] | 12,551 | ~164 | 3.3s / 5.7s |
| compact + judge (haiku), thinking off | 97.0% [91.5, 99.0] | 8,033 | ~91 | 2.4s / 5.2s |
| same, second run | 96.0% [90.2, 98.4] | 8,069 | ~90 | 2.4s / 5.0s |
| same, third run | 96.0% [90.2, 98.4] | 8,179 | ~90 | 2.6s / 5.2s |
| **compact, NO judge, thinking off** | **91.0% [83.8, 95.2]** | **6,432** | ~161 | 3.3s / 5.9s |

Thinking is where nearly all of a haiku routing decision goes, and it buys nothing measurable
(7 discordant cases, 4 against 3). `QAR_CLI_PLAN_THINKING_TOKENS=0` removes it from routing
decisions only. Compact + judge on haiku is +4 points over the full prompt (4 fixed, 0 broken,
McNemar p=0.125 at this sample size), so it is consistent with the Gemini result but not proven on
haiku alone. Haiku clears 90 percent on routing in every configuration measured. The judge here
was given the consumer's reach summary.

**Compact without the judge does not hold up on haiku** (one run, 2026-10-06, the same 100 cases,
so paired): 91.0% against 96 to 97% for compact + judge (6 broken, 0 fixed against the first judged
run, exact McNemar p=0.03), and 91.0% against 93.0% for the full prompt (3 broken, 1 fixed, p=0.63,
so no measurable difference from full). Every case it broke is a hand-off: "fix the
bug where the habit reminder fires twice" and "add a column to the spreadsheet" became a
`list_sources` or search read instead of deep work, which is exactly the decision the judge
exists for. It saves half the input (6.4k against 12.6k tokens), but the compact prompt leans on
the judge for reach, so take the two together or neither. A CLI lane stays on the full profile
until it supplies a reach summary (`QAR_READ_REACH_SUMMARY_FILE`, below) and compact + judge is
measured on that lane's own tasks.

A measurement trap found on the way: two earlier "no judge" runs had in fact run WITH the judge,
because a dependency of the consumer reloads its `.env` with override at import time, after the
shell's override was applied. Check the judge's call count in the run before believing a no-judge
number.

## Turning it on

For a consumer that embeds the orchestrator, set on `OrchestratorConfig`:

```
planner_prompt_profile = "compact"
planner_reach_judge = True
planner_reach_judge_tier = "<a tier one step above the planner, when you have one>"
```

plus a `read_reach_summary` that names EVERYTHING a read reaches. For the stock CLI, the same
switches are `QAR_PLANNER_PROMPT_PROFILE`, `QAR_PLANNER_REACH_JUDGE` and
`QAR_PLANNER_REACH_JUDGE_TIER`, and the reach summary comes from `QAR_READ_REACH_SUMMARY` (inline)
or `QAR_READ_REACH_SUMMARY_FILE`; without one the judge stays inert. On the claude_cli backend,
`QAR_CLI_PLAN_THINKING_TOKENS=0` is the latency lever.

To put the routing decision ALONE on a different model from everything else that shares the
planner tier (request understanding, card updates, summaries), set
`OrchestratorConfig.planner_model` (CLI: `QAR_PLANNER_MODEL`) to a model id. It is sent verbatim
on the decide call only; the reach judge keeps its own tier.

## Whole turns, not just the routing decision (2026-10-06, small sample)

The routing harness scores one decision. A turn is that decision plus every re-plan after a read,
the reads, any generated code and the answer. Measured on real chat turns, in-process on the dev
deployment: 10 read-only messages (the six that had failed live, plus one drawn at random from each
of four strata of the harness's conversation cases), each run under two configurations with
everything identical except the routing decision's model (`planner_model`): gemini-2.5-flash-lite
or gemini-3.1-flash-lite, compact + judge, the judge and every other call on 3.1. Replies graded by
haiku against three properties (answers the question or says plainly why not; no current date that
contradicts today; no talk of its own machinery). n = 10, so the intervals are wide and honest.

| configuration | replies passing (Wilson 95%) | planning steps, total (worst turn) | turn p50 |
|---|---|---|---|
| 2.5 routing, before | 5/10 [24, 76] | 40 (15) | 13.7s |
| 3.1 routing, before | 7/10 [40, 89] | 39 (15) | 15.2s |
| 2.5 routing, after the fixes below | 8/10 [49, 94] | 26 (5) | 9.4s |
| 3.1 routing, after | 8/10 [49, 94] | 41 (6) | 14.4s |

**The read loops were not the cheap model's alone.** Both models re-issued the identical read up
to fourteen times in one turn (3.1 on "list the tasks on this quest", 2.5 on "what should I focus
on today"). Two generic causes, both fixed in this library:

- Nothing stopped an identical read spec from running again. The run loop now skips a spec that
  already ran this turn (same keys and values, any order), tells the planner in a note that never
  reaches the answer, and ends the read loop through the read-budget wrap-up when a second step asks
  for nothing new. It is structural, on the planner's own specs, never on words it wrote.
- `CompositeRetrievalAdapter.query` dropped an adapter's refusal whenever another adapter returned
  anything. The planners were writing a code-path helper as a read query (`{"kind":
  "get_my_rundown"}`), the consumer's adapter refused it, a conversation-history adapter returned
  loose matches, and the planner saw only those and asked again. Refusals now travel with the
  results; the consumer's refusal now names the query shapes that do run, and after it the planner
  switched to a valid `{"operation": ...}` read in the live re-run.

**The wrong dates were not a grounding gap in routing.** The planner already receives today's date
in its prompt head. The one wrong "today" in this sample (September 30 for October 6) came from the
answer step after fourteen identical reads had filled the turn with old conversation records; it
did not appear in the after runs, which is one case, not a proof.

Read the "after" rows as one bundle: the read dedup, the composite refusal and the consumer's
clearer query refusal changed together, so the sample cannot say which one helped. On 3.1 the
worst turn fell from 15 planning steps to 6 but the total rose (39 to 41), so the gain there is
the loops, not fewer steps overall.

**Is the cheapest model ready to route a whole turn?** On this sample it ties (8/10 each), with
a lower turn p50 (9.4s against 14.4s) and about $0.019 of model spend per turn against $0.041 (list prices)
(3.1's figure includes one turn whose code path ran ten generations), but both of its remaining failures are its own: a general-knowledge
question it searched for three times and then deflected ("views versus impressions on YouTube"),
and a turn that ended "I couldn't start this work (empty inline answer)". Neither appeared with
3.1. So: compact + judge stays on the stronger model by default; the cheapest model on
`planner_model` is a candidate worth a larger paired sample, not a proven setting.

Not caused by routing and left for the consumer: a code path that is re-run after a correct answer
appends "I tried N versions of the code" or "I stopped after 8 attempts" to the reply (both models,
same code model), and one live turn's request-understanding step asked "where is the daily
reflection stored" instead of reading it.

## Still open

- **The cheapest model routing whole turns** needs a larger paired sample than ten (see "Whole
  turns" above); the small one ties but shows two failure kinds only it produced.
- **A CLI lane on compact + judge** needs its own reach summary and a measurement on its own tasks.
- Prices come from secondary sources, so read every cost figure as a ratio.

## Measure before you believe a difference

The noise floor is large and easy to mistake for a result. Running one configuration three times
over 86 cases, 14 of them changed their answer, and one scenario-group of 19 rows scored anywhere
from 11 to 26 percent across single runs of the SAME configuration. Compare on the whole split, use
`--repeat` to see which cases are unstable, and treat a per-group swing on twenty rows as nothing.
