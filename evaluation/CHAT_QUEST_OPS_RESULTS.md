# Quest AI chat: quest-operation routing + execution eval

## Re-run and fixes, 2026-10-06

Same in-app surface (`POST /api/quest-ai/conversations/{id}/messages/stream` on the dev backend,
which uses a cheap Gemini flash-lite tier), with the harness fixed first so the numbers are
honest:

- **Delegation detector.** The backend's frame is `{"event": "delegated", "task_id": ...}` (the id
  sometimes only on the done frame's `pending_undo`); both are read now, so short-circuit hand-offs
  count as deep.
- **Routes, not planner actions.** A planner `deep` in Quest's chat usually runs IN-PROCESS
  (generated code over Quest data). Each turn is classified `delegated` / `inline_write` /
  `inline_code` / `answer`; only `delegated` left the process.
- **Verifiers read what the app shows**: Today's Actions for habits, the timer route, the
  daily-reflection collection (local or UTC date), parked approvals by their executable kind.
  `selftest` proves every write verifier with a negative and a positive control through the app's
  own routes.
- **Hygiene.** One conversation per case, deleted at teardown (left behind, they outlive the
  fixture quest and become context for later runs); delegated tasks cancelled at once; a dev-host
  allowlist (`QAR_EVAL_DEV_HOST`); 429 retries. Since 2026-10-06 an API key cannot create a quest,
  so the fixture quest comes from an operator factory command named in env (quest-backend
  `scripts/checks/eval_fixture_quest.py`).
- **Dataset**: 50 cases (the original 22 ids unchanged plus 28), tagged read / list / write /
  inform / contrast, and an approval-card arm (`--auto-run off`): a write must be held before
  "Yes, go ahead." and applied after it.

Per Joshua's rule of the same day (no full eval runs without his approval), the baseline is ONE
pass of the 50 cases (it was stopped before its second pass), and the after-run is ONE pass on a
paired, stratified 24-case subset plus a 4-case approval arm, then a 6-case re-run after the last
fixes. All on dev, fixture data verified gone after every pass. Single passes are noisy: a case
that flips once is a signal, not proof.

### Baseline (one pass, 50 cases, before today's fixes)

