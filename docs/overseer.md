# The minimal-intervention overseer

## The idea

A high-quality model, reading and writing very few tokens, watches the run the way a person's
awareness watches their own body walk: almost always silent, occasionally sending one small signal
that causes a large downstream course correction. That is cheaper and more reliable than either
extreme (a cheap model driving the whole loop, or a strong model re-reasoning every step).

`core/overseer.py` implements this as an optional consultation the `Orchestrator` makes at two
points in a run, BOTH now non-blocking:

1. **Hook A (in-loop, non-blocking).** When the plan is a "read" (the loop will continue) AND a
   cheap, non-LLM pre-filter gate (below) says the step actually looks worth a look, the consult is
   fired into a **background thread** and the loop keeps walking without waiting. Its result is
   polled at the **top of the next plan step**, before that step's planner runs, so a `redirect` can
   steer the plan we are about to make and `answer_now`/`escalate_deep`/`escalate_human` can end the
   loop. Applying the signal one step late is the deliberate tradeoff for never stalling the walk.
2. **Hook B (answer checkpoint, non-blocking).** Once, with the draft answer included in the digest,
   right before the answer is returned to the user. This is the *last* look, so unlike hook A there
   is no later step to apply a correction one step late. It is still **non-blocking**: it submits,
   does one quick non-blocking check (covers the rare already-resolved case), and if the consult has
   not resolved yet, **ships the draft immediately** and hands the pending consult to a background
   finisher rather than waiting on every single answer (see "Hook B's non-blocking design" below). A
   fast-resolving consult still corrects the same answer before it ships, exactly as before.

A **cheap, non-LLM pre-filter gate** decides whether hook A submits at all (see "The hook-A gate"
below): the overseer model is only woken when something cheap already suggests it is worth a look
(a stuck read loop, a repeated plan, or budget pressure), not on a blind fixed cadence.

Each consultation is ONE structured tool call against a cheap, capped digest (never the full
gathered text). See "The digest" below for its exact shape. The consult returns exactly one signal:

| Signal | Meaning | Effect |
|---|---|---|
| `proceed` | on track (the default, by far the most common) | nothing changes |
| `redirect` | drifting off-subject or wasting reads | one short hint is fed back to the next plan |
| `answer_now` | enough is gathered already | stop reading, answer with what's there |
| `escalate_deep` | this needs real execution, ROUTINE AI-doable work | hand off to deep execution |
| `escalate_human` | a genuine HUMAN-ONLY fork (identity, irreversible/authorization, true ambiguity) | hand off to a confirm/decision-request, never executed autonomously |

`escalate_deep` and `escalate_human` were a single `escalate` signal in the first version; they were
split so a routine "this needs real work" case (AI acts first) is never conflated with a genuine
human-only fork (identity, an irreversible/authorization-requiring action, true ambiguity only the
user/owner can resolve). The prompt is written to bias hard toward `escalate_deep` and treat
`escalate_human` as rare, mirroring this org's "AI acts first, only genuine forks go to a human"
principle -- it must not add friction to normal automatable work.

## The digest

The digest is built by `overseer.build_digest()`, called from `Orchestrator._build_oversee_digest`.
Its fields, in order:

- **`CURRENT USER REQUEST`** -- the RAW, VERBATIM text the user typed. Always shown, so the overseer
  gets the same word-for-word fidelity the planner gets.
- **`RESOLVED AS`** -- only when the resolved `goal_condition` (anaphora like "do it" rewritten into
  the concrete instruction) differs from the raw request. An ADDITIONAL line, never a silent
  replacement of the raw text.
- **`QUALITY BAR`** -- the run's `quality_standards` (the written completion bar), when one is wired.
- **`RECENT CONVERSATION`** -- a handful of PRIOR user turns in this SAME conversation (across
  turns, not this run). The caller excludes the current turn's own request so it is never
  duplicated against `CURRENT USER REQUEST`/`RESOLVED AS`. Omitted entirely when there is no history.
