# The minimal-intervention overseer

## The idea

A high-quality model, reading and writing very few tokens, watches the run the way a person's
awareness watches their own body walk: almost always silent, occasionally sending one small signal
that causes a large downstream course correction. That is cheaper and more reliable than either
extreme (a cheap model driving the whole loop, or a strong model re-reasoning every step).

`core/overseer.py` implements this as an optional consultation the `Orchestrator` makes at two
points in a run:

1. **Hook A (in-loop).** After each plan step, on a cadence (`overseer_every_steps`), before the
   step executes.
2. **Hook B (answer checkpoint).** Once, with the draft answer included in the digest, right
   before the answer is returned to the user.

Each consultation is ONE structured tool call against a cheap, capped digest (never the full
gathered text), and returns exactly one signal:

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
  are made and zero `EVENT_OVERSEER` events are emitted; nothing about the existing loop changes.
- **Tested.** 11 unit tests in `tests/test_overseer.py` (digest building, truncation, signal
  parsing, safe-default fallback) plus wiring tests in `tests/test_runner.py`. 162 pre-existing
  orchestrator/runner/UI tests still pass unmodified.

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
```

`OrchestratorResult.overseer_signals` holds every consultation this run made
(`[{"signal", "hint", "reason", "step"}, ...]`), `None` when the overseer never ran.
