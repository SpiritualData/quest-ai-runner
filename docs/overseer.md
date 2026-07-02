# The minimal-intervention overseer

## The idea

A high-quality model, reading and writing very few tokens, watches the run the way a person's
awareness watches their own body walk: almost always silent, occasionally sending one small signal
that causes a large downstream course correction. That is cheaper and more reliable than either
extreme (a cheap model driving the whole loop, or a strong model re-reasoning every step).

`core/overseer.py` implements this as an optional consultation the `Orchestrator` makes at two
points in a run:

1. **Hook A (in-loop, non-blocking).** On a cadence (`overseer_every_steps`), when the plan is a
   "read" (the loop will continue), the consult is fired into a **background thread** and the loop
   keeps walking without waiting. Its result is polled at the **top of the next plan step**, before
   that step's planner runs, so a `redirect` can steer the plan we are about to make and
   `answer_now`/`escalate` can end the loop. Applying the signal one step late is the deliberate
   tradeoff for never stalling the walk (see "Non-blocking design" below).
2. **Hook B (answer checkpoint).** Once, with the draft answer included in the digest, right
   before the answer is returned to the user. This is the *last* look, so unlike hook A there is no
   later step to apply a correction: hook B therefore still **waits** for its consult, but only up
   to a strict short bound (`overseer_answer_checkpoint_timeout_seconds`, default 8s), after which
   the draft is accepted (degrade to proceed). The call still runs on the background executor, so
   the wait is bounded and can never hang.

Each consultation is ONE structured tool call against a cheap, capped digest (never the full
gathered text). The digest's `REQUEST` line is the **resolved, self-contained request** (the
orchestrator's `goal_condition`, which rewrites anaphora like "do it" into the concrete
instruction), not the raw surface text, and it carries a `QUALITY BAR` line (the run's
`quality_standards`) when one is in play, so the overseer judges the run against what it is actually
trying to do and the bar it must clear. The `AGENT'S READ BUDGET` line reports the *main agent's*
own cumulative read volume against its read cap, which is unrelated to the digest's own (tiny) size.
The consult returns exactly one signal:

| Signal | Meaning | Effect |
|---|---|---|
| `proceed` | on track (the default, by far the most common) | nothing changes |
| `redirect` | drifting off-subject or wasting reads | one short hint is fed back to the next plan |
| `answer_now` | enough is gathered already | stop reading, answer with what's there |
| `escalate` | this needs real execution or a human, not more reading | hand off to deep |

## Design properties (verifiable, not measured-in-production)

This is a new, **off-by-default** feature (`OrchestratorConfig.overseer = False`); enabling it does
not change any existing run until a consumer opts in. What's true today, by construction:

- **Cost-bounded by config, not by luck.** The digest is hard-capped at `overseer_digest_char_budget`
  (default 1200 chars) and consultations are capped at `overseer_max_signals` per run (default 3,
  shared across both hooks). Observation bodies are one-lined before they reach the digest, so the
  overseer never reads the full gathered text.
- **Fails safe.** Any exception, non-dict response, or unrecognized signal degrades to
  `OverseerSignal("proceed")`. The call sites (`_maybe_oversee` and its two call points in
  `core/orchestrator.py`) are also wrapped in `try/except: pass`, so an overseer failure can never
  break a turn.
- **Off means byte-for-byte identical.** With `overseer=False` (the default), zero overseer calls
  are made, zero `EVENT_OVERSEER` events are emitted, and **zero background threads are spawned**;
  nothing about the existing loop changes.
- **Non-blocking (hook A).** The overseer's provider call runs on a per-run background
  `ThreadPoolExecutor` (the same idiom as context assembly), torn down `wait=False` in `finish()`,
  so consulting the overseer never adds latency to the user-facing loop. A consult that has not
  resolved by the next poll is simply left pending and re-checked; the loop never blocks on it.
  `overseer_poll_timeout_seconds` is `0.0` in production (a pure `future.done()` check); tests set
  it positive to make the async apply deterministic, and it always degrades to proceed on timeout.
- **Tested.** 14 unit tests in `tests/test_overseer.py` (digest building incl. the `QUALITY BAR`
  and relabeled read-budget lines, truncation, signal parsing, safe-default fallback, prompt
  guidance, and the non-blocking submit/late-apply behavior) plus the end-to-end wiring tests.

## What isn't claimed yet

There is no production A/B data on redirect/escalate precision (false-positive rate, tokens saved
per run) because the feature is new and off by default. Before flipping it on for a real workload,
run it against a labeled task set (see `evaluation/`) and record top-line numbers here, the same way
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
orch.cfg.overseer_poll_timeout_seconds = 0.0                 # hook A: 0.0 = never block (design default)
orch.cfg.overseer_answer_checkpoint_timeout_seconds = 8.0    # hook B: strict short wait before shipping
```

`OrchestratorResult.overseer_signals` holds every consultation this run made
(`[{"signal", "hint", "reason", "step"}, ...]`), `None` when the overseer never ran.