- **`PRIOR ESCALATIONS THIS CONVERSATION`** -- whether an EARLIER turn in this same conversation
  already escalated (to deep execution or to a human) and its outcome, e.g. `1: escalated to deep,
  outcome: deep_met`, or `none yet`. Sourced from the caller-supplied `run(prior_escalations=...)`
  parameter (the brain has no persistent storage of its own; a caller that wants this history
  visible passes it forward from whatever it already persists about previous turns). Unlike the
  other optional sections, this one ALWAYS appears (even as "none yet").
- **`OPERATIONS THIS TURN`** -- a NUMBERED list of every operation executed so far THIS run, each
  tagged with its kind (`[read]`/`[grep]`/`[query]`/...) and a one-line result, reflecting the TRUE
  total operation count even when only a trailing window is shown.
- **`PASS`**, **`CURRENT PLAN`**, **`RATIONALE`**, **`SPEND`**, **`TIME`** -- the run's own
  progress/cost counters.
- **`AGENT'S READ BUDGET`** -- the MAIN AGENT's own cumulative raw-read volume against its read cap.
  Explicitly disclaimed as NOT this digest's own (tiny) size, to avoid a misread.
- **`DRAFT ANSWER`** -- only present at hook B, the proposed reply's first 200 chars.

**Truncation order.** `CURRENT USER REQUEST`/`RESOLVED AS`/`QUALITY BAR` and
`PASS`/`CURRENT PLAN`/`RATIONALE`/`SPEND`/`TIME`/`AGENT'S READ BUDGET`/`DRAFT ANSWER` are PROTECTED:
built first, always included in full. `RECENT CONVERSATION`, `PRIOR ESCALATIONS THIS CONVERSATION`,
and `OPERATIONS THIS TURN` are SHEDDABLE: each has its own per-section char cap, and if the overall
`char_budget` is still tight, whole sheddable sections are dropped (last-added first) until it fits.
Only if the protected fields alone somehow exceed `char_budget` (a pathologically small budget) does
a last-resort tail-truncation kick in.

## The hook-A gate (cheap pre-filter, no LLM)

Before hook A submits a consult, `Orchestrator._submit_oversee` (with `gate=True`, its default) runs
`_oversee_worth_a_look()`, a pure, free function with no model call, that returns `True` only when at
least one cheap signal suggests the step is actually worth a look:

- `consecutive_reads` has crossed `overseer_gate_min_consecutive_reads` (default 2, a stuck read
  loop), or
- the plan repeats the previous step's action+goal verbatim (`overseer_gate_repeat_plan`, default
  on -- a sign of looping on the same idea), or
- elapsed time OR gathered-read volume has crossed `overseer_gate_spend_fraction` (default 0.6) of
  its budget.

Hook B is NOT gated by this (`gate=False`): it is a one-time final check, not a cadence, so it always
consults (subject only to `overseer_max_signals`). This matches the design intent: the cheap signal
watches constantly and for free; the expensive model is woken rarely, only when something already
looks worth a look, not on a blind fixed schedule.

## Hook B's non-blocking design

Hook B used to WAIT synchronously (up to a short bound) before shipping every answer -- real added
latency on every turn even when nothing was wrong. It is now non-blocking like hook A: submit, do
one quick non-blocking check, and if unresolved, ship the draft NOW and hand the pending future to
`Orchestrator._finish_oversee_in_background`, a background daemon thread bounded by
`overseer_background_finish_timeout_seconds` (default 30s) that applies a BEST-EFFORT, non-
authoritative follow-up once (if) the consult resolves:

- **`escalate_human`**: raises a REAL decision-request via the wired `EscalationSink` (the same
  durable mechanism `_run_confirm` uses), so a human is notified even though the stream that served
  this turn's answer may already be closed. This is the one case where "best-effort" is still
  durable -- decision-requests are their own async channel, not dependent on the stream staying open.
- **`redirect` / `escalate_deep`**: recorded via a late `EVENT_OVERSEER` (`data.late = True`) so a
  still-listening consumer can see it, and so a caller that persists these events can pass them
  forward as `prior_escalations` on the NEXT turn.
