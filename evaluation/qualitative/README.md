# Qualitative QAR (Quest AI) evaluation

Goal: measure whether Quest AI in the real app chat makes good, grounded, correctly routed, safely
side-effecting decisions on a rich realistic account, not just which planner action it picks (that
is `evaluation/chat_quest_ops_routing_eval.py`, left untouched). Three datasets of 30+ cases each:

* `explicit`: the conversation is pinned to the case's quest (the assistant knows which quest).
* `implicit`: no quest pinned; the assistant must work out which of five quests is meant.
* `multistep`: requests that decompose into several steps with order or independence constraints.

DEV ONLY. Credentials come from the dev lane env file (relative to this checkout, or
`QUAL_DEV_ENV_FILE`); `devclient.py` refuses to load unless the base URL is the dev backend.

## Files

* `devclient.py`: dev REST + SSE client.
* `world.py`: the disposable world (5 quests, 12 collections, notes, docs, team context, 16 named
  pivot facts), `snapshot()`/`diff()` side-effect detector, `revert()`, `reset()`, `teardown()`.
* `judge.py`: evidence builder, deterministic pre-checks, `claude -p` judge (sonnet, no API key).
* `runner.py`: loads `datasets/*.json`, drives one fresh conversation per case, snapshots the DB
  before and after, pre-checks, judges, writes `RESULTS.md` and `/tmp/qualeval/raw/*.json`.
* `datasets/schema.md`: the case schema. `datasets/_example.json`: two smoke cases.

## Workflow

```
PY=.venv/bin/python3        # from the quest-ai-runner repo root
$PY evaluation/qualitative/runner.py setup --wait 900   # files 5 quest-approval asks, waits for a person
$PY evaluation/qualitative/world.py show                # ids, pivots
# author evaluation/qualitative/datasets/{explicit,implicit,multistep}.json (see schema.md)
$PY evaluation/qualitative/runner.py run --dataset explicit --only EXP-001,EXP-002   # try a few
$PY evaluation/qualitative/runner.py run --dataset all --workers 4
$PY evaluation/qualitative/runner.py report             # RESULTS.md
$PY evaluation/qualitative/runner.py reset              # back to seeded state, no new approvals
$PY evaluation/qualitative/runner.py teardown           # delete everything and prove it
```

### The one human step: quest approval

Quest holds every quest created by an API key (an AI actor) for a person's approval, and an API key
can never approve it. So `setup` files five "Asks for you" decisions on dev
(ids kept in `/tmp/qualeval/world_asks.json`) and exits 3 until a person, signed in to the dev app,
approves them. Re-running `setup` adopts the approved quests (matched by outcome and the ZZQEVAL
tag), then builds everything else. Approve once; after that use `reset` or `teardown --keep-quests`,
which keep the five quests and need no approval again. A full `teardown` deletes the quests, so the
next `setup` needs approving again (an identical declined ask is refused for 30 days: do not decline
them, use `--decline-asks` only deliberately).

`setup --partial` builds a smoke world with no quests (four unlinked collections) so the pipeline
can be exercised without approvals; it cannot serve the datasets. Use `run --examples` with it.

## How a case runs

1. snapshot (quests, goals, notes, measurable outcomes, every world collection's entries, queued tasks)
2. new conversation (`quest_ids` per `conversation_scope`), each message streamed with `auto_run`
3. snapshot again, `diff` = the side effects; tasks the chat queued are listed then DELETED at once
   so the dev runner lane never executes them
4. deterministic pre-checks (`precheck` + `expect_writes` + `forbid_writes` + routing); a hard
   failure scores 0 and skips the judge unless `judge_always`
5. LLM judge: per-rubric pass/fail with a quote, score, routing, side effects, pivot use, code review
6. mutating case: `revert()` the diff; if the world will not return to its snapshot, `reset()`
7. context cards the turn learned are deleted (they persist per user and would leak into the next
   case); the conversation is deleted

A case passes when score >= 0.7, no hard pre-check failure, and the judge says routing and side
effects are right.

## What the chat exposes as evidence (SSE frames)

`plan`/`replan` (planner action per step; the `text` is usually null), `understanding` ("Understood
as: ..."), `read` (gather step; `sources` is usually empty), `context` (cards assembled with ids and
titles, adapters and counts), `exec` (phase `code`, `executing`, `retry` with the error, `output`,
`continuing`, `done`; the generated Python is in `code`; there is no named tool field), `status`
ticks (including "Applied guidance: ..." and "Context from: ..."), `token`/`partial`/`done` (the
reply), `delegated` (handed to an environment as a task), `tokens`, and `explanation` (the "Explain
how I got this" payload: `used.cards`, `used.sources`, `used.reads`, `used.actions` with state,
`signals.exit_reason/goal_met/claims_unexecuted/steps`, plus understood/approach/confidence/
assumptions/limitations). The explanation is the best evidence of which cards and sources were
actually leveraged. Pivots are NOT tagged in any frame: the judge decides "used" from the reply,
code and output.

## Known limits and traps

* A timer habit's seconds cannot be seeded (the backend derives them from session records); seeded
  habit rows are completed days. Habit history needs explicit `entry_date` or the row lands on today.
* Learned context cards persist across conversations and some pre-date the world (old conversation
  cards, e.g. from the earlier routing eval). They can leak facts into a case; cards created during
  the run are removed, older ones are not. Note it in findings when a reply cites an unknown fact.
* Parallel cases share one snapshot space; a side effect seen during a parallel run is flagged
  `side_effects_ambiguous`. Keep anything that might write out of the parallel pool.
* Dev runs `auto_run=true` only; the approval-card path is untested here.
* One run per case, one judge call per case: report variance honestly, rerun disputed cases.
* The judge is sonnet through the subscription CLI; a usage-limit refusal surfaces as `JUDGE ERROR`
  and the case is reported unjudged, never passed.

## Iterating (for the next AI)

1. Read `RESULTS.md` failure classes, then the raw JSON of the worst cases in `/tmp/qualeval/raw/`.
2. Fix cases the HARNESS got wrong first (a rubric the world cannot satisfy, an `expect_writes`
   field name that does not exist) before blaming QAR. `judged.verdict.summary` and the pre-check
   `detail` strings say which.
3. Extend `PIVOTS` and the world together (add the fact to the seeded data AND to `PIVOTS`), then
   `reset`; never hand-edit dev data.
4. File QAR/quest-backend defects as findings with the case id, and re-run only those ids.
