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

## The result

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

## What is still unverified

- **Nothing has run a real turn.** Every number here comes from scoring two real decisions in a
  harness. The compact profile and the judge have never driven a live conversation end to end, so
  what the shorter actions block does to the QUALITY of the reads a planner emits, and to the reply
  that follows, is unmeasured.
- **Latency roughly doubles for the cheap model.** The judge is a serial call before planning, and
  per-decision p50 goes from 0.7s to 1.4s (the stronger model, 1.4s to 2.0s). For a voice-first
  deployment that is the real cost of the judge, not the tokens. It could plausibly run
  concurrently with context assembly, which already happens before planning. Not built.
- **One group regressed for BOTH models**, by the same two cases out of fifteen, in two otherwise
  independent runs. Each alone is statistically inconclusive and inside the noise floor, but the
  same group moving the same way twice is a signal rather than noise, and it has not been chased.
- Prices come from an unverified table, so read every cost figure as a ratio.

## Measure before you believe a difference

The noise floor is large and easy to mistake for a result. Running one configuration three times
over 86 cases, 14 of them changed their answer, and one scenario-group of 19 rows scored anywhere
from 11 to 26 percent across single runs of the SAME configuration. Compare on the whole split, use
`--repeat` to see which cases are unstable, and treat a per-group swing on twenty rows as nothing.
