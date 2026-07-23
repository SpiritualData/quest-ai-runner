# The anticipation engine

> Status: **opt-in, off by default** (`OrchestratorConfig.anticipation_enabled = False`). With the
> flag off, or no `Anticipator` wired, a run is byte-for-byte identical to today's behavior: zero
> anticipation calls, zero background threads, zero store files touched.

## The idea

The assistant should feel like it already knows what's coming. `core/anticipation.py` learns
recurring **ask patterns** — what gets asked, at what time of day, on what day of the week — and
uses them to predict the user's likely next ask *before* they type it. When a prediction is live,
its context is precomputed in the background, so if the real ask matches, the turn is seeded with
a cheap, already-assembled bundle instead of starting cold.

This is pattern-based, not model-based: **no LLM calls happen anywhere in this engine**. Learning
is pure keyword/time-signature matching plus a simple online weight update, so it's cheap enough to
run on every turn and safe enough to never be the reason a turn is slow or wrong.

## The objective function and online learning

A prediction's quality is defined by exactly one number, `score_outcome(predicted_text,
actual_text)`: the keyword-set **similarity** between what was predicted and what was actually
asked next, in `[0, 1]`. `similarity` is `0.5 * Jaccard + 0.5 * containment-of-the-smaller-set` —
Jaccard alone punishes a short ask fully contained in a longer one; containment alone rewards tiny
overlaps in huge sets; the blend does neither.

That single score drives everything, so online learning optimizes exactly what gets measured:

- **Serving.** A live prediction is only ever served as a hint when its score against the actual
  message clears `MATCH_SERVE` (0.45). Below that, the prediction still gets scored — a miss is a
  learning signal, never user-visible breakage.
- **Logging.** Every live prediction's outcome (hit or miss) is appended to
  `predictions/prediction_log.jsonl`, so hit rate and mean score are measurable offline from the
  file alone, no code required.
- **The EMA weight update.** Each learned pattern keeps one number, its `weight`, updated online via
  `update_weight`: `w <- w + ALPHA * (score - w)` (`ALPHA = 0.3`). A pattern whose predictions keep
  scoring well drifts geometrically toward 1.0; one that keeps missing decays toward 0.0 and is
  eventually pruned (`PRUNE_WEIGHT = 0.05`) — or once unseen for `PRUNE_AGE_DAYS` (30). A brand-new
  pattern starts at `CONF_FLOOR` (0.35): confident enough to predict immediately, but one bad miss
  drops it below the floor until it recurs.

The two turn-boundary operations:

- **`reinforce_or_create`** (turn end, via `Anticipator.learn`) folds the turn's actual ask into the
  scope's pattern set. If an existing pattern's keywords are at least `SIM_REINFORCE` (0.55) similar,
  it's REINFORCED (hit counted, weight EMA-updated with score 1.0 — the ask recurring is the
  strongest evidence the pattern is real — time signature and keyword profile updated to the
  latest occurrence). Otherwise a NEW pattern is created at `CONF_FLOOR`. The set is then pruned
  (weight floor, age) and capped (`MAX_PATTERNS_PER_SCOPE = 500`, lowest weight/oldest dropped
  first) — the pattern just absorbed is never itself pruned by that same pass.
- **`generate_predictions`** (turn end, via `Anticipator.plan_next`) ranks the scope's patterns by
  `rank_patterns`: `time_proximity * keyword_factor * weight`. `time_proximity` measures how close a
  pattern's hour-bucket/day-of-week signature is to now; `keyword_factor` is a floored blend
  (`0.5 + 0.5 * similarity`, or `1.0` with no topic keywords at all) so a reliably time-based
  pattern (the every-morning ask) is damped, never zeroed, by unrelated recent chatter. Patterns
  whose score clears `CONF_FLOOR` become live `Prediction`s (deduped by canonical text, capped at
  `K = 3` per scope), each with a `PREDICTION_TTL_SECONDS` (30 min) expiry.

## Scope keys

Patterns and predictions are stored **per scope key**, the same vocabulary `core.recent_context`
uses: `conv:<conv_id>`, `quest:<quest_id>`, and always `"global"`. `Orchestrator._anticipation_scope_keys`
builds the list narrowest-first (conv, then quest, then global) from the turn's `context_meta`;
`Anticipator.observe` consults them in that order, so on a score tie the narrower scope wins. A
consumer that never passes `conv_id`/`quest_id` still gets cross-conversation learning for free via
the always-present `"global"` scope.

## File layout

`FilePredictionStore` (rooted at `context_cards_dir`, same root the card/recent-context stores use)
writes under `<root>/predictions/`:

```
predictions/
  <sha1(scope_key)[:16]>.json   # {"patterns": [...], "predictions": [...]}
  prediction_log.jsonl          # one JSON object per outcome, append-only
