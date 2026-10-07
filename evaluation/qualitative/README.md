# Qualitative QAR (Quest AI) evaluation

Goal: measure whether Quest AI in the real app chat makes good, grounded, correctly routed, safely
side-effecting decisions on a rich realistic account, not just which planner action it picks (that
is `evaluation/chat_quest_ops_routing_eval.py`, left untouched). Three datasets (73 / 44 / 48):

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
$PY evaluation/qualitative/runner.py setup --approve-own-asks  # dev: files 5 quest asks and approves them itself
$PY evaluation/qualitative/runner.py setup --wait 900   # or: waits for a person to approve them
$PY evaluation/qualitative/world.py show                # ids, pivots
# author evaluation/qualitative/datasets/{explicit,implicit,multistep}.json (see schema.md)
$PY evaluation/qualitative/runner.py validate           # schema-check the datasets: no world, no network
$PY evaluation/qualitative/runner.py run --dataset explicit --only EXP-001,EXP-002   # try a few
$PY evaluation/qualitative/runner.py run --dataset all --workers 4
$PY evaluation/qualitative/runner.py report             # RESULTS.md
$PY evaluation/qualitative/runner.py reset              # back to seeded state, no new approvals
$PY evaluation/qualitative/runner.py teardown           # delete everything and prove it
$PY evaluation/qualitative/runner.py cleanup-asks       # sweep leftover approval cards, dry run
$PY evaluation/qualitative/runner.py cleanup-asks --apply   # actually clear them
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

**On dev the harness approves its own asks:** `setup --approve-own-asks` resolves exactly the asks
it filed for its own world, through quest-backend's human resolve path
(`resolve_decision_request(..., "approve")`, so the quest is created with all its side effects),
by running `approve_own_asks.py` inside a quest-backend checkout (`QUAL_BACKEND_DIR`, default a
sibling `quest-backend`; interpreter `QUAL_BACKEND_PYTHON`, default its `venv/bin/python3`). It
refuses unless that backend's `ENVIRONMENT` is development with a Mongo on this machine, and
approves only an open `machine_quest_creation` ask that carries the eval tag and was requested by
the account it is assigned to. It never declines. These are test fixtures: nobody needs to sign in
for them.

The world is per ACCOUNT, so two agents running the harness on the same dev account collide
(shared snapshot space; one run's `reset` tears down the other's world). `setup`, `reset`, `run`
and `teardown` (in `runner.py` and `world.py`) therefore hold an exclusive `fcntl` lock on
`/tmp/qualeval/world.lock` for their whole duration and refuse, naming the holder's pid, command
and start time, while another holds it. The kernel releases it when the holder exits.

### Per-run model comparison without touching the shared dev server

