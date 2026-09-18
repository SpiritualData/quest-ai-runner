# Continuing a deep run that ran out of turns

A deep run is bounded by `--max-turns` so it can never run away. That bound does its job, and it
also produces a specific, expensive failure the goal loop used to handle badly: the worker did real
work, used its whole turn budget mid-flight, and stopped. It did not fail. It ran out of room.

What used to happen next was a cold retry. The loop re-ran the SAME goal with the SAME budget in a
BRAND-NEW session, so the worker re-read the files it had already read, re-derived the decisions it
had already made, and ran out of room again in roughly the same place. Repeat until the attempt cap
or the token budget, then report a bare failure. The person saw "the task failed" and no account of
the work, which was sitting uncommitted on disk the whole time.

Live case, 2026-09-09: a task on a production lane wrote two working modules, was cut off, and was
reported to its owner as failed. Every retry had paid full price for the same rediscovery.

## What happens now

**The worker says how it ended.** Claude Code's `--output-format json` envelope carries a `subtype`.
When it is `error_max_turns`, `SubprocessGoalRunner` sets `DeepResult.limit_hit=True` and returns
the `session_id` it launched with. Nothing here is inferred: a crash, a timeout, or a plain-text
worker with no envelope leaves `limit_hit` False, because resuming a crashed session would be a
guess and the two cases call for opposite responses.

**The goal loop continues that session.** Seeing `limit_hit`, the next attempt is handed:

- `resume_session_id` — the worker relaunches with `--resume <id>`, so it still holds everything it
  read, decided and changed. It does not open a second session.
- a **larger budget** — `deep_max_turns * (n + 1)`, grown once per continuation and capped at
  `DEEP_CONTINUATION_TURN_MULTIPLIER_CAP` (4x), so a task that simply needed more room gets it
  quickly, while a worker going in circles cannot talk its way into an unbounded run one
  continuation at a time.
- a **short continuation brief** (`Orchestrator._continuation_brief`) that says it was cut off and
  not that it failed, plus the verifier's next action when there is one. Deliberately short: the
  worker is resuming, so re-sending the cold-start augmentation would spend the fresh budget
  re-reading what it already knows.

The model tier is **not** escalated by a turn limit. Running out of room says nothing about the
model being too weak, and a continuation runs inside the existing session anyway.

## What is unchanged

- **The verifier is still the authority.** A worker that finished the work and then ran out of turns
  while writing up is verified met and is never continued. `limit_hit` only decides HOW an unmet
  attempt is retried.
- **The token budget still stops everything.** A continuation is a cheaper way to finish, never a
  way around the budget.
- **A runner that cannot resume behaves exactly as before.** `resume_session_id` is forwarded by
  signature inspection, like `emit` / `context_preamble` / `working_dir`. `AcpDeepRunner` accepts it
  for parity and ignores it: with no turn budget it never reports `limit_hit`, so it is never asked.

## The same mechanism across runs, not just within one

Everything above happens inside ONE call. A session that outlives that call is the other half of the
same problem: a person replies to a finished task tomorrow, or a recurring brief runs again the next
day, and the work starts from nothing. It re-reads what it read yesterday, and it cannot say what
changed since, because it has no memory of what it said.

So the session now travels, and it travels through this same path rather than a second one.

**A run reports the session it leaves behind.** The executor reads it off the run's deep results and
sends it with the terminal status (`done`, `needs_you` or `failed`). Where a run fanned out into
several subgoals there are several sessions and only one can be the thread's: the LAST one is
chosen, because its transcript ends nearest to where a follow-up picks up. A run that opened no
session at all (a plain answer, a crash, a deep runner that is not a subprocess) reports nothing
about sessions rather than an empty string.

**A run may be handed a session to continue.** A task carrying `resume_session_id` seeds the very
same `resume_session` variable a turn-budget continuation writes, so the first attempt relaunches
with `--resume <id>`. It is consumed once per turn, and a fanned-out run never resumes: several
concurrent workers cannot share one transcript.

**Resume is best effort, and that is load-bearing.** Sessions are per project directory and are
pruned, so an across-run resume of a session from days ago routinely finds nothing. Measured against
the real binary: `claude -p --output-format json --resume <unknown-id>` exits 1, prints nothing on
stdout, and writes `No conversation found with session ID: <id>` on stderr. `SubprocessGoalRunner`
recognises exactly that combination and runs the goal once more from a cold start, which is what the
run would have been had no session been offered. The person gets their work; they lose an
optimisation. Any OTHER failure of a resumed run is reported as the failure it is, because starting
over is the wrong answer to it.

Both directions are optional on the wire. A lane running this code against a backend that sends no
`resume_session_id` and ignores `session_id` behaves exactly as it did before, and an older lane
against a backend that does send one simply never resumes.

## The other half: unfinished work is reported

A run that still falls short reports what it did. The executor's failure path used to join the
results' `error` strings and drop their `output`, which is how a task with two finished modules
reached its owner as one line of error text. The work now travels with the failure under a heading
that says plainly it is unfinished and unverified, so it can be found and picked up.

Tests: `tests/test_deep_turn_budget_continuation.py` (within one run),
`tests/test_session_continuity_across_runs.py` (across runs).
