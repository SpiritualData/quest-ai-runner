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
  person's own quest or reflection, kind: "quest"|"reflection"|"goal"|"note"}`; check = the bare domain of each link left out (`kind` `removed`, `ref` and `label` both the domain, never a path or query), nothing for links that checked out.
- Unknown `kind` or `solution_id` still renders as one generic row (label plus count). A new hub
  solution therefore shows up with zero frontend change, and gets a custom look later.

Privacy rule (hard): only labels the viewer is allowed to see. Never stream: the search query text,
page or paper content, snippets, model reasoning or narration, tool arguments, file paths, full URLs
(domain only; no query strings, tokens or paths), other people's record titles, or conversation turn
text (the existing `_safe_event_title` already collapses history cards to "Conversation turn"; keep
that). Personal record titles go only to the owner. In team chat or any shared thread, records are
sent as counts only, except a record every human participant may see (section 10). The projector is an allowlist: it builds the event from named fields, so a new
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
  therefore sent once, when the reply is final (a live "Looking through records" line shows only if a records step is running; see section 9).
  These are overlap tests of known stored text against the reply, never keyword lists over the reply's wording. The numbers (four, three, two,
  five characters) are parameters, not tuned values. Sites: only a site the reply names (its exact host, with or without `www.`; a parent or sibling host does not count) is listed beside the
  opened pages; a search that opened and cited nothing ends as a found count with no list, and the screen shows no settled line for it (`counts.read` is 0, `items` is empty; section 9). A site the reply cites stays as a `found` item and the line says "Searched the web and cited a.org", never "read". `work_trace_check` enforces both when it is given the reply.
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
`[trail glyph]  Read 14 sites, checked abstracts of 3 papers, used your 5 quest records`
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
| Scholarly (`know-the-world`) | papers | Two numbers: "Found 31, checking abstracts of 3". Titles fade in as read. | Citation lines: title, "Author et al., 2021". Papers with an abstract taken first, found-only after. |
| Your Quest (`context-assembler`, `card-store`) | records | "Looking through records", no title, no number, for every viewer. | "From your Quest": the person's own quest and reflection titles with a small type icon, shown FIRST because this is Quest's edge. Owner-only titles; counts ("Used N records") in shared threads and wherever the viewer may not see titles. |
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

## 6. Risks and what is measured

Verified by reading code and offline tests since the first draft: the in-process chat sink (`StreamSink`) forwards every event
type, so `work_trace` reaches the backend; the backend's event chain (`stream_quest_ai_events`) DROPPED any type it did not list
(it logged one warning per process), so `work_trace` is now handled there, and the group and team streams now forward it;
the tool registry call (`ToolRegistry.invoke`) has no emitter, so QAR derives events where the orchestrator holds the result
(`run_tool_calls`, `_exec_one_read`, the context event, the link guard); the literature tool exposes works as structured data
(`ToolResult.data`), web search exposes `Observation.hits` (title, url), and a page fetch exposes only the requested address, so a
read page is labelled by its domain.

Evidence (latest measured set, 2026-11-01, `evaluation/components/results.csv`, component `work_trace_experience`, 31 cases, judge
`claude-sonnet` alias, turns `claude-haiku-5-5`, git sha 184876a):
- The Trail arm passed 31 of 31 cases (plain list 20 of 31, no trace 9 of 31). It has no truthfulness-gate failure, no parity break, and no
  turn where work was done and nothing shown. The value judge rated 14 of 14 shown elements as earning their place, on 9 real turns.
- The 9 real turns come from `evaluation/components/work_trace_experience/live_turns.py`: the real orchestrator on the Claude Code
  subscription (Haiku), with real web and scholarly search and a seeded fictional card store. Their events pass `work_trace_check`. The
  in-process `StreamSink` forwards them and the registry seam is where the events are derived.
- Earlier runs the same day, on the same real turns before the fixes in sections 8 to 10, passed 3 of 10 and 7 of 10 on the Trail arm;
  section 8 to 10 record what each failure changed.
