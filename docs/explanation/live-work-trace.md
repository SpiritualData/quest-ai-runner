# Live work trace: showing the AI's work to the person, as it happens

Status: the contract (hub `hubofhubs.ports.trace`), the QAR emit (`quest_ai_runner/core/work_trace.py`), the backend forward and persist
and the frontend Trail are built. Real turns on the Claude Code subscription (web, the person's own records, scholarly papers, a
shared thread) were run on 2026-11-01 and the arms are measured by `metric-work-trace-experience` (solution `show-the-ais-work`; see
"First real runs" at the end). Background-task cards and other locales are not built. Working name for the feature in the app: "Trail".

## Why

People trust an AI more when they can see it did real work: sites read, papers found, their own
records consulted. Today a Quest AI turn does a lot (web search, scholarly search, memory recall,
link checks, failover) and the person sees only dots and then an answer. The goal is a typed,
user-facing channel that shows that work live, builds confidence, and feels like Quest.

## 1. What exists today, and the gap

How a run reaches the app (verified by reading code, not by running it):

- QAR emits `ProgressEvent` objects (`type`, `text`, `step`, `data`) through a `ProgressSink`
  (`hubofhubs/ports/deep.py`, type constants in `hubofhubs/ports/events.py`). `StreamSink`
  (`quest_ai_runner/core/adapters_sinks.py`) forwards all of them live; `MilestoneSink` keeps only
  the "surfacing" types for background runs.
- The backend chat stream (`quest-backend/app/api/endpoints/quest_ai/conversations.py`) turns each
  event into an SSE frame. Frames today: `status`, `understanding`, `plan`, `context`, `read`,
  `tokens`, `exec`, `partial`, `interim`, `delegated`, `mode_signal`, `explanation`, `token`, `done`.
- The frontend adapter (`questAiChatAdapter.ts`) consumes `status`, `delegated`, `mode_signal`,
  `understanding`, `context`, `explanation`, `exec`, `partial`, `token`, `done`. It does NOT use `read`
  or `plan` or `tokens`.
- Background tasks use a different channel: `taskLive.service.ts` (WebSocket first, SSE fallback)
  carries `{type:"progress", event:{kind,text,output,data,at}}` into `TaskLiveCard` and
  `TaskActivityFeed`, deduped by `kind + at + text`.
- After the answer, `explanation` gives "Explain how I got this" (`AnswerExplanation.tsx`), built from
  the run's recorded trace. It is the closest thing to what we want, but it is after the fact, one
  block, and not live.

What is hidden on purpose: the run's own step wording ("Looking into this...") is gated off by
`AiStatusTick` and `AiInternals`. That decision stands. The new trace must not carry that text.

The gap:

1. No frame says "a web search ran and N pages were read". Searches, fetches, scholarly lookups,
   link checks and failover happen inside hub solutions and leave no typed, user-safe event.
2. `context` frames show cards selected, but nothing for the web, papers or checks, and there is no
   running/done state or live count.
3. `ContextCardsDisplay` and `AnswerExplanation` each show part of the story in separate places. We
   should not add a third panel; the trace must absorb the first (see rules).
4. Nothing persists a compact "what I did" with the message, so a reload loses it.

## 2. The event contract

One small typed event, generic enough for any hub solution. Name: `WorkTraceEvent`. It describes one
unit of work (a "step") and is re-sent as it changes; the latest event for a `step_id` replaces
earlier ones.

```
WorkTraceEvent {
  v: 1
  trace_id:    string        // the turn id; one trace per turn
  step_id:     string        // stable per unit of work, so updates replace in place
  solution_id: string        // the hub seat id, e.g. "search-backend", "know-the-world"
  kind:        "sources" | "papers" | "records" | "check" | "action" | "notice"
                             // the presentation family; the UI picks a treatment from this
  label_key:   string        // i18n key, e.g. "workTrace.web.reading". Never display text.
  state:       "running" | "done" | "failed"
  counts:      { found?: int, read?: int, checked?: int, failed?: int, total?: int }
  items:       [ { ref: string, label: string, sub?: string, kind?: string } ]
                             // capped at 20 per step. See privacy rule below.
  affects_user: bool         // true only when the person's result or wait changed (failover, slow)
  started_at:  ISO-8601 UTC
  ended_at?:   ISO-8601 UTC  // present when state is done or failed
}
```

Rules for fields:

