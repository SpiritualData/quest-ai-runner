# Answer explanation ("Explain how I got this")

**Status:** opt-in, off by default. `OrchestratorConfig.explain_answer` / `QAR_EXPLAIN_ANSWER=1`.
**Code:** `core/answer_explanation.py`, `Orchestrator.write_answer_explanation`, `EVENT_EXPLANATION`.

A user-facing, expandable account of how a turn reached its answer: what the question was taken to
be, what information was used, the shape of the reasoning, the assumptions, the confidence and
limits, and what would change the answer.

## What this is NOT

It is not the internal reasoning narration. That channel relays the run's **own** wording (plan
rationales, "Understood as: ...", partial narration beats) and is meant for a debug surface, not a
reader. This feature carries wording **composed for the reader, about the run**. The distinction is
the whole design, so it is enforced structurally rather than by convention:

- The generation call never sees, and never relays, the run's internal narration.
- The goal condition is fed in as *input* and comes back out rewritten in second person.
- The retrieval half of the payload is built from the same **projected** shapes `EVENT_CONTEXT`
  streams (`_project_card_metadata_for_event`, `_project_sources_for_event`), which deliberately
  strip item text and keep only labels, counts and path-like items. Nothing unprojected can reach
  the panel.

A consumer should render it as a normal user feature, never behind a debug/internals flag.

## Why a second call, after the answer

`EVENT_EXPLANATION` is emitted in `finish()` **after** the terminal `EVENT_RESULT` and **before**
`EVENT_DONE`. That ordering is the feature, not an implementation detail:

- The answer is one blocking call, not a token stream. Anything folded into the answer call delays
  the answer itself by its whole generation time. Emitting the result first means the reader is
  already reading while the explanation is written, so the perceived cost is zero.
- Co-generating would also fight `REPLY_VOICE_SYSTEM`, the system prompt on every reply call, which
  explicitly forbids the reply from carrying reasoning narration, plans, source or card names,
  counts, ids, tool names or the goal condition. That prompt exists because blending is the exact
  failure mode it was written against.
- Neither shape is more faithful. Both are post-hoc reconstructions by the same model; being in the
  same completion does not let a transformer read its own activations. What actually moves
  faithfulness is grounding the account in the real trace, and that is easier here, not harder.

## Eligibility is model-free

`is_eligible(trace)` is a boolean over the turn's real record. No LLM call, no threshold to tune,
and it cannot be wrong in an expensive way. A turn is eligible when it is an answer or deep turn
with actual output AND any of:

- reads or searches produced a non-error observation
- an execution action was recorded
- it was a deep run
- context cards or sources reached the answer
- the planner took more than one step
- the web was searched

A plain "Hi" takes the small-talk short circuit: no retrieval, no actions, answered at the first
step, so every clause is false and nothing is emitted. A confirm, a clarification and a cancelled
run are never eligible, because they are not answers.

## Half the payload is a record, not prose

| Payload field | Source | Verifiable |
|---|---|---|
| `used.cards` / `used.sources` | the projected context this turn assembled | yes |
| `used.reads` | real observations (path, locator, kind) | yes |
| `used.actions` | `ExecutionRecord` facts with succeeded/failed state | yes |
| `used.web` | a web-search observation was present | yes |
| `signals.*` | exit reason, goal verdict, claim-honesty flag, step count | yes |
| `understood`, `approach`, `assumptions`, `confidence`, `limitations`, `what_would_change` | one cheap-tier model call | no |

Two consequences worth keeping:

1. **A failed generation call does not drop the panel.** `build_payload(trace, None)` still returns
   the recorded half, which stands on its own.
2. **The written half is constrained to the record.** `render_record_for_prompt` hands the call the
   real observations, the execution record with per-action outcomes, and the goal verdict, and the
   prompt forbids asserting a step the record does not show or naming a source the record does not
   name. This is the same failure the goal verifier guards with `claims_unexecuted`: a confident
   account of tool calls that never happened is worse than no account at all.

Because the written half is a reconstruction, a consumer should show a short disclaimer at the foot
of the expanded panel. Word it as a claim about the product, not about the model's cognition:
"Written by Quest AI to describe how it reached this answer. It is a summary, not a recording of
every step."

## Payload shape (version 1)

```json
{
  "version": 1,
  "understood": "string",
  "approach": "string",
  "assumptions": ["string"],
  "confidence": "string",
  "limitations": ["string"],
  "what_would_change": ["string"],
  "used": {
    "cards":   [{"title": "str", "adapter": "str"}],
    "sources": [{"label": "str", "adapter": "str", "item_count": 0}],
    "reads":   [{"kind": "read|grep|query", "path": "str", "locator": "str"}],
    "actions": [{"goal": "str", "state": "succeeded|failed|unknown"}],
    "web": false
  },
  "signals": {
    "exit_reason": "str", "goal_met": true, "verdict_reason": "str",
    "claims_unexecuted": false, "steps": 0, "deep": false
  }
}
```

Every field is optional except `version`, `used` and `signals`. A consumer must render defensively
and show nothing at all when the payload is absent.

The same payload is also set on `OrchestratorResult.explanation`, so a non-streaming consumer can
persist it from the returned result without observing the event stream.

## Configuration

| Setting | Env | Default | Meaning |
|---|---|---|---|
| `explain_answer` | `QAR_EXPLAIN_ANSWER` | off | Turn the feature on. |
| `explain_tier` | `QAR_EXPLAIN_TIER` | `fast` | Tier for the one extra call. It summarizes a turn that already happened, so it does not need the strong tier (that is `verify_tier`, which gates the turn's outcome). |

With the flag off the run is byte for byte what it was: no extra call, no extra event, nothing
attached to the result.

## Tests

`tests/test_answer_explanation.py` covers the four properties the design depends on: eligibility is
model-free and excludes small talk, the event lands after the result and before done, the recorded
half survives a failed generation call, and the feature is inert when off.
