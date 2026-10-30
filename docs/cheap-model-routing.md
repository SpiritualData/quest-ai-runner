# Running routing reliably on a cheap model
> Update 2026-10-29: the Gemini figures below are the history of this work. Measured runs now use Anthropic `claude-haiku-5-5` for every tier ($0.10 in, $0.50 out per million tokens up to 100,000-token prompts; https://platform.claude.com/docs/en/about-claude/pricing, read 2026-10-29). Gemini stays only as a named candidate arm.

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

## Queued tasks can skip the planner (`QAR_TASK_DIRECT_DEEP`)

A queued task runs through `Orchestrator.run` in background mode, so before its deep worker starts
it pays for a planner call (the largest prompt of the run) and a turn-start context search. With
`QAR_TASK_DIRECT_DEEP=1` the task executor passes the task's own text as `deep_goal`, the run goes
straight to a deep worker, and the goal check still judges the result. An offline count of one task
(`tests/test_task_direct_deep.py` shows the shape) goes from planner + verifier to verifier only.

It is off by default because a task that is only a question then costs a deep run instead of a cheap
answer. Turn it on where tasks are work orders and deep runs are free (a Claude subscription lane).
Unmeasured: how often a real task would have been answered without a deep run, and whether the
deep worker routes as well as the planner did.

## Measure before you believe a difference

The noise floor is large and easy to mistake for a result. Running one configuration three times
over 86 cases, 14 of them changed their answer, and one scenario-group of 19 rows scored anywhere
from 11 to 26 percent across single runs of the SAME configuration. Compare on the whole split, use
`--repeat` to see which cases are unstable, and treat a per-group swing on twenty rows as nothing.

### Declared tasks skip the planner (`task_route_declared`, on by default)

A task that carries an `env_id` (the person chose an environment) or `work_types` (any mix of
`web_search`, `quest_ops`, `quest_docs_ops`) skips the planner even when `QAR_TASK_DIRECT_DEEP` is
off, and the work types are named in the deep goal. Tested offline in `tests/test_task_direct_deep.py`.
The backend does not send `work_types` yet; until it does only `env_id` triggers this.


## The situational profile (built 2026-10-29, not yet measured on routing)

`planner_prompt_profile = "situational"` assembles the planner prompt per turn from only the rules that apply
(`PLANNER_RULES` in `core/planner_prompt.py`, the situation in `core/planner_doctrine.py`, the generic assembler in the hub's
`hubofhubs.doctrine`). Each rule is a section of the full prompt, wording unchanged, plus a condition over structured facts:
`mode` (normal, brainstorm, hold_off), `deep_available`, `run_in_progress`, `reach` (the reach judge's verdict),
`tools_offered`, `asks_change` and `has_material`. `asks_change` is the reach judge's boolean on the person's own message
(the same one small call, run before the first plan) or the planner's `user_intent` "act" from an earlier step; either one
saying "change" keeps the rules, and a judge that is off, fails or times out leaves it unknown. `run_in_progress` comes from
`context_meta["runs_in_flight"]`, a key the consumer supplies (QAR never sets it, like `tool_pins` or `scope_tags`): the
number of runs of this conversation in flight, 0 for none. Until a consumer sends it the fact is unknown and `work-in-progress`
stays in every turn. A fact the turn does not know is
None and keeps the rule, so a rule leaves only on a positive reason, and nothing is gated on words the model wrote. The
default stays "full", which is every rule in order and byte-identical to the old prompt.

| rule | tokens | in the prompt when |
|---|---|---|
| role, decision-order (rubric, user_intent, answer what you hold), read-real-content, sufficiency, read-action, answer-action, model-tier-field, parallel-subquestions, tail-frame | 4,606 | always |
| boundary-examples | 312 | deep runner not known absent, normal mode |
| command-vs-question | 932 | deep runner not known absent, normal mode |
| work-in-progress | 284 | a run is in flight or unknown |
| code-file-commands | 464 | normal mode, change not ruled out |
| execute-dont-describe | 343 | normal mode, change not ruled out |
| reach-of-a-read | 836 | normal mode, reach "outside" or unknown |
| specificity | 587 | there is context or a read to ground an answer in |
| cached-hint | 164 | same |
| can-it-be-done | 155 | normal mode, change not ruled out |
| deep-action | 1,046 | normal mode, change not ruled out |
| confirm-action | 225 | normal mode, change not ruled out |
| deep-difficulty | 154 | normal mode, change not ruled out |
| deep-fan-out | 84 | normal mode, change not ruled out |

Measured offline (`esoi run prompt_size --impl situational`, zero model calls, 3.5 characters per token, planner prompt with
the tools block and the frame, without the decide schema): a brainstorm or hold-off turn 40 percent smaller than the full
profile, a question after the first step with the reach judge saying "inside" 32 percent smaller, a cold first step 0 percent
(it knows nothing yet). The 54 classification cases, each with the facts its label implies, keep every rule their expected
route depends on (`ROUTE_RULES` in `evaluation/components/prompt_size/score.py`) and are 11 percent smaller on average. Whether
the smaller prompts route as well is the paid comparison of full, compact and situational, not yet run. A first step with the judge
saying no change was asked drops 7 rules (a question about the person's own records: 10,404 tokens down to 7,893, 24 percent, and 30 percent under the full profile's 11,240). The
judge's `asks_change` accuracy is measured in the paid pass, not yet run.

## Structural fixes for recorded chat misses (2026-10-30, built, not yet measured)

Six misses of the 2026-10-30 `chat_quality` sample (4 of 10) were read against their traces and fixed by changing what the model
is shown or what the planner's schema makes it fill in, not by more prose. None adds a model call; none is measured yet.

| miss | cause | change |
|---|---|---|
| a figure in a stored list never found | the compact profile never said grep reaches files only (three greps of documents for a figure held in records), and a read the adapter clipped looked like a complete one | the compact actions block says where records are read; a read ending in the adapter's `[truncated]` marker gets a "this read was cut, absence proves nothing" note in the planner and answer views |
| a booking or news reaction answered from goals alone | the prompt's "read the records first" exception is acknowledged and skipped by a cheap planner | the reach judge also returns `needs_records` (a boolean on the person's own message); an inside verdict that sets it stamps one settled line into the planner prompt |
| a write ignored a constraint the planner had read | the brief said only what the planner chose to restate | a required `facts_read` list (quotes with sources); appended to a deep or deferred brief, and shown to the answer |
| a parked write lost the findings the person asked to be told | the parked-only branch replaced the reply with the approval card | the answer written from this turn's reads stays in front of the ask (only when something was read) |
| a requested read-back not reported | the post-work synthesis took request, proposal and result only | it covers each named part and is given the planner's done-standard (this covers deferred runs; the Quest chat's direct-deep reply is written in the backend) |
| another quest's private data used to answer | the card fence does not cover the model's own reads | a turn bound to quest ids gets one scope note in the context both the planner and the answer read; an unbound turn gets nothing |