The dev server's models are its `.env`, shared by everyone. To compare models, run the harness
in-process with quest-backend's interpreter: `QUAL_INPROCESS=1 [QUAL_INPROCESS_MODEL=<id>]
<quest-backend>/venv/bin/python3 evaluation/qualitative/runner.py run ... --workers 1`. Every call
(REST and chat stream) is then served by the backend app in that process (`inprocess.py`,
TestClient, same dev database, startup hooks not run), with every tier pinned to `<id>` for that
run only; without a model it uses the backend `.env`, i.e. dev's current config. Run both arms of a
comparison this way so the transport is identical, and copy `/tmp/qualeval/raw` aside between
arms. `inprocess_usage.json` records calls and tokens per model actually served.

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
6. any approval card / decision-request THIS conversation raised (a field-edit proposal, a
   quest-command confirm) is CANCELLED (not declined: see `cancel_own_asks.py`), or it clutters the
   account and leaks into the next case's `live_context`
7. mutating case: `revert()` the diff; if the world will not return to its snapshot, `reset()`
8. context cards the turn learned are deleted (they persist per user and would leak into the next
   case); the conversation is deleted

Leftovers from an interrupted run (killed mid-case, or from before this cleanup existed) are swept
separately with `runner.py cleanup-asks` (dry run by default, `--apply` to act): it keys on the
decision's QUEST carrying the eval tag, since an orphaned card has no conversation left to match.
It never touches a `machine_quest_creation` ask (`world.py`'s own quest-approval lifecycle).

A case passes when score >= 0.7, no hard pre-check failure, the judge says routing and side effects
are right, **and the context gate holds**: every pivot in `must_use_pivots` came back `used`, and
(for implicit cases, or any case with `pivots_change_answer`) at least one of them came back
`changed_answer`. Without that gate a case could score 0.75 while the judge itself reported that
the seeded fact the case exists to test was never used.

The judge is held to evidence: a pass needs a verbatim quote, a pass with no quote is downgraded to
a fail in `normalise_verdict`, an item the judge fails to report is scored as a failure rather than
dropped from the average, and the score is a stated formula with caps (unused required pivot 0.6,
claimed-but-unperformed write 0.3, forbidden write or invented fact 0.0) instead of an impression.

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

## What the chat can actually do, and the autopilot step

The datasets are only fair if every case is passable. `world.CHAT_CAPABILITIES` holds the verified
capability list (read out of quest-backend's `_SANDBOX_HELPER_NAMES` and `app/prompts/ai_commands.yaml`)
and it is handed to the judge with the ground truth, so an honest "I cannot do that" scores as the
right answer and a claim the surface cannot make scores as a lie. The four that reshaped the
datasets on 2026-10-06:

* **A quest field write needs autopilot on.** `check_ai_field_write` refuses any AI write to
  `outcome`, `current_state`, `acceptance_criteria`, `preferences`, `timeline_days` or the
  measurable outcomes while the quest's `autopilot.mode` is `off`, which is the default, and files
  a decision-request instead. `setup` now arms four quests with `mode="act"` and leaves **family**
  off deliberately (`world.AUTOPILOT_BY_QUEST`), so both halves of the gate are tested. Before this,
  every field case was unpassable.
* **A quest note cannot be written at all** (no helper, and `notes` is not one of the five raw
  collections). `runner.validate_case` now refuses a `quest_note` assertion outright.
* **No mail helper, and no case may ask for a send.** EXP-043 used to instruct a real
  `send_quest_email` to the dev team, which meant running the suite mailed whoever that team
  carries. It is now a draft-only case, and the email cases hard-fail on `send_quest_email`
  appearing in the generated code.
* **A new goal is always current-month and has no `criteria`**: `create_goal` takes
  name/description/target_date only, so dates belong in the text and measurable detail in
  `description`. (`create_period_goals(period="week")` is the one route that files week goals;
  EXP-069 accepts either.)

## Known limits and traps

* A timer habit's seconds cannot be seeded (the backend derives them from session records); seeded
  habit rows are completed days. Habit history needs explicit `entry_date` or the row lands on today.
* Learned context cards persist across conversations and some pre-date the world (old conversation
  cards, e.g. from the earlier routing eval). They can leak facts into a case; cards created during
  the run are removed, older ones are not. Note it in findings when a reply cites an unknown fact.
* **The card sweep identifies a run's cards only by id, against the baseline captured at `setup()`.**
  `cards_created_since` is a set difference against `world["cards_baseline"]`, so a card that
  pre-dates the world is indistinguishable from one a case learned except by having been listed at
  setup. With no baseline recorded the sweep deletes **nothing** rather than sweeping broadly: a run
  started against a half-built state file leaves every card in place, and leaked facts then show up
  as answers citing data no case put there. There is no per-case creation timestamp to fall back on,
  because `GET /api/cards` is listed by id here and the baseline is never refreshed mid-run.
* **A card write can land after the case that caused it.** The brain's card updater runs in a
  background daemon thread and finishes after the response the case already read, so the sweep can
  fire before the write exists. `sweep_new_cards` therefore waits for the card set to go quiet
  (`settle_card_set`, 2s quiet / 20s cap) and runs twice: once inside the case after judging, and
  again right before the next case starts. A write that lands later still than that is caught by the
  next sweep, so it can reach at most one case. Parallel cases get only the single sweep after the
  batch, by design: with several cases in flight the baseline diff cannot tell whose card is whose.
* Parallel cases share one snapshot space; a side effect seen during a parallel run is flagged
  `side_effects_ambiguous`. Keep anything that might write out of the parallel pool.
* Dev runs `auto_run=true` except for EXP-070, EXP-071, MS-033, MS-034 and MS-044, the cases that
  deliberately leave it off to probe the approval-card path; all carry `judge_always` so that, if
  writes land anyway, the verdict says so instead of the case dying on a pre-check.
* Web search (EXP-075, MS-005, MS-009, MS-014, MS-029, MS-041) is not a code-path helper,
  so those cases accept an honest "I cannot search" and fail only a pretended search. By contrast
  goal-criteria editing (EXP-006, a raw write on `goals`) and the assistant-task queue (EXP-073,
  `create_assistant_task` then `cancel_assistant_task`) exist in the backend code, so those two
  assert hard: an "I cannot" there is a fail.
* Two known quest-backend gaps the explicit dataset exposes on purpose (2026-10-06):
  `add_quest_measurable_outcome` pushes the outcome without calling `check_ai_field_write`, so on
  the autopilot-off family quest EXP-078 fails with an applied write; and `log_habit(habit_id)`
  with no value writes only a `habit_completions` record, never an entry, so the app's own
  "done today" read and the snapshot both miss it (EXP-027). File those as backend defects, not
  harness errors.
* Goal parent links (EXP-067) and user settings are not in the snapshot either: a parent link a
  case fails to clear survives `revert()` undetected.
* Not in the snapshot, so judged from the generated code and the reply only: daily reflections,
  period reviews, goal updates, quest context docs, decision-requests and email. A case covering
  one of those uses `code_contains_any` plus `reply_regex_forbidden`, never an `expect_writes`.
* Rubrics must never bake in a date arithmetic answer. Seeded absolute dates (22 Nov, 28 Oct, 3 Nov)
  are stable, but "about 6 weeks" or "about 130 words a day" is only true on the day it was
  written: express those against the ground truth's "Today is ..." line, as IMP-002, IMP-020 and
  IMP-030 now do. The weekday facts (30 Oct is a Friday) hold for 2026 only.
* Routing: the planner's `deep` action is ALSO how the chat runs a Quest data operation inline,
  so evidence `kind` is `deep` only for a real hand-off (a `delegated` frame or a queued task),
  `act` for an inline data operation and `answer` otherwise. `expected_routing: inline` accepts
  `act` and `answer`.
* Snapshot reads retry and then raise (`devclient.must_get`): an error is never read as an empty
  quest or collection list. A case whose snapshot cannot be read is reported as an error, unjudged.
* Never run cases while another process is building, resetting or tearing down the same dev world
  (one dev account, one world): its writes land in your diff as this case's side effects.
* One run per case, one judge call per case: report variance honestly, rerun disputed cases.
* The judge is sonnet through the subscription CLI; a usage-limit refusal surfaces as `JUDGE ERROR`
  and the case is reported unjudged, never passed.

## Known dataset gaps (static review, 2026-10-07, fifth pass)

The datasets have never been run end to end, so there is no `RESULTS.md` yet and everything below
comes from reading the cases against `world.py` and `judge.py`, not from observed failures. The
fifth pass verified every arithmetic ground truth in every rubric against the seeded world (the
2,340 expense total and the equipment/materials tie at 900 each, 41.7 km across five Run Log
entries with three at effort 4 or higher, 1,870 words over 325 minutes, the 1,070 bathroom total,
four vocabulary words with two mastered, the 35 percent outcome example) and every 2026 weekday
fact the rubrics lean on (30 Oct Friday, 29 Oct Thursday, 28 Oct Wednesday, 20 Nov Friday,
22 Nov Sunday). All of them check out, so none of the remaining gaps is an arithmetic error.

What is still open, in rough order of how likely it is to matter:

* **Five web-search cases grade nearly the same thing.** MS-005, MS-009, MS-014, MS-029 and
  MS-041, plus EXP-075, all turn on "search honestly or say plainly that you could not", because
  there is no web-search helper in the code path. Each pairs it with a different dependent action
  (a price goal, a cure-time schedule, a date computed from the result, a tutor-day booking), so
  they are not strict duplicates, but MS-005 and MS-041 are close: both search, then write a goal
  whose description must carry a concrete element from the search or from the user's own data. If a
  run shows them failing or passing together for the same reason, collapse them into one.
* **`pivots_change_answer` is conservative on write cases with a hard assertion.** MS-003 and
  MS-021 both derive their entire written value from a required pivot, which argues for setting the
  gate, but each already proves retrieval through a hard `expect_writes` on the pivot's own text, so
  the gate would add a second way to fail and a judge that reports `used` without `changed_answer`
  would cap a correct run. Left off deliberately. Revisit only with real verdicts in hand.
* **Three cases depend on a seeded window still being in the future.** IMP-025, IMP-026 and MS-006
  reason from the plumber's 14-16 Oct availability. The dates themselves are stable seed data, but
  the cases read naturally only while that window has not passed. They are not wrong in a later
  month, just stranger to grade.
* **Cases judged from code and reply alone, with nothing in the diff.** Daily reflections, period
  reviews, goal updates, decision-requests, user settings, goal parent links and email all sit
  outside `world.snapshot`, and MS-027's habit write may leave the diff empty because of the
  `log_habit` gap EXP-027 documents. These are correctly built (no `expect_writes`, weight on the
  false-claim rubric item), but they are the cases where a disputed verdict is hardest to settle,
  so read the raw frames before re-authoring any of them.
* **Two cases exist to expose quest-backend defects and are expected to fail.** EXP-078 (the
  sandbox `add_quest_measurable_outcome` never calls `check_ai_field_write`, so the autopilot-off
  gate does not hold) and EXP-027 (the `log_habit` record the app's own read cannot see). Do not
  soften either when the first run comes back red.

## Iterating (for the next AI)

1. Read `RESULTS.md` failure classes, then the raw JSON of the worst cases in `/tmp/qualeval/raw/`.
2. Fix cases the HARNESS got wrong first (a rubric the world cannot satisfy, an `expect_writes`
   field name that does not exist) before blaming QAR. `judged.verdict.summary` and the pre-check
   `detail` strings say which.
3. Extend `PIVOTS` and the world together (add the fact to the seeded data AND to `PIVOTS`), then
   `reset`; never hand-edit dev data.
4. File QAR/quest-backend defects as findings with the case id, and re-run only those ids.
