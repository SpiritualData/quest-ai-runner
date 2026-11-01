# Incremental replies and asks: say what is done, why more is coming, and ask when blocked

Status: phases 1 to 4 built on the runner and backend sides, shipped OFF (section 10 is the wire contract). Original design below; every "today" claim was read from code. Every "today" claim was read from code in the named files (QAR is this repository;
backend and frontend are the Quest repositories). What was not read or run is in section 9.

## 1. The requirement
In Quest AI chat, solo and team: (1) when the goal is not met and more work remains, show the output so far, say more is
being done and why, then post the next response as a SEPARATE message, repeating within a stop rule; (2) when BLOCKED,
create and show an ask (the existing "Asks for you" decision-request), by default for the person who gave the input,
in a team chat possibly a teammate; (3) collapse earlier incremental messages of one input behind an expand control.
Binding standards: every message adds value (no filler status lines); decisions come from the model, prompt or
structure, never from words in model output; no new model call unless it earns its cost.

## 2. What happens today (read from code)

### 2.1 A turn whose goal is not met
- Answer turns: the verifier judges the draft (`_verify_goal`, schema in `core/auxiliary_prompts.py`). The verdict has
  `met`, `reason`, `next_action`, `blocker` (`more_work | evidence_only | needs_person | tool_failure`), `question`.
  An unmet turn returns the answer anyway with `exit_reason="max_turns"` (`core/orchestrator_run_close.py`). The verdict
  shows as ONE live status line, "<reason> To complete: <next>" (`core/orchestrator_run_loop.py`): not a message, gone on reload.
- Deep turns: the goal loop (`core/orchestrator_deep_run.py`) emits per unmet attempt a milestone "Worker output" and a
  status "Goal not met: <reason>", then retries, steps up a tier, resumes the session (`deep-run-continuation.md`) or
  widens context. Stop rules exist: attempt cap (`deep_goal_max_iterations`), token budget, `stalled` (same point
  twice), `tool_failure` (same failure twice), the coverage gate (`coverage_gap`, `continue_decision`).
- Queued tasks: `_follow_up_while_incomplete` (`runner/auto_followup.py`) runs up to 3 extra runs while the ask is
  unfinished and nothing waits on a person; the person sees only a task-feed status line.
- Chat hears a delegated task twice: "started" and the closing message. Milestones are kept OFF the chat on purpose
  (`runner/executor_reporting.py` `_on_milestone`; incident 2026-09-27: an unverified "fixed" claim from a rejected
  attempt reached the chat). Team threads accept only closing kinds (`TEAM_THREAD_TASK_KINDS`, backend `team_chat.py`).

### 2.2 A second message already exists, in one narrow case

QAR emits `EVENT_INTERIM` (hub `ports/events.py`; `interim_text` and the emit at the end of the goal loop in
`core/orchestrator_deep_run.py`). The backend stores it as `kind="interim"`
(`app/api/endpoints/quest_ai/conversations.py`; `app/business/quests/quest_ai_interim.py`); the app shows a "Still
working" pill and coverage line (`InterimNote.tsx`, `src/utils/interimMessages.ts`, `QuestAIScreen.tsx`). Narrow in four ways:

1. It fires only when `done_evidence_enabled` is on (backend `quest_ai_done_evidence_enabled`, default False), the blocker
   is `more_work`, there is output, and the plan is an "analysis" or the run declared coverage.
2. The text is partial output + a coverage line + "Still working: <next_action>". The WHY is not in it: the reason goes
   only to the live status line.
3. When the final lands the interims vanish: the app drops them (`collapseTurnInterims`) and the server marks them
   `superseded`, hidden on reload (`supersede_interim_messages`). No expand.
4. Team chat drops it: `stream_send_message` (`app/business/conversations/conversations.py`) and
   `app/api/endpoints/conversations.py` handle only status, exec, result, work_trace, explanation.

### 2.3 Blocked work does not reliably become an ask

