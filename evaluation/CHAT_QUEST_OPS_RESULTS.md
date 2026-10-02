# Quest AI chat: quest-operation routing + execution eval (2026-10-02)

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