- `solution_id` is the hub seat id from `hubofhubs/esoi_selection.json`. The arm name (for example
  `openalex_semantic_and_web`) is never sent: the person does not choose arms.
- `label_key` is a key, not text, so the frontend translates it with the normal i18n packs. Unknown
  keys fall back to a generic label for the `kind`, never to the raw key.
- `items` by kind: sources = `{ref: domain, label: page title or domain}`; papers = `{ref: paper id,
  label: title, sub: "Author et al., 2021"}`; records = `{ref: record id, label: title of the
  person's own quest or reflection, kind: "quest"|"reflection"|"goal"|"note"}`; check = none, counts only.
- Unknown `kind` or `solution_id` still renders as one generic row (label plus count). A new hub
  solution therefore shows up with zero frontend change, and gets a custom look later.

Privacy rule (hard): only labels the viewer is allowed to see. Never stream: the search query text,
page or paper content, snippets, model reasoning or narration, tool arguments, file paths, full URLs
(domain only; no query strings, tokens or paths), other people's record titles, or conversation turn
text (the existing `_safe_event_title` already collapses history cards to "Conversation turn"; keep
that). Personal record titles go only to the owner. In team chat or any shared thread, records are
sent as counts only. The projector is an allowlist: it builds the event from named fields, so a new
field cannot leak by accident. It also trims labels to 80 chars and strips newlines.

Where it is emitted (the seam, not each arm):

- New constant `EVENT_WORK_TRACE = "work_trace"` in `hubofhubs/ports/events.py`, LIVE only (like
  `exec`, it is not in `SURFACING_EVENTS`), plus a small `hubofhubs/ports/trace.py` holding the
  dataclass, the allowlist projector and a validator. This keeps the contract public and reusable.
- QAR emits at the tool-registry seam, where every hub call already passes (and where the tool
  ledger records results). Derive events from the structural result of the call, not from model
  wording: web `search`/`fetch` observations (`websearch/research.py`), scholarly results
  (`science/*`), assembled cards (`EVENT_CONTEXT` point in `orchestrator_run_begin.py`), link guard
  verdicts (`critique/link_guard.py`), provider outage notes (`models/outage.py`, `seen_since`).
  Arms stay unchanged.
- Transport: no new channel. The chat stream forwards a `work_trace` SSE frame the same way it
  forwards `read` and `context`. For background tasks, send it as a task progress event with
  `kind: "work_trace"` and the event in `data`; `text` must change with state, because the task
  dedupe key is `kind + at + text` and would otherwise drop updates.
- Persistence: on `done`, the backend stores a compact summary (counts plus up to 20 items per step,
  already filtered) on the saved message, following how `explanation` is stored, so a reload and the
  stream-resume path show the same summary. Live-only detail is not stored.
- Throttle: coalesce updates per step to about 4 per second; always send the final `done`.

### Built (phases 1 to 3), and what the build fixed in this contract

Contract changes: none to the field names. Additions that are not fields: the wire frame, the opt-in and the persisted shape.

- Hub: `hubofhubs.ports.trace` (`WorkTraceEvent`, `project`, `validate`, `WorkTrace`), `EVENT_WORK_TRACE`, and the JSON schema
  `docs/reference/work-trace-event.schema.json` in the hub. `kind` and `solution_id` are validated as well-formed slugs, not
  against a closed list, so an unknown one still reaches the screen as a generic row. A `counts.failed` count is allowed.
- QAR opt-in: `context_meta["work_trace"] = {"audience": "owner" | "shared"}` (optional `trace_id`). Without it nothing is
  emitted. Any audience other than `owner` keeps `records` as counts only.