- **`proceed` / a timeout**: nothing to do.

This deliberately does NOT autonomously launch a new deep execution for a late `escalate_deep`:
nothing is left to receive its result once the original caller has already moved on with its
answer, so auto-running unattended work with no supervision is out of scope for this fire-and-forget
path. A consult that resolves FAST (the common case for a quick model) still corrects the SAME
answer before it ships, exactly as in the original synchronous design.

## Design properties (verifiable, not measured-in-production)

This is a new, **off-by-default** feature (`OrchestratorConfig.overseer = False`); enabling it does
not change any existing run until a consumer opts in. What's true today, by construction:

- **Cost-bounded by config, not by luck.** The digest is hard-capped at `overseer_digest_char_budget`
  (default 1600 chars, with its own per-section caps on the sheddable history fields) and
  consultations are capped at `overseer_max_signals` per run (default 3, shared across both hooks).
  Observation bodies are one-lined before they reach the digest, so the overseer never reads the
  full gathered text.
- **Fails safe.** Any exception, non-dict response, or unrecognized signal degrades to
  `OverseerSignal("proceed")`. The call sites in `core/orchestrator.py` are also wrapped in
  `try/except: pass`, so an overseer failure can never break a turn.
- **Off means byte-for-byte identical.** With `overseer=False` (the default), zero overseer calls
  are made, zero `EVENT_OVERSEER` events are emitted, and **zero background threads are spawned**;
  nothing about the existing loop changes.
- **Non-blocking (both hooks).** The overseer's provider call runs on a per-run background
  `ThreadPoolExecutor` (the same idiom as context assembly), torn down `wait=False` in `finish()`,
  so consulting the overseer never adds latency to the user-facing loop or the answer.
  `overseer_poll_timeout_seconds` is `0.0` in production (a pure `future.done()` check); tests set
  it positive to make the async apply deterministic, and it always degrades to proceed/ship-the-
  draft on timeout.
- **Tested.** `tests/test_overseer.py` (digest building incl. all new sections and the truncation
  order guarantee, the five-signal split, the non-blocking submit/late-apply behavior for both
  hooks, the hook-A gate) plus `tests/test_goal_condition_derivation.py` and
  `tests/test_conversation_understanding.py` for the related goal-condition changes.

## Known limitation: token-accounting isolation (not fixed, documented)

**Finding.** In the common single-provider setup (no `MultiProvider` with per-family sub-providers
registered), `Orchestrator.get_provider_for_model()` falls back to `self.provider` -- the SAME
provider instance the planner and answerer use. Provider implementations (e.g. `AnthropicProvider`)
track `tokens_in`/`tokens_out` as simple cumulative instance attributes (`self.tokens_in += ...`).
This means the overseer's OWN `provider.plan()` calls increment the SAME counters the digest's
`SPEND` line reads, and that `finish()` reports as the turn's total token cost: the overseer's own
consultation cost bleeds into "how much the agent has spent," which could make the overseer (and a
consumer reading `OrchestratorResult.tokens_in/out`) think the agent burned more than it actually did.