Planner prompt tokens (`estimate_tokens`): full 10,181 to 10,149 and compact 3,858 to 3,825 for the body; the decide schema
gains one bare field (full 1,680 to 1,702, compact 852 to 874), so body plus schema is 11,861 to 11,851 (full) and 4,710 to 4,699 (compact).


## Planner prompt design notes (moved from comments in `core/planner_prompt.py`)

```
THE PLANNER PROMPT: the single tunable that governs how the brain decides.
Rendered with .format(...). Generic: no org names, no app names.

Built at module load from three named parts so the doctrine gates from context_doctrine
(SUFFICIENCY_GATE, MODEL_TIER_GATE) can be woven in WITHOUT brace-escaping issues.
Those constants contain NO literal {/} characters, so they pass through .format() untouched
when the final assembled string is .format()-ed in _plan(). Only the real format slots
({user_message}, {transcript}, {context_view}, {gathered}, {max_reads}, {max_subq},
{max_deep}) are substituted; JSON-example braces use the standard {{...}} double-brace form.
```

```
The ORDERED DECISION PROCEDURE, and the first thing the planner reads.

Why it exists (measured 2026-10-05): the doctrine below it is correct but it is long, and the
order a reader meets it in is the order a cheap model applies it in. "CORE PRINCIPLE -- READ REAL
CONTENT BEFORE ANSWERING" sat ahead of "REACH OF A READ", so on an eval of 765 labelled messages
a small model issued a read for work no read could ever reach on 37 of 76 hand-off cases, and
when nothing attached could reach the work it handed it off anyway instead of saying so. The
rules were all present; their PRIORITY was not. Priority is what this block states, in the
cheapest form a model can hold: stop at the first rule that applies.

Keep it SHORT and keep it FIRST. Everything under it refines these rules, it never reorders them.
```