- Used, not selected (2026-11-01, after the first real runs): the `records` step counts only records the answer drew on, never every card
  the context assembler selected. A selected card is a candidate; it becomes a record read when the planner's `facts_read` quotes it (a
  run of four consecutive words found verbatim in the card's own text) or names its id or full title, or when the run fetched the card by
  id. Nothing here reads the model's meaning: it verifies a quote or a known id against stored text. A turn whose answer used no record
  emits no records step at all. The frontend no longer maps the `context` frame to a records step either. Papers: `read` is the works the
  finished reply cites (its DOI or full title stands in the reply, or the first author's surname and the year stand together), `found` is
  the largest single search total, and only cited works are listed; while the turn runs `read` is the abstracts taken, and the step settles
  on the cited ones when the reply is final. A site a search only listed stays on screen only when the reply cites its address host.
- Records are titles, and the reply must draw on them (2026-11-01, after the UX review). A record is shown by its human title only: one short line
  (at most 60 characters and 10 words, no colon or semicolon, not cut off, not the card id), never a conversation turn, a "topic" line or any text
  of a card. A card without such a title is not counted at all. A card the planner took (a verbatim run of four words in `facts_read`, its id or
  title named there, or a fetch by id) is a candidate until the finished reply is known; it becomes a record only if the reply draws on it: its
  title stands in the reply, three consecutive words of the card's text do, or the reply shares two distinct long words or numbers with the card's
  text. A reply that says it found nothing in a record it read, or that it has no web search, therefore carries no records. The records step is
  therefore sent once, when the reply is final (no live "looking through your records" line, because what the reply used is not yet known).
  These are overlap tests of known stored text against the reply, never keyword lists over the reply's wording. The numbers (four, three, two,
  five characters) are parameters, not tuned values. Sites: only a site the reply names (its exact host, with or without `www.`; a parent or sibling host does not count) is listed beside the
  opened pages; a search that opened and cited nothing ends as a found count with no list, which the screen should say as "searched the web,
  no page opened" (`counts.read` is 0, `items` is empty). `work_trace_check` enforces both when it is given the reply.
- Steps QAR emits (one `step_id` per turn each): `web` (seat `search-backend`, kind `sources`, key `workTrace.web.reading`),
  `papers` (`know-the-world`, `papers`, `workTrace.papers.searching`), `records` (`context-assembler`, `records`,
  `workTrace.records.looking`), `links` (`watch-for-risk`, `check`, `workTrace.links.checking`, `affects_user` true when a link was
  removed). Counts: `web.found` is results listed, `web.read` is pages fetched; `papers.found` is the total matches, `papers.read`
  is works whose abstract the run took; `links.checked` and `links.failed` (removed or unverifiable).
- SSE frame to the app: `data: {"event": "work_trace", "data": <WorkTraceEvent>}`, on the Quest AI chat, ad-hoc and group
  conversations and the team thread streams. On `done`, `data.work_trace` holds the list of finished steps (the same events),
  so a client that missed frames or reconnected still settles. A reply stored in message history carries `workTrace` (the same
  list; wire name `workTrace` on every chat surface, stored as `work_trace`), so a person who opens the chat later sees the
  settled line. A step still running when the turn ends is not stored.
- Group and team chat: the person who asked sees the stream as emitted (their own record titles). Every other human
  participant gets each event live through the sync bus, `topic "conversation"`, `action "work_trace"`, payload
  `{"replyToId": <the asking message id>, "workTrace": <WorkTraceEvent>}`, already projected by the hub with record titles
  removed (`records` and `action` steps are counts only; web domains and paper lines stay, they are public). The asker is not
  sent their own events over the bus. The stored `workTrace` of a shared thread is that same counts-only projection, so the
  history, the bus `message` push and a later reload agree. A one-on-one chat (one human) stores the full event.
- Backend switch: `QUEST_AI_WORK_TRACE=off` stops requesting, streaming, delivering and storing the trace.
- Metric hook: `evaluation/components/work_trace_check.py` (`check_turn`, `passes`, `score`) takes a finished turn's structural
  call records and its emitted events and reports overstated counts, private titles or items in a shared thread, URLs, queries or
  paths in labels, and steps that did not reach a terminal state. No model is called.
- Replays: the backend stream registry replays frames by index, so a resume repeats earlier `work_trace` frames. That is harmless
  by the replace-by-`step_id` rule, and the `done` frame repeats the final state.
- Not built: failover notice, overseer or act steps, the background-task progress path (`kind: "work_trace"` on task progress),
  and the non-stream group reply path (a reply produced without streaming carries no trace).

## 3. The UI concept: "Trail"

Idea: Quest is about journeys. Each piece of work is a waypoint the AI passed on the way to your
answer. A thin vertical line with small round waypoints, each lighting up as its step completes.
It uses Quest's own brand color for the line and lit waypoints (as a fill or stroke, never as text
color, per the styling standard), the existing surface and border tokens, and `colors.textMuted` for
text. No favicon fetching (it would tell a third party what was read); domains show as small monogram
chips. No spinner pill, no "Thought for" label, no ChatGPT-style list of search chips.

Collapsed default (one line, under the answer, after it settles):
`[trail glyph]  Read 14 sites, 3 papers, your 5 quest records`
The glyph is a short connected row of waypoints, one per kind used. Tap to expand. Nothing is shown
for kinds that did not run, and the line is hidden entirely on turns with no work (a greeting).