**Why it is not fixed here.** The obvious fix (snapshot `tokens_in`/`tokens_out` before submitting
the overseer's own call, subtract the delta back out after) is UNSAFE given the non-blocking design
this doc describes: the overseer's call runs in a BACKGROUND THREAD *concurrently* with the main
loop, which may make its OWN provider calls (more reads, the next plan, the answer) WHILE the
overseer's call is in flight. A naive before/after delta on a shared, un-locked counter cannot
reliably attribute which portion of the delta was the overseer's vs. the main loop's concurrent
work; "fixing" it that way would silently corrupt the count instead (a worse bug than the leak it
would fix). A genuinely correct fix requires each provider CALL (not each provider instance) to
report its own token usage, e.g. `plan()`/`answer()` returning usage alongside the result, and the
loop attributing it at the call site -- a real interface change across every `ModelProvider`
implementation (`AnthropicProvider`, `GeminiProvider`, `OpenAIProvider`, `ClaudeCliProvider`,
`MultiProvider`), out of scope for this feature.

**Practical implication.** When `overseer=True` and no multi-provider routing isolates the overseer
onto a separate provider instance, treat `SPEND`/`res.tokens_in`/`res.tokens_out` as including a
small amount of the overseer's own cost, not a perfectly pure measure of the main agent's spend. This
is usually negligible (the digest and signal are both small/cheap by design) but is not currently
zero.

## What isn't claimed yet

There is no production A/B data on redirect/escalate precision (false-positive rate, tokens saved
per run) because the feature is new and off by default. A first labeled signal eval exists:
`evaluation/overseer_signals_eval.py` feeds 11 hand-labeled hook-A/hook-B digests through
`oversee()` with a real model, including CONTRAST PAIRS (same fix-and-commit request with a
describes-the-fix draft vs a reports-it-done-with-evidence draft; the same subject phrased as a
question vs an instruction) and no reuse of `OVERSEER_PROMPT`'s own exemplar phrasings, so a pass
shows judgment rather than phrase-matching. Result 2026-07-06: 11/11 correct signals at
`gemini-3.5-flash`, with both contrast pairs discriminated correctly. That is still a small
authored suite, not production data: before flipping it on for a real workload, run it against a
larger labeled task set and record top-line numbers here, the same way
[TF-DF-IDF sampling](TF_DF_IDF_SAMPLING.md) and the [retrieval evaluation](../README.md#evaluation)
are documented with real, reproducible numbers rather than estimates.

## Enabling it

```python
from quest_ai_runner.config import RunnerConfig, build_orchestrator

cfg = RunnerConfig(...)
orch = build_orchestrator(cfg)
orch.cfg.overseer = True
orch.cfg.overseer_tier = "best"       # a high-quality tier; see model_registry.TIERS
orch.cfg.overseer_every_steps = 1
orch.cfg.overseer_max_signals = 3
orch.cfg.overseer_poll_timeout_seconds = 0.0                  # both hooks: 0.0 = never block (default)
orch.cfg.overseer_background_finish_timeout_seconds = 30.0    # hook B's background finisher bound
orch.cfg.overseer_gate_min_consecutive_reads = 2               # hook-A gate: stuck-read-loop threshold
orch.cfg.overseer_gate_repeat_plan = True                      # hook-A gate: repeated-plan signal
orch.cfg.overseer_gate_spend_fraction = 0.6                    # hook-A gate: time/read budget fraction
```

Or from the environment (the CLI `poll`/`chat` consumers read these in `_config_from_env`):

```bash
QAR_OVERSEER=true              # enable (off by default)
QAR_OVERSEER_TIER=best         # the judge's model tier (default best)
QAR_OVERSEER_MAX_SIGNALS=3     # hard cap on consultations per run
```

`OrchestratorResult.overseer_signals` holds every consultation this run made
(`[{"signal", "hint", "reason", "step"}, ...]`), `None` when the overseer never ran.
`run(..., prior_escalations=[...])` lets a caller feed forward earlier turns' escalation outcomes in
this same conversation, so the overseer digest's `PRIOR ESCALATIONS THIS CONVERSATION` section is
populated across turns (the brain itself keeps no persistent state).

## Token-efficiency: the tool schema is deliberately terse

`ClaudeCliProvider.plan()` (the keyless CLI provider) cannot force native `tool_choice`, so it
appends the ENTIRE `OVERSEE_TOOL` JSON schema as inline text to the prompt on EVERY consultation.
`OVERSEE_TOOL`'s field `description`s are therefore kept to bare mechanical minimums (e.g. `hint`:
`"only for redirect, under 200 chars"`); the full behavioral semantics of each signal/field live
ONLY in `OVERSEER_PROMPT`'s prose, never duplicated in the schema. Trimming the escalate_deep/
escalate_human-split schema this way cut the appended schema block from ~597 to ~318 characters
(about 47% smaller), a real, repeated saving since it is paid on every single consultation, not once.