- `blocker="needs_person"` sets `DeepResult.needs_person` (deep loop). From grep, nothing in `core/` turns it into an
  escalation. In a live chat turn the person gets the worker output and no ask.
- In the queued-task lane `runner/executor_reporting.py` posts "I need one thing from you: <question>" as a chat message
  of kind `decision` and reports `needs_you` with an EMPTY decision id: the text looks like an ask, but no
  decision-request row exists, so nothing appears in "Asks for you" and nothing resumes on an answer.
- Real asks come from the planner's `clarify` and `confirm` actions (`core/orchestrator_understanding.py`) and the
  `create_decision` tool (`core/tools_quest_ops.py`), which takes only summary, kind, review link and quest: it cannot
  name a teammate. `Escalation.assignee` exists as a routing key but the planner never fills it.

## 3. The Asks for you feature, end to end

- Model: `TeamDecisionRequest` (backend `app/models/team.py`): `assigned_to_user_id`, `kind`, `summary`,
  `default_on_silence` (hold, proceed, ai_answer), `deadline`, `status` (open, resolved, expired, withdrawn), `resolution`,
  `response_text` (free text), `notify_user_id` (told the answer), `conv_id`, `quest_id`, `executable`, `requester_kind="ai"`.
- Create: `team_operations.create_decision_request` (`dedupe_key` = one OPEN ask per key). HTTP
  `POST /api/teams/{team_id}/decisions`; the assignee must be a team member (422). From a chat turn:
  `QuestDecisionEscalationSink.escalate` (`quest_ai_escalation_sink.py`): default assignee is the rep's human, or the
  requester for an action they asked for; dedupes on (conversation, capability).
- Resolve: `POST /api/teams/decisions/{id}/resolve` (assignee only, delegates allowed), then `finalize_decision_resolution`
  runs a parked executable or resumes the paused task (`resume_task_after_decision`, returns `follow_up_task_id`).
  Withdraw, answer-seen, for-user, for-quest and counts endpoints exist.
- Screens (quest-frontend): the ledger (`src/components/decisions/*`); in chat, `components/chat/ai/ApprovalCard.tsx`
  (approve, decline, `RoutingChoiceCard` option buttons).
- In chat today: solo chat loads the quest's open decisions that are assigned to the viewer and shows them as cards in a
  gutter at the bottom of the list, not under a message (`QuestAIScreen.tsx` `loadPendingDecisions`). Team chat attaches the
  freshest open decision for the sender to the AI reply (`_latest_open_decision`, `TeamAiDecoration.tsx`). Both are
  approve/decline cards. I found no chat-native free-text answer box for an "I need X from you" ask (not exhaustively checked).
- Two keyword-rule breaks on the way: the sink drops "phantom" asks by matching phrases in the model's summary
  (`_NON_ACTIONABLE_ESCALATION_MARKERS`), and the card parses ask text (`askHeadline`, `askOptions`).

## 4. How messages are stored and grouped

- Solo chat: messages sit in the conversation document (`quest_ai_storage.add_message`) with no turn id; the app treats
  "everything after the last user message" as the turn. `run_id` exists only for task messages.
- Team chat: team-chat documents; the AI reply points at its input with `reply_to_id`, and each stored message is
  published to all participants over the sync bus (`_publish_conversation_message`), so several messages per input are
  already deliverable. The asker's stream carries one reply.
- Existing collapse patterns: `taskProgressSupersession.ts` (hides), `collapseTurnInterims` (drops), the Trail
  (`WorkTrace.tsx`, one per turn, expands to a rail), `AnswerExplanation`, `CollapsibleMessageText` (uncommitted in
  another lane, only referenced). Model history excludes interims on purpose (`without_interim`); keep that.

## 5. Shared components and the solo/team split

Solo chat is `QuestAIScreen.tsx` (about 6,000 lines, own message loop). Team chat is `ConversationScreen.tsx` with
`ChatMessageList.tsx`, `conversationChatAdapter.ts`, `TeamAiDecoration.tsx`. Shared: `ApprovalCard`, `RoutingChoiceCard`,
`WorkTrace`, `AnswerExplanation`. Not shared: interim rendering, card placement, stream frames. They have diverged
before, so each rule goes in one pure util (`groupTurnMessages`), one mounted component, one backend persist function.