| Metric | Result |
|---|---|
| Routing correct | 47/50 (LA1 and G7 handed off, X5 answered instead of handed off) |
| Plain operations kept in-process | 44/45 non-contrast cases (G7 queued a background task); LA1's hand-off was an extra "check" task after a full answer |
| Writes that really landed (verified in the app's own reads) | **7/23** |
| Reads / listings correct | 15/22 |
| Turn latency | median 16.1s, p90 52.0s, max 497.3s (G5 rename) and 387.5s (G6 complete) |
| Turns hitting the 15-step read cap | 2 |

What failed, by root cause (each traced to code, not guessed):

1. **Habit "done" landed where the app does not look.** `log_habit` wrote a legacy
   `HabitCompletion` row; the app's Done button writes the period entry through
   `POST /api/data/entries`. H1-H4 replied "done" while Today's Actions showed not done.
2. **No helper for notes or timers**, so code wrote a note into `preferences`, or said it could not
   start a timer.
3. **Habit and collection lookups were not scoped to the quest.** The quest's
   `habit_collection_ids` mirror is empty for habits made in the app, so generated code fell back to
   the first account-wide name match and logged ANOTHER quest's habit. "Which habits am I tracking"
   answered "none" with four linked.
4. **Unknown entry fields were accepted** (`distance` for `distance_km`), and the reply said saved.
5. **Raw writes hung to the 120s timeout.** pymongo's write result is not JSON serializable, so the
   sandbox RPC reply was dropped; the critic then re-ran an already applied write. This is the
   whole of the 497s and 387s turns.
6. **A read forced into a write**: "how many km did I **log**" matched a substring guess.
7. **Field writes on an autopilot-off quest** ran, were refused by the AI field-write gate, and
   replied "I tried, but it did not work" (or "I'm not sure what you'd like me to do").
8. **Creating a reflection was blocked** by the guard meant for changes to existing documents.
9. **Context bleed**: a deleted quest's conversations became "general" and grounded other quests'
   answers; an Oct 2 eval turn had left a rep learned note ("set the goal to a sub-48-minute 10K")
   that still rewrote answers about other quests.

### Fixes (quest-backend unless noted; all on `october`)

| Fix | Commit |
|---|---|
| `log_habit` writes through the app's own entry path and re-reads it; `start_habit_timer` / `stop_habit_timer` / `add_quest_note` helpers over the routes' shared code | 36550a77 (swept into a concurrent commit), 860ca623 |
| Daily reflection save clears the entry-list cache | 860ca623 |
| A deleted quest's conversations stay fenced out of other quests | a4151f7d |
| Quest-scoped habit/collection lookup (`get_quest_collections`), unknown entry fields refused, quick recordings stay inline, listings read fresh, YOUR RESPONSIBILITIES is not the goal list, raw writes return counts instead of hanging | fbe91cc7 |
| No "I tried N versions of the code" machinery in replies | 15077aaa |
| Raw writes clear the caches the app reads | 3a3715e2 |
| Structured read/write verdict from the classifier (`CODE_WRITE`), field writes on autopilot-off quests ask first, creations pass the document guard, quest collections readable by the planner | c834e6da |
| Code generation sees the quest's own collections and field ids | 87dda628 |
| `entry_date` accepted on any collection | 48dc5ca8 |
| Read answers see the whole result (was the first 1000 chars of a repr) | f8543ba7 |
| Planner grounding names the quest-collections read | 675631a6 |
| QAR: a short message that names its own subject is never a clarifying question ("Summarize my latest daily reflection.") | 6ef8e6b |
| Critic-pass fixes: exact re-read by id, minutes under target marked started, undo scoped to the named habit (was the whole entries collection), upsert reported as created, guard exemption narrowed | 85d10a83 |

The classifier's write verdict was checked on 30 labelled messages with the real fast-tier model
(`scripts/checks/check_classifier_write_verdict.py`): 30/30 intent, 30/30 read/write.

### Paired before/after, same 24 cases, one pass each

| id | kind | before route | before | before s | after route | after | after s | after the last fixes |
|---|---|---|---|---|---|---|---|---|
| R1 | read | answer | Y | 12.0 | answer | N | 9.8 | N (stale rep note, removed after) |
| R4 | read | answer | N | 27.4 | answer | Y | 8.6 | |
| L1 | list | answer | Y | 34.4 | answer | N | 9.3 | Y 23.3s |
| L2 | list | inline_code | N | 32.5 | answer | N | 10.5 | Y 10.8s |
| L3 | list | answer | N | 26.4 | inline_code | N* | 20.1 | |
| LA2 | list | answer | N | 30.9 | inline_code | Y | 19.6 | |
| LA5 | read | answer | N | 13.7 | inline_code | N* | 19.4 | |
| SC1 | list | answer | N | 34.0 | answer | Y | 34.2 | |
| C1 | inform | answer | Y | 9.4 | answer | Y | 10.5 | |
| D1 | write | inline_code | N | 13.0 | inline_write | N* | 13.6 | Y 17.0s |
| D2 | read | inline_code | Y | 49.8 | inline_code | Y | 22.9 | |
| D3 | write | inline_code | N | 16.1 | inline_write | N* | 15.2 | N, handed off as a task |
| H1 | write | inline_write | N | 22.6 | inline_write | Y | 15.2 | |
| H2 | write | inline_write | N | 52.0 | inline_write | Y | 17.1 | |
| T1 | write | answer | N | 11.3 | inline_write | Y | 12.1 | |
| T2 | write | answer | N | 9.5 | inline_write | Y | 11.1 | |
| E3 | write | inline_write | N | 12.0 | inline_write | N | 15.7 | Y 13.0s |
| G3 | write | inline_write | N | 12.6 | inline_write | Y | 13.6 | |
| G5 | write | inline_write | Y | 497.3 | inline_write | Y | 13.6 | |
| G7 | write | delegated | N | 16.0 | inline_write | Y | 11.4 | |
| F1 | write | inline_write | N | 10.7 | inline_code | Y | 16.4 | |
| F3 | write | inline_write | N | 17.6 | inline_code | Y | 12.6 | |
| X1 | contrast | delegated | Y | 2.1 | delegated | Y | 2.0 | |
| X3 | contrast | delegated | Y | 2.1 | delegated | Y | 1.7 | |

`*` verifier artefacts, read with the reply: L3 summed Oct 4 to 7 for "the last few days" (13.2 km,
correct for that window; the verifier wanted the Oct 2 entry too); LA5's fixture habit rows are all
dated today by the entries route (a backdated habit entry needs `entry_date`), so "0 in the last
three days" is what the data says; D1/D3 saved the reflection under the account's UTC date, which
the verifier did not accept until 3c07ad0. F1/F3 pass as "held for approval, reply honest": the
quest's autopilot is off, so the change is on an approval card.

| Metric (24 paired cases) | Before | After |
|---|---|---|
| Correct | 7/24 | 16/24 in one pass; 20/24 after the last fixes and verifier corrections |
| Routing correct | 23/24 | 24/24 (D3 handed off once in the last re-run) |
| Writes correct | 1/12 | 9/12 in one pass; 11/12 after the last fixes (D1 and E3 passed, D3 did not) |
| Median / max turn | 16.1s / 497.3s | 13.6s / 34.2s |
| Step-cap hits | 2 (of 50) | 0 |

Approval-card arm (`auto_run=false`, the app's default), 4 write cases: H1, G3 (note) and F1
(current state on an autopilot-off quest) were each held before "Yes, go ahead." and applied after
it. E1 (journal entry) was held but did not land after the yes until 48dc5ca8; re-run, it was held
and then applied.

### Still open

- **D3 hand-off.** "Here's today's reflection: ... heading into the weekend taper. Save it." was
  once queued as a background task by the pre-brain task detector (probably reading "weekend" as a
  deferral) despite the new inline rule. One flip in two runs.
- **Rep learned notes are account-wide.** Since 2026-10-05 the feedback judge declines facts, but a
  style note learned in one quest's chat still applies in every quest's chat. The eval account's
  rep also carries older notes from other testing (marathon, knee) that colour unrelated replies.
- **Pre-existing deleted-quest conversations stay general.** Nothing left in the data ties them to
  the quest they were about (checked on dev: 0 of 225 quest-less conversations recoverable), so no
  backfill was possible.
- **Retry after a partial write.** When generated code writes, then fails later in the same
  program, the critic re-runs the whole program. The write helpers are now idempotent-ish (period
  upserts, re-reads), but a raw `insert` is not.

---

# Original run (2026-10-02)

Harness: `evaluation/chat_quest_ops_routing_eval.py`
(`setup` / `run` / `run-inapp` / `teardown`). Measured on **dev**
(`api.batmanhq.duckdns.org`), one disposable fixture quest built through the app's own REST
endpoints, torn down and verified gone afterwards.

## The question

Two things at once, for a Quest subscriber who has **no external environment** (no machine, no
Claude Code):

1. **Routing.** Do plain quest-database operations stay **inline**, instead of being handed off to
   a deep run? And does work that genuinely needs an external environment still go to deep?
2. **Execution.** For what it does perform inline, is the operation actually **correct**, verified
   independently against the Quest API rather than believed from the reply text?

## Dataset: 22 cases over 11 areas

| Area | Cases | Expected |
|---|---|---|
| Read quest fields (outcome, measurable outcomes, current state, acceptance criteria) | R1-R4 | inline |
| List / query quest data (goals, habits, collection entries) | L1-L3 | inline |
| Daily reflection through chat | D1, D2 | inline |
| Mark a habit complete (binary + timer habit) | H1, H2 | inline |
| Habit timer start | T1 | inline |
| Add a collection entry with specific field values | E1 | inline |
| Create a goal / update a goal / add a quest note | G1, G2, G3 | inline |
| Quest field write (current state, outcome) | F1, F2 | inline |
| Inform only (how does Quest work) | C1 | inline |
| CONTRAST: code change, corpus file research, run tests on a machine | X1-X3 | **deep** |

The arm reported here is **`run-inapp`**: the real in-app Quest AI chat surface
(`POST /api/quest-ai/conversations/{id}/messages/stream`), driven over HTTP with
`auto_run=true` (a user who has pressed "Allow all"), with routing read off the SSE
`plan` / `exec` / `task_queued` frames. That is the surface a subscriber actually talks to:
quest-backend builds its own Orchestrator, not QAR's library one.

## Headline results

| Metric | Result |
|---|---|
| Routing correct | **14/22 (64%)** |
| Deep contrast cases routed to deep | **3/3** |
| Plain quest operations kept inline | **11/19** |
| Plain quest operations over-routed to deep | **8/19** |
| Write operations that completed **inline** | **0/10** |
| Write operations that completed at all (via the delegated environment) | 2/10 (G1, G2) |
| Quest-field reads correct | **4/4** |
| Listing / querying correct | **0/3** |
| Slowest single turn | 274.5s (L3, "how many km did I log") |

**Deep routing is not the problem. Inline quest operations are.** Every genuinely-deep case was
dispatched correctly and fast (1.7s-11.2s). The failure is on the other side: eight plain quest
operations were sent to an external environment, and not one write succeeded inline.

## Per-case

| id | area | expect | routed | correct | secs | what happened |
|---|---|---|---|---|---|---|
| R1 | read quest fields | inline | answer | yes | 73.1 | returned the outcome verbatim |
| R2 | read quest fields | inline | answer | yes | 27.2 | both measurable outcomes |
| R3 | read quest fields | inline | answer | yes | 25.6 | current state |
| R4 | read quest fields | inline | answer | yes | 16.3 | acceptance criteria |
| L1 | list quest data | inline | answer | **no** | 47.6 | listed 2 of 3 goals, dropped the month-scope goal |
| L2 | list quest data | inline | answer | **no** | 24.5 | described the habit but never named it |
| L3 | list quest data | inline | answer | **no** | 274.5 | never returned the logged distances (8.2, 12.5) |
| D1 | daily reflection | inline | **deep** | **no** | 62.6 | reflection reached no quest note |
| D2 | daily reflection | inline | answer | yes | 198.6 | answered, but from an unrelated April 2026 review |
| H1 | habit completion | inline | answer | **no** | 93.1 | claimed inline, habit row still `completed=False` |
| H2 | habit completion | inline | answer | **no** | 165.9 | no timer-habit row for today was created |
| T1 | habit timer | inline | **deep** | **no** | 22.1 | honestly said it cannot start a timer |
| E1 | collection entry | inline | **deep** | **no** | 33.2 | no Run Log entry with distance 10.4 exists |
| G1 | goal create | inline | **deep** | yes* | 153.2 | goal created, but only via the environment |
| G2 | goal update | inline | **deep** | yes* | 12.9 | goal marked complete, only via the environment |
| G3 | quest note | inline | **deep** | **no** | 16.4 | quest still has 0 notes |
| F1 | quest field write | inline | **deep** | **no** | 15.3 | `current_state` unchanged |
| F2 | quest field write | inline | **deep** | **no** | 10.8 | `outcome` unchanged |
| C1 | inform only | inline | answer | yes | 11.9 | correct explanation, no work created |
| X1 | contrast: code | deep | delegated | yes | 1.8 | real task `atask_4303d9a2f4d5` queued |
| X2 | contrast: files | deep | deep | yes | 11.2 | real task `atask_1ded0161c9c8` queued |
| X3 | contrast: machine | deep | delegated | yes | 1.7 | real task `atask_6b296780631c` queued |

`*` G1 and G2 only completed because this dev account **has** an external environment attached.
On an account without one, both are operations that would simply not happen.

**Scoring correction, stated openly:** the harness scored X1 and X3 as routing failures because
its `delegated` detector only watches for a `task_queued` SSE frame, and those two turns
short-circuit straight to delegation with no `plan` frame at all (1.7-1.8s, `actions=[]`). Their
replies said "I sent this to your environment", and the dev `assistant-tasks` list confirms all
three contrast tasks were really created. So deep routing is 3/3, not 1/3, and the raw harness
summary (12/22) understates routing; the corrected figure is 14/22. The `delegated` detector needs
fixing before the next run.

## Qualitative problems found

**1. Every write over-routes to deep, or silently does nothing.** Adding a note (G3), adding a
collection entry (E1), recording a reflection (D1), creating a goal (G1), completing a goal (G2)
and both quest-field writes (F1, F2) were all handed to an external environment. For a subscriber
with no environment, that is the whole write surface of the product unreachable from chat. The two
habit writes (H1, H2) did stay inline, and are worse: the reply reads as success while the habit
row stays `completed=False` and no timer row is created at all.

**2. Quest field writes bypass the dedicated field-update tool.** F1 ("update the current state")
and F2 ("change the outcome") are exactly what `update_quest_fields` exists for, and both are
unambiguous explicit user requests for that field. Both went to a deep run instead and neither
field changed. This is the same class of bug as the AI-generated-code field-write issue fixed on
2026-10-02: the governed route exists, the chat brain is not reaching for it.

**3. A listing question later in a conversation answers from stale conversation context.**
L1 ("list the goals on this quest") ran with **zero** read steps (`actions=['answer']`) after R1-R4
had filled the conversation, and returned two of the three goals, dropping the month-scope
GOAL-C. Re-asking the identical question in a **fresh** conversation (`qaconv_4e89939dc218`) did 15
read steps and returned all three. So the omission is conversation-context reuse standing in for a
fresh read, not a retrieval or period-filter bug.

**4. Reads are correct on quest fields and unreliable on anything collection-shaped.** The four
quest-field reads were 4/4 and fast. All three listing/aggregation reads failed: the goals list
dropped a goal, the habit list never named the habit, and the distance question never returned the
numbers that were sitting in the collection.

**5. Latency is a real part of the experience.** Median turn 47s; L3 took 274.5s and D2 198.6s for
what a user reads as one simple question. Several correct reads (R2, R3, L2, and the fresh-
conversation goals probe) burned all 15 read steps, i.e. hit the step cap rather than finishing
early.

**6. Cross-quest bleed on reflection history.** D2 ("what did I write in my most recent week
review") answered from an unrelated 28 April 2026 review belonging to the account, not to the
quest under test. Scored correct by the verifier (it answered), but it is the wrong scope.

## Recommended fixes, in priority order

1. **Keep every quest-database write inline.** A note, an entry, a goal, a habit completion and a
   quest field are all single governed API calls. None should ever reach the deep-run classifier.
   The classifier needs an explicit allow-list of quest-operation intents that cannot be deep.
2. **Route quest-field requests to `update_quest_fields`.** An explicit "change my outcome /
   current state" must call the governed field-update tool inline, with
   `user_asked_for_this_field=true`, never a deep run and never generated code.
3. **Make a failed inline write say so.** H1 and H2 reporting success while nothing was written is
   the worst outcome in this whole run. Verify the write and report honestly when it did not land.
4. **Force a fresh read for listing questions.** Do not let accumulated conversation context
   satisfy "list my goals"; it produced a silently incomplete list.
5. **Fix collection reads** (naming collections, aggregating entry field values).
6. **Scope reflection history to the quest** in question.
7. **Fix the harness `delegated` detector** to count a no-plan delegation turn, so the next run
   scores deep routing correctly without a manual correction.

## Honest limitations

One dev account, one run per case, no repeats, so per-case latency and any single verdict carry
run-to-run variance. 22 cases, not the 50 the quest's acceptance criteria name. `auto_run=true`
throughout, so the approval-card path (the default, `auto_run=false`) is untested. The T1 verifier
is deliberately always-false (it captures the reply for reading rather than asserting), so T1's
"correct" column is not a real pass/fail. The no-external-environment claim is measured as the
routing decision plus the verified absence of the write; the dev account used does have an
environment attached, which is why G1 and G2 completed.