```

Each scope's JSON file holds its learned `patterns` and its current live `predictions`; each
prediction record optionally carries a `context_view` sidecar (the precomputed bundle text, kept
out of the shared `Prediction` dataclass on purpose — it's a runner-lane persistence detail, not
part of the reusable contract). Every store method is best-effort: a missing or corrupt file
returns `[]`/`""` rather than raising, and writes are atomic (tempfile + `os.replace`, the same
convention `adapters.card_repository` uses).

## The never-break-a-turn guarantee

Every method on `Anticipator` and `FilePredictionStore` is wrapped so a failure degrades silently
to the normal path — a prediction miss, a corrupt file, an assembler exception, all just mean *no
anticipated context this turn*, never a broken one:

- **Off means inert.** With `anticipation_enabled = False` (the default), the orchestrator's
  anticipation blocks are skipped entirely: no `observe()` call, no background thread, no file
  under `predictions/` is ever created.
- **Serving can only save work, never cost it.** The `--- ANTICIPATED CONTEXT` block is prepended
  as a hint; the turn's normal context assembly still runs in full and still leads. A served
  prediction is a discardable shortcut, never a substitute for grounding.
- **Learning is fully backgrounded.** `Anticipator.learn` + `plan_next` run on a daemon thread
  registered the same way background context-index threads are (`config._register_index_thread`),
  so `config.shutdown_background_index()` closes and joins it; the returned answer never waits on
  learning.
- **A failed precompute is just no bundle.** If the wired `ContextAssembler`'s `assemble()` raises
  for a predicted ask, `plan_next` logs it and moves on — the prediction still exists, it just
  carries no `context_view`.
- **Concurrency is last-writer-wins, by design.** Scope files are read-modify-written without a
  lock; the atomic `os.replace` prevents *corruption*, not *lost updates*. Two concurrent turns
  touching the same scope (the always-present `global` scope, most likely) can silently clobber
  each other's pattern update — an acceptable loss for a best-effort learning signal that will be
  re-observed on the next recurrence. Turn-end learn/plan is also **single-flight per
  orchestrator**: while one turn's background thread is still running, the next turn's kickoff is
  skipped (dropped, not queued), so fast turns can't pile up threads racing the assembler. A
  consumer with real write concurrency (e.g. a multi-worker service) should back its store with
  storage whose per-scope update is atomic at the document level rather than reusing the file
  store.
- **The outcome log grows unbounded.** `prediction_log.jsonl` is append-only with no rotation;
  it's an offline metrics record, so rotate or truncate it externally if a long-running
  deployment cares.

## Enabling it

```bash
QAR_ANTICIPATION=1     # opt in (off by default); read in cli.py's _config_from_env
```

Or from config directly:

```python
from quest_ai_runner.config import RunnerConfig, build_orchestrator

cfg = RunnerConfig(...)
cfg.orchestrator.anticipation_enabled = True
orch = build_orchestrator(cfg)   # resolve_anticipator wires a FilePredictionStore-backed
                                  # Anticipator over the same context_cards_dir the card store uses
```

`resolve_anticipator(cfg, context_assembler=...)` builds the `Anticipator` over whatever
`ContextAssembler` `build_orchestrator` already resolved (so it's never constructed twice) —
that's what powers per-prediction precompute; passing `None` still works, predictions then just
carry no precomputed bundle.

## Reusing the pure functions with your own storage

The learning algorithm lives once, as pure functions with no file or LLM I/O:
`extract_features`, `similarity`, `score_outcome`, `update_weight`, `reinforce_or_create`,
`rank_patterns`, `generate_predictions`, `match_actual`. A consumer with its own persistence (an
async database, a distributed cache) reuses these directly instead of porting a duplicate
implementation — read/write `Pattern`/`Prediction` dataclasses from your own store, call the same
functions, and the online-learning contract (the EMA formula, the pruning thresholds, the objective
function) stays identical everywhere the module is used. `FilePredictionStore` + `Anticipator` are
just the runner lane's own wrapper of that same core.