While running (before the first answer token): the same line shows live inside the reply area, with
numbers ticking up ("Reading 6 of 14 sites"). The first beat appears only after the existing 500 ms
grace (`useDelayedVisibility`), so a fast turn paints nothing. Waypoints light in order of arrival.

Settling: when the first answer token lands, the live trail slides into the one-line summary below
the answer text and collapses. It never stays expanded on its own. On `failed`, it stays quiet unless
the failure changes the answer (then one plain line, see below).

Per solution treatment (all collapsed into the summary, each distinct when expanded):

| Solution (seat) | Kind | While running | Expanded, after |
|---|---|---|---|
| Web (`search-backend`) | sources | Domain chips arrive one by one; the counter ticks "Reading 6 sites". | List of domains with page titles; a site that could not be reached is marked once, in muted text. |
| Scholarly (`know-the-world`) | papers | Two numbers: "Found 31, reading 3". Titles fade in as read. | Citation lines: title, "Author et al., 2021". Read papers first, found-only after. |
| Your Quest (`context-assembler`, `card-store`) | records | "Looking through your quest records" with a count. | "From your Quest": the person's own quest and reflection titles with a small type icon, shown FIRST because this is Quest's edge. Owner-only titles; counts in shared threads. |
| Link guard (`watch-for-risk` link check, `critique/link_guard`) | check | Appears after the draft, brief: "Checking links". | Only when the answer has links: "Checked 6 links". If it removed one: "Left out 1 link I could not verify". |
| Failover (`call-a-model`) | notice | Nothing. | Shown only if `affects_user`: "Switched to a backup model, this took a little longer." Never in the summary counts. |
| Overseer / consent (`watch-for-risk`) | check | Nothing. | Only when it stopped something: "Paused 1 step for your OK" (links to the existing approval card). Otherwise silent. |
| Act (`act-in-the-world`) | action | Existing approval and undo UI stays the owner. | "Changed 2 things in your quest" with their titles, only as a pointer to what already shows. |
| Model call, tier choice | none | Not shown. They add nothing for the person. | Not shown. |

Deep runs already have `ExecTexture`; the Trail does not replace it.

Unmet-value check for the summary line itself: it is the only thing on screen by default. The
expanded view is on demand.

## 4. Rules (every one is a test or review item)

1. Earn its place. An element is shown only if it tells the person something they can use: how much
   was looked at, where from, or that something changed their result. No decorative motion, no
   counts of internal steps, no timing for its own sake.
2. No internal jargon. No "step", "arm", "solution", "tool call", "tier", "seat", "ledger" in any
   string. Say "sites", "papers", "your quest records", "backup model".
3. Not the internal channel. The trace never carries `status` text, `plan` rationale, `narration`
   or `understanding` strings, and is never gated behind `aiInternalsVisible`. It is its own
   user-facing feature, like `AnswerExplanation`.
4. One home. The Trail absorbs `ContextCardsDisplay` for the person's records (one panel, not two),
   and links to "Explain how I got this" rather than repeating it.
5. Honest counts. "Read" means pages actually fetched or records actually used, not results listed.
   A count never claims more than the run did, and `failed` is shown only when it matters.