```
THE COMPACT PLANNER PROFILE

Input tokens are what a routing decision costs: a planner call carries thousands of them and
returns about a hundred. Measured on the full prompt above (cl100k, 2026-10-05): the head is
3,475 tokens and the actions block 2,289, which together are more than half of one call, while
the grounding data is 1,772 and the request itself is 12.

This profile is the same doctrine with the parts a PLANNER does not use removed. What goes:
  * the long prose behind the ordered rubric, which now states the same priorities in 411 tokens;
  * SPECIFICITY_GATE and CACHED_HINT_GATE, which govern how an ANSWER is grounded. The planner
    does not write the answer, and the answer path enforces both again on its own
    (``_grounding_block``, ``grounding_answer_tail``), so a planner pays for them twice;
  * SUFFICIENCY_GATE's checklist, folded into one line of the read action.
What stays, in full: the ordered rubric, the boundary examples, the read grammar (the planner has
to emit valid reads), the goal-versus-brief distinction, and the model-tier gate.

It is OPT IN (``OrchestratorConfig.planner_prompt_profile``, default "full"), so an existing
deployment is byte-for-byte unchanged until its operator asks for it.
```

```
THE RUBRIC GOES FIRST, and that was measured, not assumed. This repo learned the opposite for
the TOOLS block (CHANGELOG 2026-09-27: above the planner body, a live Gemini planner filled
tool_calls correctly and still chose action "answer"; moving it last fixed every probe), so the
same placement was tried here. It is worse: on the 376 scored rows of the dev half,
gemini-2.5-flash-lite scored 86.4 percent with the rubric first and 84.6 percent with it last
(2026-10-05). The lesson does not transfer, because a tool block is an OPTION the model has to
notice while deciding, and a rubric is the FRAME it decides inside.
```

```
Folded into the grounding of EVERY reply a latched turn produces, whatever terminal path it took
(zero extra LLM calls). It carries two things:

  1. The HONESTY FLOOR (a hard rule): a held turn ran nothing, so no reply of it may say or imply
     that it did, or that execution is imminent. A real held reply once said "The system will now
     execute this action to add it directly to your plan" while nothing ran: the worst possible
     failure, and the reason this rides on EVERY latched reply.
  2. The NO-ACTION ACKNOWLEDGMENT (guidance, with explicit permission to skip): if the message
     asked for something, name the hold out loud so the user knows why nothing happened and how to
     lift it. This is deliberately NOT gated on an intent detector. A real run proved why: the
     cheap regex prefilter does not see "Set that up.", "Book it." or "Do the thing we discussed"
     as directives at all, so gating on it left exactly the turns that most needed the
     acknowledgment without it. The ANSWERING model reads the message anyway; it is the better
     judge of whether the message asked for something, and the note tells it to skip the
     acknowledgment when the user was plainly just thinking out loud.

No em dashes: this steers user-facing text.
```