## 6. Design

### 6.1 Message-kind contract

Every message for one input carries `turn_id` (the input message id; in team chat the existing `reply_to_id`) and `seq`.
Ids are `turn_id:seq`, so a replayed stream (the app re-posts with `client_message_id`) cannot duplicate. Three kinds:

| kind | meaning | required fields | must never |
|---|---|---|---|
| `interim` | work continues | `done` (what exists now), `why_more` (the gap, from the verifier `reason`), `next` (verifier `next_action`), optional `coverage`, `will_continue: true` | be last in a turn, repeat the previous interim, carry a claim the run's record does not support |
| `final` (today's reply) | the turn ended | text, `outcome`: `met` or `stopped` with `stop_reason` (`budget`, `stalled`, `attempt_cap`, `tool_fault`) and what is left | say more is coming |
| `ask` | blocked on a person | `decision_id`, `assigned_to`, `question`, `why_only_you`, `done` so far, `options?` | exist without a real decision-request row |

Wire: `interim` and `final` reuse existing frames; `ask` reuses `EVENT_DECISION`. No new hub event type; `interim`
data gains `turn_id`, `seq`, `why_more`, `next`.

### 6.2 When an interim is worth sending

All inputs are structural and already on hand at the point where the goal loop commits to another attempt (the existing
emit site, after the budget check). Send only when ALL hold:

1. The verifier said `met=false`, `blocker=more_work` (a model decision read as an enum, not words).
2. The loop WILL run again: not `stop_after_this`, budget and attempts left, not stuck, no open ask. "More is coming"
   is then true by construction: the message is emitted after the decision and before the launch.
3. There is a delta since the last message: coverage advanced, a new write or tool receipt, or new output content.
   No delta, no message; the Trail keeps showing live work.
4. Bounds: at least `min_seconds_between` since the last message, at most `max_interims_per_turn` (proposed 15 s and 4,
   both settings). Past the cap progress stays in the Trail; the final or ask still comes.
5. `done` is built from receipts and declared coverage. The raw worker claim is shown only when no claim check flagged
   it (`claims_unexecuted`, `claim_corrected`); otherwise only receipts. This answers the 2026-09-27 incident.

`why_more` and `next` come from the verdict the loop already paid for: no new model call. Optional arm: the same verdict
call also returns a `worth_showing` boolean (a model decision, allowed; string matching on the reply is not).

Stop rule: the mechanism adds no loop. It reports the existing one, so every bound in 2.1 still ends it, and the last
attempt produces the final, never an interim.

### 6.3 The blocked ask

Blocked means `blocker=needs_person` with a `question`, plus `tool_failure` only when the ledger class is a refusal a
person can fix (access, credential, permission). A platform fault ends as a `final` with `stop_reason=tool_fault`;
stalled or out of budget also end as a final with a Continue button (exists for analyses). Neither is an ask.

Flow:
1. Code creates the decision once through the escalation port: `dedupe_key = turn_id` (one open ask per blocked turn, however the gap is worded; the deep worker is told not to raise its own decision-request in a thread turn), `conv_id`, `quest_id`,
   `default_on_silence=hold`, `notify_user_id = asker`. If creation fails the final says the question could not be
   filed, never that an ask exists.
2. The turn emits `ask` (`EVENT_DECISION` with the id) after posting `done` so far in the same group.
3. Who: the default is the asker. The verifier schema gains `asked_of` (`asker | member`) and `member_ref`; the model
   picks from the participants list it is given, and code resolves `member_ref` against human participants who are team
   members (the endpoint returns 422 otherwise). Unresolved falls back to the asker and the card says so. The asker
   stays `notify_user_id`. The `create_decision` tool gains `assignee` with the same resolution (`resolve_member`
   exists in `core/tools_quest_ops.py`).
4. Card in chat (replaces the bottom gutter in solo and the attached card in team): the question, one line "why only
   you", what was done (link to the group), `options` as buttons when structured, a free-text box (`response_text`),
   the silence default and deadline, Answer and "Not now". Rendered from fields, not parsed text; one shared
   `AskCard` variant of `ApprovalCard` in both screens.
5. Answering uses the existing resolve call; the work resumes via `resume_task_after_decision`. The resumed turn keeps
   `turn_id` and continues `seq`, so the next message lands in the same group and the card folds to "You answered".
6. Others: the assignee and the asker see the card (the asker sees "Waiting on <name>" and can withdraw it). Everyone
   else sees "Waiting on <name>" with no question text, unless the question uses only quest data visible to all humans
   in the thread (`_visible_to_all_humans`). Same rule as the Trail: private records never reach other people.

### 6.4 Collapse and expand

- Grouping comes from `turn_id` by one pure util: identical live, on reload and in both screens. Only the newest
  message and the Trail live line are polite live regions.
- Rule: the LAST message of a group (interim, ask or final) is open; all earlier interims fold into one button row
  "Earlier updates (n)". Expanding shows each compactly (long ones via `CollapsibleMessageText`); expansion is local
  view state. A group of one is unchanged.
- This replaces `superseded` hiding: interims stay when the final lands. Keep writing `superseded` for old app builds.
- The Trail: one per turn, under the latest message (live line, then settled summary, expandable rail). Folded rows carry
  none; the live line follows the newest message and the settled summary stays with the final.
- Notifications: an interim must not push or raise unread counts; only the ask (to its assignee) and the final do.

## 7. ESOI framing

- Problem: existing `prob-long-run-silent-until-the-end`, plus a proposed sibling: a turn that cannot finish ends with a
  bare answer or text that only looks like an ask, so people cannot tell whether work continues or what is needed.
- Solution seats: extend `show-progress` (what is shown, and when) and `escalate-to-a-human` (who is asked, once).
  Proposed implementation names: `reply-in-steps`, `ask-on-block`.
- Ideals: `ideal-incremental-output-while-working`, `ideal-asks-only-what-is-theirs`, proposed
  `ideal-never-left-guessing` (always know whether work continues, why, and what only you can do).
- Arms (policy): A0 today (interim only with done evidence and analysis); A1 every unmet attempt (spam baseline); A2 the
  gate of 6.2; A3 A2 plus a model `worth_showing`; A4 A2 without the cap. Arms (ask): B0 today; B1 ask on the blocker
  enum, asker only; B2 B1 plus `asked_of`. Arms (view): C0 hide at final; C1 collapse and expand, judged by hand and
  screen checks, not the model cascade.
- Metric (proposed id `metric-interim-reply-truthful-and-useful`), cascading conditionals in the repository syntax:
  `offline [sample=full | real=no | gate=all-pass | when=scripted verdict and event sequences through the policy and
  the message-kind contract, plus reference_good and reference_bad arms, no model] > one [sample=sample:1 | real=yes |
  gate=all-pass | when=one real unmet turn on haiku via claude_cli, hand read] > ten [sample=sample:10 | real=yes |
  gate=all-pass] > thirty [sample=sample:30 | real=yes | gate=thresholds below | when=final tier, haiku through
  claude_cli, which the subscription bills at no money, so cost is usage headroom and wall clock]`.
- Cases (disposable world): second pass; large set with coverage; met first time (zero interims); missing credential;
  person-only decision; ambiguous owner in a team thread; tool fault (final, no ask); stuck loop (stop, final).
- Checks, all computed from the turn record (events, stored messages, ledger, decision rows), none from reply wording:
  1. More-coming truth: every interim is followed, in the same turn, by another attempt start and a later message.
  2. No dangling end: every turn ends with exactly one final or one ask; no interim is last.
  3. Ask iff blocked: a decision row exists exactly when the case is blocked by construction (precision and recall);
     at most one open ask per gap; assignee valid; notify is the asker.
  4. Delta and bounds: no two consecutive messages with equal structure; count and spacing within bounds.
  5. Why present on every interim; no em dash in any user-visible string; each `done` item maps to a receipt or coverage.
  6. No harm: met rate and tokens per met turn no worse than A0 within noise (repeat runs, whole set).
  7. Value: a blinded judge on a different model ("does this tell the reader something the previous one did not"): a
     reading, never the gate. Thresholds are unset; set them from the offline floor and the first 10 real turns.

## 8. Phased build plan (smallest first)

Phase 1, the smallest slice, QAR only, dark: a pure `core/interim_policy.py` (`decide(state) -> send | skip, reason`),
settings `interim_replies` (off), `max_interims_per_turn`, `min_seconds_between` in `core/orchestrator_types.py`, the emit
site in `core/orchestrator_deep_run.py` calling it and adding `why_more`, `next`, `turn_id`, `seq`; offline cases under
`evaluation/components/` and tests under `tests/`. Metric tier 1 runs here.

Phase 2, backend persistence for both chats: fields in `app/models/chat_message.py`, `app/storage/quest_ai_storage.py`,
`app/business/quests/quest_ai_interim.py`; one persist function used by `app/api/endpoints/quest_ai/conversations.py` and
`stream_send_message` (`app/business/conversations/conversations.py`); map the frame in `app/api/endpoints/conversations.py`;
stop superseding; interims silent in `_notify_new_message`; a setting beside `quest_ai_done_evidence_enabled`
(`app/config.py`). Endpoint test with real handler and Mongo.

Phase 3, frontend view: `groupTurnMessages` (`src/utils/interimMessages.ts`), an `EarlierUpdates` row, `InterimNote.tsx`
with why and next, types in `src/services/sseStream.ts`, mounts in `QuestAIScreen.tsx` and
`ChatMessageList.tsx`/`conversationChatAdapter.ts` (coordinate with the lane holding `ChatMessageList.tsx` uncommitted),
Trail placement, locale strings, and `quest-docs` in the same change.

Phase 4, asks: verifier `asked_of`, `member_ref` (`core/auxiliary_prompts.py`, `core/orchestrator_judges.py`); a
`needs_person` turn creates the decision and emits `EVENT_DECISION` (`core/orchestrator_deep_run.py`); the task lane
creates a real decision (`runner/executor_reporting.py`); `assignee` on `create_decision` (`core/tools_quest_ops.py`,
`runner/quest_client.py`); backend sink uses the structural blocker, `dedupe_key`, `notify_user_id`
(`quest_ai_escalation_sink.py`); frontend `AskCard` variant inline in both screens plus the "Waiting on" row.

Phase 5: a task's verified interim reaches its chat via the same contract (never the raw "Worker output" milestone);
`TEAM_THREAD_TASK_KINDS` admits it under the same caps. Phase 6: tiers 2 to 4 on haiku via claude_cli, select arms, flip
defaults on dev first. Add this page to `docs/README.md`.

## 9. Risks and unverified items

Not verified (settle before Phase 2):
- Whether the solo app knows the user message id when an interim arrives (for `turn_id`); if not, the server stamps it.
- Where `resume_task_after_decision` output lands in a chat, and whether the turn group survives it.
- `_notify_new_message` behavior for a new `kind` (push, unread count, digests), and how old app builds render it
  (assumed a plain assistant bubble).
- Multi-rep turns need the rep id in the group key; a new human message mid-run is proposed to start a new `turn_id`
  and leave the old group open (`receive-input-mid-run` seat); untested.

Risks:
- Showing an unverified claim again (2026-09-27): receipts-first `done` and claim flags help; a wrong verifier reason
  remains possible. Haiku may fill `blocker` and `asked_of` unreliably (tiers 3 and 4 measure it).
- Team spam (caps, delta rule, silent notification; the numbers are a judgement call); wrong person asked (unresolved
  refs fall back to the asker, the card names who); ask fatigue (`dedupe_key`, no re-ask of a declined question).
- Process dies after an interim: next open must read "stopped early" (mark, do not delete).
- Existing keyword gates (phantom-ask phrases, `_answer_describes_unexecuted_work`) sit on this path; add none.

## 10. Wire contract as built (phases 1 to 4, runner and backend)

Switches, all default OFF: QAR `OrchestratorConfig.interim_replies` (no env var, like `done_evidence_enabled`; the
consumer sets it), with `max_interims_per_turn` (4) and `min_seconds_between_interims` (15). Backend setting `quest_ai_incremental_replies_enabled` (`QUEST_AI_INCREMENTAL_REPLIES_ENABLED`).
With either off, behaviour is as before this page, except the ask fixes (a `needs_person` verdict now files one real
decision; a platform fault or spent budget ends as a final), which are not behind the switch.

Who stamps what: the runner knows the verdict, not the input message id. QAR sends `why_more`, `next`, `done`, `coverage`,
`attempt` and its own `seq` hint. The backend is authoritative for `turn_id` (the input message id; in team chat the
`reply_to_id`) and `seq` (next number within the turn, so a replay cannot duplicate: message id `turn_id:seq`).

Message fields (stored on the message document, solo and team; absent on old messages, which are a group of one):
`turn_id: str`, `seq: int` (1-based), `kind: "interim" | "final" | "ask"`, and per kind:
- interim: `done: str`, `why_more: str`, `next: str`, `coverage?: object`, `will_continue: true`. `text` is also filled
  with a plain-text rendering (done, then why, then next) so an old app build shows a normal assistant bubble. Never
  `superseded` by this feature, but the backend still writes `superseded: true` on them when the final lands so old
  builds hide them (new builds read `turn_id` and ignore `superseded` for grouping).
- final: the ordinary assistant reply plus `turn_id`, `seq`, `outcome: "met" | "stopped"`, `stop_reason?`
  (`budget | stalled | attempt_cap | tool_fault`). Stored `kind` stays what it is today (absent or "answer"); `final` is
  carried by `turn_kind: "final"` so existing kind-based code is untouched.
- ask: `decision_id`, `assigned_to` (user id), `assigned_to_name`, `question`, `why_only_you`, `done`, `options?: [str]`,
  `waiting_on_name`. For viewers who may not see the question, the server redacts: they get `kind: "ask"` with
  `decision_id`, `assigned_to_name`, `waiting_on_name` and `redacted: true`, no `question`, `done` or `options`.
SSE frames: `interim` frame `{type:"interim", text, data:{turn_id, seq, done, why_more, next, coverage?}}`;
ask reuses `{type:"decision", ...}` with `data` above (`turn_id`, `seq` added); the final result frame gains `turn_id`,
`seq`, `outcome`, `stop_reason`. Team streams now carry the same three frames; the sync bus publishes each stored message.
Unknown kind on an old build: rendered as a plain assistant bubble from `text` (every new message keeps `text` filled).
Notifications: interim never pushes or counts unread; ask notifies its assignee; final as before.

What the runner sends (as built). Context the consumer passes in the turn's `context_meta`: `participants`
(`[{user_id, name}]`, the conversation's humans who are team members), `asker_user_id`, `turn_id` (if known), `conv_id`,
`quest_id`.
- `interim` event: `text` is the plain rendering; `data = {final: false, coverage, attempt, seq, done, why_more, next,
  will_continue: true}`. `done` is receipts first, then the coverage line, then the worker's own account only when no claim
  check flagged it.
- Ask (a `needs_person` verdict, or a tool failure whose ledger status is 401 or 403): QAR files ONE decision-request
  through its `EscalationSink` (new optional `Escalation` fields: `dedupe_key`, `conv_id`, `notify_user_id`, `options`,
  `why_only_you`; the key is `turn-ask:<turn_id or conv_id>:<hash of the question>`), then emits `{type: decision, text: <done
  plus question>, decision_id, data: {outcome: "asked", question, why_only_you, done, options, assigned_to, asked_of,
  assignee_fell_back, asker_user_id}}`. The terminal result frame that follows has `data.outcome = "asked"`; the consumer stores
  ONE `ask` message for the turn, not an extra final. If filing fails, the turn ends as a final whose text says the
  question could not be filed.
- Final: the result frame's `data` gains `outcome` (`met | stopped | asked`) and, for `stopped`, `stop_reason`
  (`budget | stalled | attempt_cap | tool_fault`). A platform fault (any tool failure other than 401 or 403), spent budget and
  a stalled loop are finals, never asks.
- The verifier schema gains `asked_of` (`asker | member`) and `member_ref`, offered only when the turn lists more than one
  participant; code resolves `member_ref` against `participants` and falls back to the asker.
- `create_decision` (the AI's tool) takes an optional `assignee`, resolved the same way against `participants` (else the quest's
  assignable members), falling back to the asker; the asker is also `notify_user_id`.
- The queued-task lane no longer posts text that looks like an ask with an empty decision id: it files a real decision (key
  `turn-ask:<task id>:...`) or says plainly it could not.
Not built (later phases): task-lane interims (phase 5), measured arms (phase 6), the metric rows (section 7).

Backend additions to the contract (as built in quest-backend, setting `quest_ai_incremental_replies_enabled`, default off):
- Stored ask also carries `asker_id`, `visible_to_all` and `waiting_on_name`; the asker's copy adds `can_withdraw: true`. A
  redacted ask is `{id, kind: "ask", turn_id, seq, decision_id, assigned_to_name, waiting_on_name, redacted: true, text: "Waiting on <name>"}`.
- Ask SSE frame is `{event: "decision", text, decision_id, message_id, data}`; on team streams interim and decision frames also
  carry `message` (the wire message as that viewer may see it). The final carries `turn_id`, `seq`, `turn_kind: "final"`, `outcome`, `stop_reason`.
- New apps fetch `GET /api/quest-ai/conversations/{id}?include_interim=true` to keep interims after the final; old builds
  render `interim` and `ask` as a plain assistant bubble from `text`.

### Answering an ask resumes the work (as built, backend module `ask_resume`)

When a person answers a decision filed by a live chat turn (solo, team thread or group), the work continues in the SAME
conversation and the SAME `turn_id`. A task-lane ask already did this through its paused task; a chat turn has none, so
the backend starts a new real-time task (`source: chat_ask_answer`, `resumes_decision_id`) whose brief holds the original
message, the ask and the answer. Its closing note takes the next `seq` of the ask's turn, like a resumed task today.
Only when incremental replies are on, and only after the existing answer path has recorded the answer.

Rules:
- Who runs it: the original asker's lane and rep, with the asker's access. The answerer contributes only the words they
  typed; their own data, quests and tools are never used, so nothing private to the answerer reaches another participant.
- What others see: one note (an `interim`) saying who answered and that the work continues, with neither the question nor
  the answer in it, then the result as an ordinary reply. When the ask was not visible to every human in the thread
  (`visible_to_all` false), the brief also tells the work not to quote the question or answer. The ask itself stays
  redacted for the same viewers as before.
- Once: an atomic claim on the decision row (`chat_resume_task_id`), taken only while the decision is `resolved`. A second
  answer, a replayed hook, a withdrawn or expired ask never starts a second run (an ask that is not resolved is not claimed).
- Declined go/no-go (`kind: approve`, not approved, not a pass): no task. A final `stopped` note says it was declined.
  A pass resumes, and the work decides for itself, as for a task.
- A deadline auto-proceed resumes too, and the note says no one answered in time.
- No keyword gating: every branch reads stored fields (status, kind, resolved_by, visible_to_all), never model output.
- Wire: no new fields or frames. The note is an ordinary `interim` message (`turn_id`, next `seq`); the declined note is a
  final (`turn_kind: "final"`, `outcome: "stopped"`). Team clients get both on the sync bus; solo clients see them on reload.