6. No em dashes anywhere (copy, keys' values, docs, any prompt text). Use a comma, colon or parentheses.
7. i18n: every string is a key in the `en` pack with a count-aware plural form; other packs follow
   the existing sync flow. "Quest" stays untranslated as a brand noun (see the translations playbook).
   Numbers and domains are data, not text.
8. Accessibility: the summary is a button with an accessible label that names the counts; live
   updates do not announce every tick (announce the settled summary once, polite). Respect reduced
   motion: no ticking animation, just the final number.
9. Never block the answer. The trace is decoration around the reply: a bad or missing event
   renders nothing, wrapped in an error boundary, like `ContextCardsDisplay`.
10. Privacy rule from section 2 is enforced in the projector (code), never by prompt.

## 5. Build plan (smallest first, all testable offline)

Phase 0 (not part of this build): frontend slice using frames that already exist (no backend change, no model run).
- Map today's `context` frame (cards and sources, already projected safely) into a `records` step.
- Files (quest-frontend): `src/services/sseStream.ts` (type union and `WorkTraceEvent` type),
  `src/utils/workTrace.ts` (pure reducer and summary-line composer), `src/components/chat/ai/WorkTrace.tsx`
  (collapsed line plus expand), `QuestAiDecoration.tsx` (mount), `questAiChatAdapter.ts` (feed frames
  to the reducer), `src/i18n/locales/en/quests.json` (keys with plurals).
- Tests: jest on the reducer with fixture frames (ordering, replace-by-step-id, plurals, unknown kind
  fallback, privacy: shared thread shows counts only); a render-harness test like `ChatLiveness` has.
  Check the `frontend` tmux window for errors. Ship only if it replaces `ContextCardsDisplay` cleanly.

Phase 1: contract in the hub (public repo `ai-hub-of-hubs-structure`).
- Files: `hubofhubs/ports/trace.py`, `hubofhubs/ports/events.py` (new constant), a JSON schema
  under `docs/reference/`, tests with fake solutions: projector drops every field not on the
  allowlist, caps items, strips URLs to domains, rejects bad `state`.

Phase 2: emit in QAR.
- Files: tool registry seam (find where hub tool calls are registered; not located yet), 
  `core/orchestrator_run_begin.py` (context point), `core/adapters_sinks.py` (nothing to change if
  `StreamSink` forwards all types; `MilestoneSink` drops by design), `runner/channel_session.py`
  if it filters event types. Tests: fake web, scholarly and link-guard results produce the expected
  events with no network and no model.

Phase 3: backend.
- Files: `quest_ai/conversations.py` (forward `work_trace` frame; owner-only filter and team-chat
  counts-only rule), `models/chat_message.py` plus storage (compact stored summary, same pattern as
  `explanation`), background task progress path. Test with a data-level endpoint test (TestClient,
  real handler, real Mongo) per `tests/api/endpoints/CLAUDE.md`: stored summary has no forbidden
  fields; a second user cannot receive the owner's record titles.

Phase 4: frontend per-solution looks (web chips, paper lines, records, link check, notice), settle
animation, `TaskLiveCard` and `TaskActivityFeed` handling of `kind: "work_trace"`, other locale packs,
update `quest-docs` (a visible change to how people see answers).

Phase 5: review. Run the `/joshua` feedback pass before calling it done; publish a mock page for
review using the artifacts skill, not text.

## 6. Risks and what was not verified

Verified by reading code and offline tests since the first draft: the in-process chat sink (`StreamSink`) forwards every event
type, so `work_trace` reaches the backend; the backend's event chain (`stream_quest_ai_events`) DROPPED any type it did not list
(it logged one warning per process), so `work_trace` is now handled there, and the group and team streams now forward it;
the tool registry call (`ToolRegistry.invoke`) has no emitter, so QAR derives events where the orchestrator holds the result
(`run_tool_calls`, `_exec_one_read`, the context event, the link guard); the literature tool exposes works as structured data
(`ToolResult.data`), web search exposes `Observation.hits` (title, url), and a page fetch exposes only the requested address, so a
read page is labelled by its domain.

Evidence now, and what is still not verified (updated 2026-11-01 after the first real runs and the UX review):
- Verified by real runs. The real orchestrator on the Claude Code subscription (Haiku, `claude-haiku-5-5`), with real web and scholarly search
  and a seeded fictional card store, emits `work_trace` events that pass `work_trace_check` (`evaluation/components/work_trace_experience/live_turns.py`;
  results in `evaluation/components/results.csv`). The in-process `StreamSink` forwards them and the registry seam is where the events are derived.
  Signed-in browser runs on solo and team chat showed the frames reaching the app (screenshots under the session's `real/ui`); they were taken
  before the "used, not selected" fix, so they show the old behaviour (unrelated records, conversation text as record titles) and no screenshot
  of the fixed build exists yet.
- Verified offline only. The projector, the privacy rules, replace-by-`step_id`, and the pure rules of the screen (`workTrace.ts`, through
  `run-worktrace-view.js`) on hand-built and captured events.
- Not verified. Post-fix screenshots of the Trail on the real thread (all states, 390 and 1280, light and dark); three live frames of one run
  and a second participant's live view; measured contrast and a screen reader transcript for the Trail; a two-account live run with a private
  record title in the question; `progressEventKey` dedupe and `resume_from_event` replay against a real resumed stream; the look, which has had
  no recorded review of a mock page; and whether a person's paraphrase of a record (the reply uses other words than the card) is counted
  (it is not, below the word-overlap rule, so the record count can be too low, never too high).

Risks:
- Privacy is the main one. A team chat or shared thread must not show one member's record titles.
  Mitigation: allowlist projector in code, owner check in backend, counts-only in shared threads.
- Confidence theatre. Counts that overstate work would damage trust. Mitigation: counts come from
  structural results only, and rule 5.
- Noise. Too many steps or motion makes the answer feel slower. Mitigation: 500 ms grace, throttle,
  one summary line by default, nothing for turns with no work.
- Cost of the third panel. Mitigation: absorb `ContextCardsDisplay`, link to `AnswerExplanation`.
- Scholarly "found vs read" depends on what the arm exposes; some arms return a ranked list without
  per-paper read state. Then show "Found N" only.
- Translation of plural forms across the existing language packs may lag the English pack.

## 7. Review changes (UX critique, frontend build decisions)

These override sections 3 and 4 where they differ.
1. Never moves. The live line collapses in place into the summary (same spot under the reply); it does not slide below the answer. A moving block shifts the text being read.
2. Summary line: records first (Quest's edge), then sites, papers; at most three clauses, kinds with zero reads are omitted ("Read 0 sites" never shows). Papers found but none read: "Found 31 papers". If everything is zero, no line at all.
3. Cut: a waypoint per step, the Action row (the approval and undo UI already owns it), the Overseer row (the approval card owns it), "Checked N links" when nothing was removed, and any repeat of "Explain how I got this". The glyph is one static three-dot mark, lit as kinds arrive; the vertical rail appears only when expanded, one waypoint per group, not per item.
4. Running state shows one line (the newest active kind) plus a single chip row (6 chips, then "+N"). Height is fixed, so a long run never grows the page; the count updates at most 4 times a second.
5. Expanded groups show 5 rows each with "Show all N" (cut-off text must be reachable). Failed items are one muted "could not open" line, never red. Only an `affects_user` failure gets a plain sentence in the summary.
6. Settle on the first answer token, or on done or error if no token came. Never re-expand by itself.
7. Motion: chips fade in over 120 ms, collapse takes 200 ms. Reduced motion: no fade or tick, final numbers only.
8. Phone (390px): summary wraps to two lines at most, the whole row is the 44px target, chips wrap, no horizontal scroll.
9. Accessibility: button with aria-expanded and a label naming the counts; one polite announcement on settle, none while running; chips are hidden from screen readers (the list is the real content).
10. Dark and light: waypoints and chip rings use the `brand` fill or border only; every word is `text` or `textMuted`; the monogram letter is `text`.
11. The word "Trail" is not shown to people; the line stands alone.
12. Phase 0 maps the `context` frame to records, but counts only cards that are the person's own (not history turns, guidance or docs), and shows a card title only if it is not a file path; otherwise it is counted without a name.

## 8. First real runs (2026-11-01) and what they changed

Four real turns (`evaluation/components/work_trace_experience/live_turns.py`: the real orchestrator on the Claude Code subscription, real
web and scholarly search, a seeded fictional card store) passed the structural check (`work_trace_check`) with no violation. Reading
the events and the elements shown found these, each fixed or recorded:

- A web search that opened no page showed nothing at all, and its live line read "Reading 0 of N sites". Now the line says it searched the
  web, and the live line says "Searching the web". (quest-frontend `workTrace.ts`.)
- Sites a search only listed were shown as chips and rows beside read ones. A `sources` item now carries `kind` `read` or `found`
  (hub `project_item`); only opened pages show as chips, and the expanded list shows listed sites only when none was opened.
- Three scholarly searches added their totals into one "papers found" figure and counted a paper again when two searches returned it, and one
  paper under two DOIs showed twice. `found` is now the largest single search total and a work is read once. (`core/work_trace.py`.)
- A heading over a single group, and a counts-only group in a shared thread that only repeated the summary, did not earn their place. Headings
  show only for several groups, and a line with nothing behind it is a sentence, not a button.
- Fixed afterwards (same day): the records step counted the cards the context assembler selected for the turn, so a question about the web
  or an unrelated Python question showed the person's unrelated quest records (relevance scores of about 0.5 for unrelated cards against
  1.0 for the matching one on the same store; a threshold would be an invented margin). It now counts only records the answer used (see
  "Used, not selected" in section 2), and `work_trace_check` flags a records count or item beyond the used cards. Papers a search returned
  but the reply did not cite are no longer listed as read, and listed sites the reply does not cite are no longer shown.