- Signed-in browser runs on solo and team chat showed the frames reaching the app (screenshots under the session's `real/ui`); they were
  taken before the "used, not selected" fix, so they show the earlier behaviour (unrelated records, conversation text as record titles).
- The projector, the privacy rules, replace-by-`step_id`, and the pure rules of the screen (`workTrace.ts`, through
  `run-worktrace-view.js`) are covered offline on hand-built and captured events.
- A reply that restates a record in other words than the card is not counted (below the word-overlap rule), so the record count can be too
  low, never too high.

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
8. Phone (390px): summary wraps to two lines, three when it names a record and pages (section 9), the whole row is the 44px target, chips wrap, no horizontal scroll.
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

## 9. Judge failures on the saved real turns, and the wording they changed (2026-11-01)

The value judge kept failing three elements on the ten saved real turns. Joshua's rule: nothing is shown unless it adds value for the
person or covers a plausible unstated need. Each failure was read against the case before anything was changed:

- The live line "Searching the web" was vague. The live line is now the newest concrete thing opened ("Opened blog.rust-lang.org", with chips for the pages opened before it), and
  before a page is open a running search says "Searching the web" (changed after a real Claude in Chrome run: a search that opened nothing left only
  waiting dots for 20 seconds or more, and the step running is a fact the line can state). Sites a search only listed
  and papers retrieved in a result set are not shown live: whether the answer rests on them is not known until it is final.
- The summary "Read N of your quest records" was a bare count. Records are now named: one or two ("Used your record: Run a half marathon
  by June"), or the first and the rest counted ("Used "Run a half marathon" and 3 more of your records"). Pages and papers follow the same
  rule ("Read blog.rust-lang.org", "Checked abstracts of papers by Santos et al., 2026 and 4 more"). A shared (team) thread names only records every participant may see (section 10). Where nothing can be named the count stays.
- Wording follows what the run did (2026-11-01): "Read" is used only where a full page or text was opened (sites). A paper counts as read
  when the run took its abstract, which is less than reading it, so papers say "Checked the abstract of a paper by Lee et al., 2021" and
  "Checked abstracts of papers by Lee et al., 2021 and 4 more", never "Read papers". The same wording holds in the bare count ("Checked
  abstracts of 5 papers"). The `papers` live label is "Found 31, checking abstracts of 3", never "reading".
- The removed-link heading "Good to know" was vague, and the group under it repeated one line. Removed links are now a sentence in the
  summary ("Left out a link to example.com that I could not verify", section 10), shown only when a link was removed; there is no such heading and no group behind it.
- A correct count of five cited papers was called a conflict. The judge was wrong, caused by the harness: `score.py` showed it the first
  900 characters of the reply, and two of the five papers stood further down. It now reads the whole reply (`REPLY_CHARS`).

One source of truth for the words: `workTrace.ts` composes the live line, the summary and the group rows (`liveText`, `summaryText`,
`expandedGroups`); `WorkTrace.tsx` renders them and `scripts/run-worktrace-view.js` prints the same strings for the judge, through
the same English pack. Nothing is re-worded in Python or in a parallel script.

- **Records speak for every viewer (2026-11-01, third review).** The earlier rule that a bare "Used 1 record" adds nothing left a turn that
  used only private records with no line at all, live or settled, so in a group chat there was no visible progress for it. A records
  step now shows a counts-only, title-free line to every viewer: live "Looking through records" (no number while running; it shows only
  while a records step is running) and settled "Used 1 record" / "Used {{count}} records". Where the viewer may see titles they are named
  as before (section 10). A title the viewer may not see never reaches the line, the live text or the rows.
- **A settled web search that found results but opened no page, and whose results the reply did not cite, shows NO line (product decision,
  2026-11-01).** Listing unused sources, collapsed or not, fails the value judge ("unused source, never opened") and the earlier real-turn review
  found it a value failure; the working dots and the live line already showed the search while it ran. The emitter leaves such a step as a found
  count with no items; the screen shows a settled line only when the step has items, that is, a page was opened or the reply cited the site,
  and it names the cited sites in the shape of the "Read" sentences: "Searched the web and cited a.org", "... cited a.org and b.org", "... cited a.org and 3 more sites" (strings `summary.citedOne/Two/More`; "cited", never "read", because `counts.read` is 0 for a cited-only site, and "Read ..." stays for opened pages). The earlier
  attempt to list found-but-uncited sites as expandable rows was reverted. Implemented once in `workTrace.ts` `summaryClauses` and mirrored in
  the hub terminal `trail.py`.

Three elements still failed after this (a bare team count, a removed link that cannot be seen, and a false positive of the used-record
rule). Section 10 records what each was changed to.

## 10. Fixing the last three judge failures (2026-11-01)

Joshua's rule: fix every instance now; everything shown adds value; never overstate; no keyword gating on model output.

- **Shared thread: name a record only when every human participant can see it.** A shared thread now names a record when it is visible to EVERY human of the thread and otherwise counts it; a line that
  only says a count is kept (section 9, third review: "Used 3 records"), since dropping it left a records-only turn silent. Visibility is decided from structure by the backend (`app/business/conversations/work_trace_visibility.py`), never by
  the runner and never from what a record says: a quest card (`quest-<quest id>`) is visible when the quest, loaded by id, passes
  `user_can_access_quest` for every human of the thread; a team-scoped card in a team thread is visible when every human is a member of
  that team; anything else, and anything that cannot be resolved, is private. The backend sets `visible_to_all` on each records item
  (overwriting any mark that arrives), for the asker's stream too so the asker, the others and the stored reply show the same line. The hub
  projector keeps a title for a viewer who may not see titles only on an item whose mark is exactly `true`. The app
  (`redactForShared`) does the same and treats an unmarked item as private. Wording: "Used the team's record: A", "Used the team's records: A
  and B", `Used the team's record "A" and 2 more records`. `work_trace_check` flags any titled item in a shared thread that is not marked, and
  any private title anywhere in the trace; the backend endpoint tests carry private-title canaries (a quest only the asker owns, a quest a
  third participant cannot open, a card nobody can place, a forged mark from the emitter).
- **Removed link: show where it pointed.** The link-check step now carries one item per link left out, the bare domain only (`kind`
  `removed`; the path, query string and fragment never leave, and a link with no domain such as a DOI is counted, not named). The line reads
  "Left out a link to example.com that I could not verify", "Left out links to a.org and b.org that I could not verify", "Left out 2 links to
  a.org that I could not verify", or, when some link has no domain, "Left out 2 links I could not verify". The judge sees these words
  exactly as the person does (the rubric is unchanged). `work_trace_check` requires bare hostnames that belong to a removed link and fails on
  any path, query or fragment of an address anywhere in the trace.
- **Used-record false positive.** `reply_draws_on` counted words common in general English ("practice", "second"). A word now counts toward a
  record only if it is rare in general English (absent from the three frequency bands the context adapters already ship,
  `adapters/english_word_bands.py`, registered into core with `set_word_rarity` because core imports no adapter): the reply draws on a record
  when its title stands in it, every rare word of its title does, a run of three of the card's words does, or it shares THREE distinct rare
  words or numbers of two digits or more with the card's text. This is a table lookup over stored text and the reply, never a keyword list
  over model output. Unregistered, no word is rare, so only a title or a quote can show a record (an undercount, never an overcount).
  The saved spaced-repetition turn is an offline case in `tests/test_work_trace.py`.

## 11. One rule for names: the line names, the rows add (2026-11-01)

The post-fix judge failed the papers case because the summary named papers ("Checked abstracts of papers by Cherif et al., 2015 and Yang et al.") and the rows
beneath repeated the same names. The rule, applied once in `workTrace.ts` (`saidNames`, `expandedGroups`) so solo, shared-list and team
surfaces cannot differ:

- The summary line names what it can (one or two, then "and N more"), exactly as in section 9. A name the line says is never said again in a row.
- Rows add only what the line did not say. A named paper shows its title only; an unnamed paper shows title and "(first author, year)". A named
  page shows its page title only (no row at all when it has none); an unnamed page shows its domain and title. The first record is named in the
  line, so a records list holds the others. A group with no rows left is not drawn, and the line is then not a button (`hasExpandableDetail` is
  derived from `expandedGroups`, so the button and its rows cannot disagree).
- Counts-only lines ("Checked abstracts of 5 papers") were considered for the case where rows follow and rejected: collapsed, the line is all the person sees,
  and a bare count was the section 9 failure.

Measured: `npm run test:worktrace` pins the rule (the same checks failed on the previous source). The judge on TRACE-REAL-PAPERS (a post-fix row,
not held-out, one case) no longer reports repeated names but still rejects the summary line as counting work that the rows list; a counts-only
line failed the same way. That is open: the judge sees the collapsed line and the expanded rows together, which the person never does.

### The open item was the judge, not the Trail (2026-11-01, changed after seeing the failure)

Reading the judge's verdicts on TRACE-REAL-PAPERS: element 1, the summary "Checked abstracts of papers by Santos et al., 2026 and 4 more", got "Counts work, repeats
rows below"; every row got "Lets person open cited paper". The rubric (`score.JUDGE_RULE`) said an element fails when it "repeats another element"
and the judge saw the summary and the expanded rows in one flat list. Section 9 requires the line to name what it can (a bare count fails), and the
rows list the same papers, so no summary shape could pass: naming repeated the rows, counting counted work. The rubric contradicted the spec, because
the spec's person reads the line alone and opens the rows only by tapping. `score.py` now labels each element with the moment it is shown
(`MOMENTS`) and says only elements at the same moment can repeat each other (`MOMENT_RULE`); `evaluation/components/test_work_trace_judge.py` pins the
prompt. The same turn under the new rubric: 6/6 elements earn their place. The Trail was not changed. Because the rubric changed, saved captures were
rescored under it and are labelled as such in `results.csv`.
