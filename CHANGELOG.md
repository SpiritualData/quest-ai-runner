# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Semantic card dedup/merge: the post-deep updater merges into a clear twin instead of creating
  one.** When `Orchestrator._update_cards_after_deep` would CREATE a new context card, it now first
  asks the card store's OPTIONAL `find_similar_card(text, *, user_id, min_score)` capability whether a
  sufficiently-similar card already exists for THIS user (by embedding COSINE similarity) and, if so,
  REDIRECTS the edit to UPDATE that card (merging the proposed name/description + content) rather than
  minting a near-duplicate. The capability is detected by duck-typing, exactly like the existing
  card-update detection: `QdrantCardRepository.find_similar_card` implements it by reusing the SAME
  document embedder + cards collection it already embeds cards into on write (no second embedding
  path), scoped to the user's `u:<user_id>:` id-namespace so a merge can never cross users;
  `FileContextStore.find_similar_card` delegates to the repo when present. A store with no embeddings
  (the default `FilesystemCardRepository`) exposes nothing, so the updater cleanly degrades to
  create-as-before (no string/fuzzy fallback). Threshold is the new `OrchestratorConfig`
  `card_merge_similarity` (default `DEFAULT_CARD_MERGE_SIMILARITY` = 0.85, clear-twin only); a value
  of `1.0` disables the behavior. Edits that already target a shown existing card are untouched, and
  the whole path is best-effort (any miss/error -> create as before). Tests in
  `tests/test_async_card_update.py` (orchestrator wiring: redirect, no-match, capability-absent,
  user-scope isolation, existing-id-untouched, threshold-1.0 disable) and
  `tests/test_qdrant_card_repository.py` (offline embedded-Qdrant: same-user match, cross-user
  isolation, threshold gating).

### Changed
- **Recently-used / recently-updated cards win a near-tie in keyword retrieval (bounded boost).**
  In the keyword arm, `usage_count` and `last_verified_at` were only TIE-BREAKERS, so a card the user
  just relied on did not actually resurface more readily. `FileContextStore` now applies a small
  bounded multiplier (`_recency_boost_factor`, default cap +20%, configurable via the new
  `recency_boost_max` constructor arg, set 0.0 to disable) to the RANKING score, blending a usage
  signal (saturating at 5 uses) with a recency signal (newest content `ts`, 30-day half-life). The
  CONFIDENCE GATE still uses the un-boosted relevance score, so an irrelevant-but-recent card is never
  resurrected; a card with no history is unaffected (factor 1.0). Tests in
  `tests/test_card_content.py::TestRecencyBoost` (including gate-independence).
- **Card content de-duplicates references on write (no more accumulating the same pointer).** The
  post-deep card updater (and `record()`) appended content items with no dedup, so re-adding the
  same collection id / file path / note across runs stored duplicate items until the recency-trim
  dropped the oldest. New `content_identity_key()` + `dedupe_content()` (in `card_content_render.py`)
  collapse items that point at the SAME reference (collection by id, file by path, note by text) into
  one merged item, keeping the existing item's stable id and refreshing to the newest `ts` + freshest
  non-empty `why`. Applied in `FileContextStore._update_card_inner` (after additions, before the
  recency-trim) and in `_record_inner`. Within one card / user scope only; lossless (no information
  dropped on merge). Tests in `tests/test_card_content.py::TestContentDedup`.

### Fixed
- **Narration beats no longer assert ungrounded conclusions before the search finishes.** The
  relayed planner-rationale beats (the conversational "thinking out loud" lines) were told to be
  "specific and opinionated," which pushed the model to declare conclusions from partial GATHERED
  (e.g. "I'm not seeing a spec, so we must be relying on native translation") before it had read
  enough. The re-plan rationale instruction now requires the beat to speak only to what GATHERED
  actually shows, to state an absence as "I haven't found X yet" (never as "X doesn't exist" or a
  substitute fact), and to voice an unconfirmed hunch as a hunch or question, not a settled fact. The
  step-0 instruction now forbids naming what it expects to find before any read. Test
  `test_narration_rationale_instructions_demand_grounding_discipline`.
- **Hybrid consolidation no longer guts a card's non-item content.** The consolidating rebuild used
  to reconstruct `context_view` from each card's content `items` alone, pasting only `item.text`
  under a `### <title>` header. That silently dropped everything a rendered card section carries
  beyond its items: a keyword card's summary, its file PATH LISTINGS (the core value of the IDF arm,
  which are not content items), and its conventions. A keyword card with no items was skipped
  entirely. The rebuild now starts from each surviving card's VERBATIM `rendered_section` (the whole
  block both arms already emit). Both arms attach `rendered_section` to their `card_metadata`
  (`adapters/file_context_store.py`, `adapters/vector_context_assembler.py`); item pruning is applied
  by REMOVING the pruned items' exact rendered fragments from that verbatim section (never
  re-synthesizing it), with a guard that leaves the section intact when a fragment cannot be located.
  Item-less file/reference cards now participate in consolidation too (`consolidate_context` /
  `_validate_consolidation` in `core/card_filter.py` keep a named card with an empty items list as
  "keep the whole card"). The orchestrator's deep preamble (`_materialize_deep_context`) likewise
  pastes each card's verbatim `rendered_section`, swapping only a pointer-delivered file item's
  fragment for a pointer line. New tests in `tests/test_vector_context.py` (mixed item/file-only,
  prune-by-removal, drop), `tests/test_consolidate_context.py` (file-only card keep/drop), and
  `tests/test_per_goal_context_iteration.py` (deep preamble rendered_section + pointer swap).

### Added
- **Expandable "What I'll remember" panel in the Textual terminal UI.** After a deep
  run completes, the orchestrator's FUTURE-CONTEXT bullets (what the AI noted for
  next time) are now surfaced in a collapsible `FutureContextPanel`. A dim hint line
  appears in the transcript ("f: What I'll remember  (N items)"); press `f` to toggle
  the panel open or closed. The panel is never shown when there is nothing to remember.
  A pure helper `_build_future_context_text()` builds the Rich Text and is testable
  offline without a Textual event loop. New tests in `tests/test_future_context_ui.py`.
- **Consolidating holistic context filter (one LLM pass over the merged card set).** After both
  retrieval arms each filter their own cards, `HybridContextAssembler` now runs a single
  consolidating LLM call over the merged, deduped card set that (a) drops tangential or redundant
  cards across arms, (b) reranks the survivors, and (c) prunes which content ITEMS inside each kept
  card survive. Content stays VERBATIM (the model selects card ids and item ids only, it never
  rewrites text), via the new `consolidate_context()` in `core/card_filter.py` (parsed with the
  existing `_extract_json`). The hybrid then rebuilds `context_view` from only the surviving
  cards/items, in the returned order, under `### <title>` headers. It engages only when a
  `model_provider` is wired (the balanced tier, set in `config.py`) and at least one merged card
  carries structured items; on no provider or any failure it falls back to exactly the prior
  mechanical merge (the never-worse guarantee). Each arm now attaches structured `items` (and a
  `title`) to its `card_metadata`, built by the new `render_card_content_blocks()` in
  `adapters/card_content_render.py` (which `render_card_content` is refactored to reuse, byte for
  byte). New tests: `tests/test_consolidate_context.py`, plus consolidation cases in
  `tests/test_vector_context.py`.
- **Deep preamble materializes context paste-vs-pointer.** The per-goal DEEP context is now rendered
  from the assembled `card_metadata` items: a file item tagged `deliver="pointer"` (and
  `pointer_eligible`) becomes a short pointer line naming the path (the worker re-reads the file with
  its own tools), while everything else is pasted verbatim. The planner/answer path still uses the
  fully-pasted `context_view`. The `EVENT_CONTEXT` event now carries a lightweight projection of the
  card metadata (ids, titles, per-type counts, file paths) instead of dumping each item's full text.
  File-only deployments with no structured items keep using `context_view` unchanged.

### Fixed
- **The instant response now goes out the moment it is generated, never sequenced behind context
  search or guidance.** The narration first beat was generated in the background but only EMITTED at
  `Narrator.flush_first()`, which the orchestrator calls after the (potentially slow) guidance
  `select()` and the search-status emission. So even though the one-sentence ack was ready in ~1s, it
  could not appear until the main pipeline reached that point. The first beat now emits ITSELF from
  its background thread the instant the model returns (`Narrator.begin` → `_gen_and_say`), decoupled
  from the main pipeline; `flush_first()` is now only a join barrier that preserves ordering (the ack
  still precedes the planner's later relay beats). Emit is lock-guarded so the background ack and a
  main-thread relay beat never interleave. Safe because every consumer funnels events through a
  thread-safe queue (`run_stream`) or marshals to the UI thread. Regression test
  `test_narrator_first_beat_emits_from_background_without_flush`.
- **The instant "thinking out loud" line now actually shows while context is being searched (terminal
  UIs).** The orchestrator emits the narration/instant-ack beat as `EVENT_PARTIAL` tagged
  `data={"narration": True}`, and the frontend already consumed that key, but both terminal sessions
  (`interactive.py`, `textual_ui.py`) still gated on the legacy `data["ack"]` flag. So the immediate
  acknowledgment was never recognized: the Textual UI misrouted it into the discarded answer buffer
  (the real answer comes from `final.text`), and nothing appeared during the search. Both consumers
  now recognize `data["narration"]` (keeping `ack` for back-compat) and render it as a dim line.
  Regression test `test_instant_ack_emits_narration_flagged_partial`.
- **The context-search status reads "searching context…" instead of "searching corpus…".** That
  stage is the `ContextAssembler.assemble()` call (a hybrid keyword + vector search over the wired
  context cards plus turn history), not a single named source. "searching context" names the stage
  (STAGE 2: FIND CONTEXT) honestly and matches what the user expects to see.

- **The internal FUTURE-CONTEXT section no longer leaks into the deep output shown to the user.**
  When the async card updater is active, the deep worker is asked to END its output with a
  `=== FUTURE CONTEXT (for similar requests by this user) ===` section that the updater parses to
  learn reusable pointers. That section is plumbing, not part of the deliverable, but it was being
  shown verbatim at the end of the user-facing deep result. New `_strip_future_context()` removes it
  from every user-facing surface (the final deep `EVENT_RESULT`, the `deep_output` milestone, the
  "Worker output" not-met milestone, and the deferred-deep fold-in) while `DeepResult.output` stays
  raw so `_parse_future_context()` / the card updater still see it. Stripping and parsing split the
  output at the same delimiter, so they are complementary with no overlap. Tests
  `test_strip_removes_future_context_from_user_output`, `test_strip_is_noop_without_delimiter`,
  `test_strip_and_parse_are_complementary`.

- **Direct follow-ups are answered from the conversation instead of triggering a fresh corpus
  search.** When a user asked a follow-up whose answer the assistant had JUST given (e.g. it
  described a plan and its file path, then the user asked "what's the filepath?"), the planner still
  ran a full read/grep loop over the corpus — slow, and pointless because the answer was already in
  the transcript it was handed. The planner prompt now leads with a TRANSCRIPT-FIRST principle:
  before choosing `read`, check the RECENT TRANSCRIPT and GATHERED, and if the answer is already
  there (the common direct-follow-up case) answer straight from it; only `read` when the current
  message genuinely needs substance the conversation does not already contain. The transcript NOTE
  carries a matching carve-out so a direct follow-up is treated as the user explicitly asking about
  the prior turn. Regression test
  `test_planner_prompt_instructs_answering_followups_from_transcript`.

- **Capability/discovery menus no longer pollute the answer (the planner stops answering from the
  operations list).** The auto-injected `list_operations` menu (and any `list_sources`/`describe_*`
  discovery read) was added to `gathered` and rendered to the answer LLM as "ACTUAL CONTENT READ FOR
  THIS ANSWER" — so for a substantive request the model answered FROM the menu ("I have these
  operations; shall I run discover_goals / grep?") instead of gathering the real material (codebase,
  conversation). Discovery observations are now tagged (`discovery: true`) at the read site and: (1)
  excluded from the answer grounding entirely (`_grounding_block`) — when only a menu was gathered,
  there is no answer-content section, so the model grounds on real context or says it lacks it; (2)
  excluded from a single-goal deep run's `context_preamble` (the worker has its own tools and does
  not need the orchestrator's operations menu); (3) relabeled in the planner/worker view as
  "AVAILABLE CAPABILITIES — a MENU of what you can call, NOT content"; and (4) the planner prompt now
  states a capability listing is not "real content" and it must read actual sources before
  answering. Regression test `test_discovery_listing_is_not_answer_grounding_content`.

- **Each finished deep task now carries its FULL output on the completion milestone**
  (`data.deep_output`), so a consumer can show exactly what the task produced rather than only a
  "Completed: <goal>" line.

- **A deferred deep run's output is now folded into the final answer, instead of being discarded.**
  When the planner routed to `answer` but the turn then escalated to a deferred deep run (because the
  message requested a change, or the answer only described unexecuted work), the deep worker did the
  real work but the user-facing reply stayed the pre-deep proposal — typically a "shall I proceed?"
  stub with zero awareness of what the deep run produced. The deliverable was emitted only as a side
  milestone (which some consumers, e.g. the Textual UI, truncate to its first sentence), so the real
  result was effectively lost and the turn looked like it stopped to ask permission for work it had
  already finished. Now, after a deferred deep run produces substantive output, the orchestrator
  re-synthesizes the final reply grounded in that output (new `SYNTHESIZE_AFTER_DEEP_PROMPT` +
  `_synthesize_after_deep`), grounds `context_view` in it, and still holds the synthesized reply to
  the overall goal via the answer-goal-verification loop (so the turn keeps going if the deliverable
  is incomplete rather than stopping half-done). Regression test
  `test_deferred_deep_output_is_folded_into_final_answer` in `tests/test_orchestrator.py`.

- **Progress narration no longer repeats itself.** When narration is on, the per-step beats were
  drifting into the same line reworded several times ("looking up the details of your marathon
  quest" six times). Two changes: (1) the planner's narration instructions are tighter and the
  re-plan instruction now tells it to react to what it just found in GATHERED and say what that
  makes it check next (bridge insight to intent), not narrate a read in isolation; (2) `Narrator`
  now backstops with a normalized near-duplicate check (`_is_repeat`/`_norm`) so a paraphrase of an
  earlier beat is dropped, not just an exact echo. Narration tests in `tests/test_ux_features.py`.

- **The planner now distinguishes a QUESTION from a COMMAND, so asking ABOUT something is answered
  instead of being executed as a task.** Routing was keyword-driven: a message containing an action
  verb ("fix", "add", "change", "build", "update", "show"…) was treated as work to do, even when the
  user was only ASKING — "how would I add a field?", "what would it take to fix the back button?",
  "should we refactor this?" These were mis-handled as deep tasks instead of being answered. Two
  changes fix it: (1) the `PLANNER_PROMPT` now opens with an explicit QUESTION-vs-COMMAND gate that
  decides intent BEFORE the code-change rule — an interrogative ("how/what/why/should we/is it…") or
  a "?" message that asks about something is a QUESTION → answer; a plain or polite imperative ("fix
  X", "can you add…", "please update…") is a COMMAND → deep; when torn, answer. (2) the message-intent
  escalation fallback `_message_requests_change()` no longer auto-escalates a question that merely
  contains a change verb: an interrogative message returns `False` (answer), while a polite imperative
  aimed at the assistant ("can you fix…", "please add…") still returns `True` (execute). Genuine
  commands and bug reports are unaffected. Regression tests in
  `tests/test_orchestrator.py::test_message_requests_change_distinguishes_questions_from_commands` and
  `::test_question_with_change_verb_is_answered_not_executed`.

- **Async hand-off deep runners no longer trigger a runaway relaunch loop.** A `DeepRunner` that
  does not execute inline but queues the real run to finish out-of-band (e.g. a chat runner that
  creates a tracked task and returns a `DeepResult(met=True, output="task #N launched")` sentinel)
  was being re-verified by the deep-goal loop: the verifier judged the sentinel `output` against
  the goal, found it not-met, escalated the model and RELAUNCHED a fresh task — every iteration, up
  to `deep_goal_max_iterations`. The result was N phantom tasks per message and a stream that ended
  in error with no reply. `DeepResult` gains a `deferred: bool = False` flag; when a run is
  `deferred`, the goal loop TRUSTS its `met` and stops (no re-verify, no relaunch). The real outcome
  is verified when it reflects back. Inline runners are unaffected (default `False`). Regression test
  in `tests/test_per_goal_context_iteration.py::test_deferred_handoff_runs_once_and_is_not_reverified`.

- **Context-card RETRIEVAL quality: vector-selected cards now RESOLVE their references, and an
  unrelated query returns no card.** Two retrieval bugs made learned cards unusable end to end even
  though card LEARNING was correct:
  1. **References were resolved only in the keyword arm.** Resolution lived inside
     `FileContextStore._render_card_content`, so a card SELECTED by the semantic
     `VectorContextAssembler` rendered only its description and silently dropped the live
     collection / conversation data it pointed at. The render logic (recency+relevance ranking,
     budgeted resolution through the resolver registry) is now extracted into ONE shared routine,
     `adapters/card_content_render.py::render_card_content`, used by BOTH arms. `VectorContextAssembler`
     gained a `reference_resolvers=` param and resolves each selected card's `content` via the shared
     routine; `QdrantCardVectorStore.search` now forwards the card's `content` (and `card_id`) in the
     hit payload so the vector arm has the references to resolve. `FileContextStore` is unchanged in
     behavior (it delegates to the same routine).
  2. **No relevance cutoff: every query returned ALL of a user's cards.** The confidence gate now
     applies to the hit's RAW similarity score (recency decay only re-orders hits, it never pushes a
     still-similar hit below the floor), making a Voyage-cosine card floor meaningful. Calibrated
     against the card-quality eval (on-topic cards score ~0.50-0.58, incidental ones <=0.41), so a
     ~0.45 floor admits a topic query's own card and drops every card on a truly unrelated query.
     The global default stays the permissive `0.0`; the card wiring sets the floor explicitly.
  Verified by `evaluation/card_quality_eval.py` (real model + Voyage): 0/4 -> 4/4 select+resolve and
  1/1 clean-on-unrelated, stable across runs. New offline regression tests in
  `tests/test_vector_context.py::TestVectorArmCardResolution` lock the vector-arm resolution and the
  raw-score gate. Generic by construction: all resolution still goes through the injected resolver
  registry (a type with no resolver degrades to an unresolved-pointer line).

### Changed
- **Terminal UI: deep-run output is bigger live, scrollable when expanded, and kept on screen after
  each task finishes.** The calm inline deep dashboard now shows a few legible lines per run (3 for a
  single run, tightening to 2/1 as runs go concurrent) instead of a single cramped line. The expanded
  detail panel (`d`) is now a real scroll region holding a run's ENTIRE history (it no longer caps at
  a 22-line tail): it grows to fill the screen, auto-follows the tail, and pages back/forward with
  `PgUp`/`PgDn` (paging up pauses follow; scrolling back to the bottom resumes it). Most importantly,
  each deep task's FULL output is now written into the scrollback transcript the moment it finishes
  (detected via the `EVENT_EXEC` terminal phase, with a turn-end flush as a fallback for cancelled or
  phase-less runs), so what each deep task did stays readable after the live widgets are hidden.
  Tests in `tests/test_deep_output_ui.py`.

### Added
- **Conversational stage narration: one continuous "thinking out loud" train of thought
  (`OrchestratorConfig.narrate`, `narration_system_prompt`).** When `narrate=True`, the orchestrator
  narrates the turn as ONE evolving human train of thought rather than emitting hardcoded status
  labels. The flow is a HYBRID with two beat sources, both emitted as `EVENT_PARTIAL` (so a consumer
  shows them live and speaks them on voice) and both in the selected rep's voice (`rep_preamble`):
  - **The opening beat (instant ack)** is one cheap call on the planner tier, started concurrently
    with context assembly so it adds NO wall-clock latency on quick turns. It is grounded in the new
    message + recent conversation + persona, and (having read nothing yet) claims no findings, so it
    cannot fabricate. HOW it speaks is overridable by the consumer via `narration_system_prompt`
    (the persona is layered on top).
  - **Every later beat is the planner's OWN `rationale`** — when narrating, the planner writes its
    `rationale` field as a short, spoken, in-persona line about what it is doing/noticing now,
    grounded in the gathered observations, in the planning call it ALREADY makes (Approach B: zero
    extra LLM call). The orchestrator relays it via `Narrator.relay()` at read/deep stages, so the
    beat reflects genuine, data-grounded reasoning and quick (answer-only) turns add nothing. The
    `plan`/`replan` event carries no duplicate text while narrating (the rationale is spoken as the
    beat instead of shown as an expandable detail).
  This SUPERSEDES and folds in the old `instant_ack` path (the ack is just the first beat, not a
  separate mechanism; `instant_ack=True` still works and routes through the narrator). Every
  narration failure is swallowed: the turn never depends on it. `Narrator` in `core/orchestrator.py`;
  em dashes are stripped defensively and consecutive beats are space-separated (brand-voice safe).
- **Pluggable card PERSISTENCE boundary: `CardRepository` (`adapters/card_repository.py`).** A tiny
  `CardRepository` Protocol (`load_all` / `read` / `write` / `delete` / `exists` / `revision`, every
  method BEST-EFFORT and NEVER raising) plus the default `FilesystemCardRepository` (one
  `<cards_dir>/<id>.json` file per card, atomic temp-file + `os.replace` writes, and a cheap
  `(max_child_mtime, file_count)` change-stamp for `revision()`). `FileContextStore` now routes
  EVERY card read / write / delete / enumerate / cache-invalidate through an injected repository:
  the constructor still takes `cards_dir` and builds a `FilesystemCardRepository(cards_dir)` by
  default, and accepts an optional `card_repository=` override so a consumer can persist cards in a
  database / vector store while ALL card logic (selection / IDF / recency / the card-update API /
  `export_for_embedding` / bootstrap) stays in the store. The in-memory cache now invalidates on the
  repository's `revision()` (the old `_dir_stamp` filesystem scan was removed). Fully behavior-
  preserving: with the default filesystem repo the on-disk format and selection are byte-for-byte
  unchanged (all existing card/context/vector tests pass).
- **Optional native text-search hook on the repository boundary (`search_cards`).** A repository MAY
  additionally expose `search_cards(query, *, limit) -> Optional[Dict[str, Dict[str, Any]]]` to
  serve the keyword/IDF arm directly from a native full-text store (e.g. a Qdrant-backed repo)
  instead of the store scanning every card in memory. `FileContextStore.assemble()` detects the
  capability by duck-typing (`hasattr`, never an isinstance check): when present and returning a
  non-`None` set, those cards become the candidate pool for the keyword arm (the store then applies
  its existing IDF ranking / confidence gate / recency over them); when absent or returning `None`
  the store falls back to today's in-app IDF over `load_all()`. Purely additive: the default
  `FilesystemCardRepository` does NOT implement `search_cards`, so its behavior is unchanged.
- **Generic Qdrant-backed card persistence: `QdrantCardRepository` + `QdrantCardVectorStore`
  (`adapters/qdrant_card_repository.py`, behind the `[qdrant]` extra).** A reusable
  `CardRepository` that persists context cards as points in ONE Qdrant collection (no `cards_dir`),
  so any consumer can store cards in Qdrant by wiring config/connection only — no consumer
  reimplements a card repo. The connection mirrors `QdrantVectorStore`: pass an existing `client=`,
  OR `url=`/`api_key=` for a server, OR neither for an EMBEDDED local Qdrant under `path`. It takes
  an `embedder` callable (`texts -> vectors`, e.g. `make_voyage_embedder`), a `collection`, a
  `vector_size`, and an OPTIONAL `scope` dict (payload key/values for multi-tenant isolation, e.g.
  `{"user_id": ...}`) — every read/write/delete/search is filtered by `scope` so a scoped card
  never leaks. `write` derives the card's embed-text via the NEW shared `card_embed_text` helper
  (so it MATCHES `FileContextStore.export_for_embedding`), embeds it ONCE, and upserts one point
  `{id from (scope, card_id), vector, payload: card + scope + a flat _search_text field}`;
  `search_cards` uses a Qdrant full-text index + `MatchText`. `QdrantCardVectorStore` is a
  query-only `VectorStore` over the SAME collection (`upsert`/`sync` are no-ops), so each card is
  embedded exactly once. All methods are best-effort and never raise. Wired by env via
  `QAR_CARDS_BACKEND` (`file` default | `qdrant`): when `qdrant`, `resolve_context_assembler`
  builds a `QdrantCardRepository` from `QAR_CARDS_COLLECTION` / `QAR_QDRANT_URL` /
  `QAR_QDRANT_API_KEY` + the `QAR_EMBEDDER_BACKEND` embedder and passes it as the default
  `FileContextStore(card_repository=...)`, with the query-only `QdrantCardVectorStore` as the vector
  arm; `file` keeps today's behavior byte-for-byte.
- **Shared `card_embed_text` helper (`adapters/card_repository.py`).** The single source of truth for
  a card's embed/search text (name + description/summary + content note/why + keywords). Both
  `FileContextStore.export_for_embedding` and `QdrantCardRepository.write` call it, so the vector
  arm's seed/sync text and an embedding repo's write-time text can never drift.
- **Source-agnostic context-card CONTENT (cards are no longer file-only).** A context card
  (`adapters/file_context_store.py`) can now carry an optional top-level `content` list of TYPED
  items, each either a REFERENCE resolved FRESH to current content on every use, or an LLM NOTE
  (synthesized text). Files become just ONE reference type, so a card may have zero `files[]` and
  still be selectable, renderable, and embeddable. Item shape:
  `{"id", "type": "file|collection|conversation|query|note", "locator": {…}, "ts": <epoch float>,
  "why": "<short>"}` (for `note`, `locator = {"text": …}`). Fully additive: a card with no `content`
  and no resolvers wired behaves byte-for-byte as today's file-only card (the existing
  `file_context_store` tests pass unchanged).
- **`ReferenceResolver` framework (`adapters/reference_resolver.py`).** A tiny `ReferenceResolver`
  Protocol (`resolve(locator, *, max_chars) -> str`, NEVER raises, returns `""` on failure) plus a
  `{type: ReferenceResolver}` registry. Built-in resolvers ship for `note` (returns the locator
  text) and `file` (re-reads the live file fresh through the store's own path, reflecting
  staleness). The data-backed types (`collection`, `conversation`, `query`) are CONSUMER-INJECTED so
  the library stays generic; an un-wired type degrades to a graceful unresolved-pointer line
  (e.g. `[collection ref: <name>/<id> (unresolved)]`) instead of failing. `build_resolver_registry`
  merges built-ins with consumer resolvers (consumer wins on collision). Wired through a new
  `RunnerConfig.reference_resolvers` field and into the default `FileContextStore` in
  `resolve_context_assembler`.
- **Recency-bounded content resolution in `assemble()`.** Because a card's content can grow
  unbounded, each selected card ranks its content by recency (`ts`) plus relevance to the task
  (term overlap), resolves only the top-N within a char budget, and skips/trims the rest. New
  `FileContextStore` constructor knobs `max_card_refs` / `max_card_ref_chars` (module constants
  `_MAX_CARD_REFS=8`, `_MAX_CARD_REF_CHARS=4000`, `_MAX_CARD_CONTENT_ITEMS=200`). Never raises.
- **Card-update API (read-modify-write) on `FileContextStore`.** `add_content(card_id, item)`,
  `update_content(card_id, item_id, new_item)` (correction; appends if the id is unknown),
  `remove_content(card_id, item_id)`, and a batched `update_card(card_id, add=, replace=, remove=)`.
  Each is a safe atomic read-modify-write that normalizes the item, applies the recency trim, and
  persists. `record()`/`_record_inner` are generalized to also append non-file content via
  `outcome["content"]` (a list or single dict) without changing their file-pinning behavior. An
  async LLM updater can use this later; the API and tests land now.
- **No-file cards flow end to end.** `_card_term_weights` now tokenizes content `why`/note text (so
  a pure note/collection card is IDF-selectable), and `export_for_embedding` includes note text +
  content `why` (so such a card is vector-searchable). New built-ins exported from
  `adapters/__init__.py`: `ReferenceResolver`, `NoteResolver`, `build_resolver_registry`,
  `make_file_resolver`. New offline tests in `tests/test_card_content.py` (stub resolvers).
- **Async post-deep context-card updater (prepare for the FUTURE after a deep run).** After a deep
  task finishes (answer already delivered), an ASYNC, best-effort LLM process updates THIS user's
  context cards so the next similar request starts better-grounded. Two parts in
  `core/orchestrator.py`:
  (1) when the updater is active, each deep brief is appended with
  `DEEP_FUTURE_CONTEXT_INSTRUCTION`, asking the worker to END its output with a machine-parseable
  section after the line `=== FUTURE CONTEXT (for similar requests by this user) ===`;
  `_parse_future_context()` slices that section back out (last delimiter wins; absent -> "").
  (2) `_update_cards_after_deep_async()` spawns a background daemon thread (never blocks the answer,
  never affects the `OrchestratorResult`, never raises) that gathers the request + what executed +
  the parsed future-context + the user's CURRENT relevant cards (`assemble().card_metadata`), makes
  ONE cheap `balanced`-tier LLM call (forced `card_edits` tool, falling back to text parsed with the
  repo's `_extract_json`) returning a STRUCTURED edit plan, and applies it via the card-update API
  (`update_card(fields=, add=, replace=, remove=)`): name/description `fields`, content `add`
  (preferring resolvable `collection`/`file` references over copied snapshots, a `note` only when
  nothing external can be pointed at), `replace` corrections of stale items, and `remove`. Edits are
  user-scoped (card ids prefixed `u:<user_id>:` from `_ctx_meta['user_id']`, so cards never leak
  across users) and bounded (`async_card_update_max_cards`, `async_card_update_max_edits_per_card`).
  Card-update capability is detected GENERICALLY by `_card_update_store()` (any object exposing
  callable `update_card` + `add_content`, unwrapping composite/hybrid assemblers), not by a
  `FileContextStore` type check. New `OrchestratorConfig` toggle `async_card_update: bool = True`
  (env `QAR_ASYNC_CARD_UPDATE=0/1` in the CLI); when off, or when no card-update store or no provider
  is wired, the deep loop is byte-for-byte unchanged (no future-context block appended, no LLM call).
  Centralized prompts: `DEEP_FUTURE_CONTEXT_INSTRUCTION` and `CARD_UPDATE_PROMPT`/`CARD_UPDATE_TOOL`
  (no em dashes). New offline tests in `tests/test_async_card_update.py`.

### Changed
- **Selection/render algorithm extracted into pure module-level functions in `conversation_format`.**
  `select_current_slice(messages, query, *, recent_turns, max_chars)` and
  `select_related(conversations, query, *, max_convs, max_chars, get_conv_id)` are now standalone
  pure functions in `adapters/conversation_format.py`, operating on plain message/conversation dicts
  and returning `(rendered_text, metadata_list, truncated_flag)` tuples. The shared helpers
  (`_msg_role`, `_is_user`, `_render_turn`, `_relevance_doc`) and constants
  (`_USER_SCORE_BOOST=1.5`, `_USER_VERBATIM_CAP=2000`, `_AI_COMPACT_CHARS=400`) are also defined
  at module level there. `SessionFileConversationStore.current_slice` and `.related_slices` delegate
  to these functions after resolving keys and applying scope filtering; behavior is identical.
  Any other `ConversationStore` backend (Mongo, etc.) can now reuse the same algorithm without
  duplicating it.

- **Per-deep-goal context + a verifier that drives context-widening and tier escalation.** When the
  planner fans deep work into N goals, each goal now selects its OWN context instead of sharing one
  run-level view: `_run_deep` builds a per-goal block from the wired `context_assembler.assemble(goal)`
  plus a relevant `conversation_store.current_slice(conv_id, goal)`, and threads THAT into the goal's
  `context_preamble`. The goal verifier (`_verify_goal` / `VERIFY_GOAL_TOOL` / `VERIFY_GOAL_PROMPT`)
  now also returns `need_more_context` (bool) + `context_query` (string) + `next_tier` (one of the
  registry tiers, optional), parsed robustly (a string reply is recovered with the shared
  `_extract_json` helper; all three default to False/""/None on any parse miss). When a goal is NOT
  met and `need_more_context` is set, the loop pulls MORE context for the next iteration
  (`_widen_for_goal`: a fresh assembler read for the named missing context, WIDER conversation
  retrieval via `related_slices`, and a targeted `retrieval.grep`), the widening growing each round so
  a retry always sees more than the last, and runs the next attempt at the verifier's `next_tier`
  (resolved through `registry.resolve_tier`, kept worker-runnable) or one rung up the deep-model
  ladder. Bounded by the existing `deep_goal_max_iterations` cap + token budget. Visible STATUS ticks
  fire when per-goal context is selected, when more context is fetched, and when the tier changes. NO
  em dashes in the verifier prompt. Fully additive and guarded: with exactly one goal and no
  assembler/store wired, behavior is byte-for-byte unchanged. `run()` forwards a `ctx_meta` (carrying
  `conv_id`/`conv_scope`) into `_run_deep` so the per-goal helpers can reach the conversation store.
- **`ConversationStore.current_slice` selection: minimal "always in" + user/AI asymmetry.** The
  reference `SessionFileConversationStore.current_slice` no longer force-includes the last
  `recent_turns` messages raw. The ONLY guaranteed turn now is the **last USER turn** (the actual
  intent); everything else (recent AI turns, older turns) is a relevance CANDIDATE selected by
  TF-DF-IDF, not auto-included by recency. `recent_turns` is now the "considered window" (default
  lowered to 4): the last N turns merely join the candidate pool. USER turns are PREFERRED (a x1.5
  score boost) and render VERBATIM (capped only if absurdly long); AI turns earn inclusion purely by
  relevance, even the latest one, and are always COMPACTED. Per-turn relevance is LENGTH-NORMALIZED
  so length alone never wins, and an AI turn's TF-DF-IDF *document* is its compact form so a long AI
  answer cannot dominate df/idf. The last USER turn is guaranteed present even after `max_chars`
  truncation (the last message is the fallback anchor when there is no user turn). `related_slices`
  now applies the same AI compaction to its per-conversation tail. No method signatures changed (a
  separate Mongo implementation mirrors them); the `ConversationStore` Protocol docstring documents
  the new `recent_turns` meaning. No new LLM call is added: the no-LLM TF-DF-IDF + compaction is the
  pruning, and Step 1 stays one resolve round-trip.

### Added
- **`compact_message(text, *, max_chars=400)` helper** (`adapters/conversation_format.py`): compacts
  a long message to its beginning + end plus the few most salient MIDDLE sentences/lines, selected
  with TF-DF-IDF at sentence/newline granularity (each chunk is one document, reusing
  `extract_terms` / `select_representatives`), joined with an ellipsis marker within `max_chars`. A
  short message is returned unchanged. Never raises (falls back to head+tail truncation). Used to
  compact AI turns in the conversation store so a long AI answer cannot dominate downstream df/idf.
- **User Input Understanding (Step 1) + storage-agnostic `ConversationStore`.** The brain now
  treats resolving WHAT the user means as a first-class step that runs ONLY when needed. A new
  `ConversationStore` Protocol (`core/adapters.py`, with `ConversationContext` value object) offers
  `current_slice(conv_id, query, ...)` (always the last N turns PLUS TF-DF-IDF-selected relevant
  older turns of the SAME conversation) and `related_slices(query, scope, ...)` (TF-DF-IDF slices
  from OTHER conversations in scope); both NEVER raise. `Orchestrator.run()` gains keyword-only
  `conv_id` / `conv_scope` (full back-compat). When a store is wired AND a `conv_id` is present AND
  a cheap NO-LLM gate (`_needs_context_to_understand`) flags a short/anaphoric/acknowledgement input,
  the brain pulls the current slice and asks the model ONCE to rewrite the message into a
  self-contained GOAL CONDITION (`RESOLVE_REQUEST_PROMPT`). A `MORE_CONTEXT_NEEDED` reply pulls
  related conversations and retries ONCE; a `CLARIFY:` reply short-circuits the turn to a terminal
  confirm (executor → needs_you) without running the planner. On a successful resolve it emits a
  new streamed `EVENT_UNDERSTANDING` event (in `SURFACING_EVENTS`) and prepends the pulled
  conversation context + resolved request to `context_view`; context SELECTION (Step 2) then targets
  the resolved request while the planner still receives the user's LITERAL words. Self-contained
  inputs skip Step 1 entirely (no LLM hop, ZERO added latency); with NO store wired, behavior is
  byte-for-byte today's. New reference impl `adapters.SessionFileConversationStore` (local Claude
  session files) reuses the TF-DF-IDF sampler and a new shared `adapters/conversation_format.py`
  module factored out of `ClaudeConversationsAdapter` (its public behavior is unchanged). Wired via
  `RunnerConfig.conversation_store`; the executor builds a `conv_scope` from the task and passes
  `conv_id`/`conv_scope`, and only falls back to dumping the full transcript when no store is wired.
- **WebSearchAdapter**: new `RetrievalAdapter` that grounds the shallow orchestrator loop in live
  web data via the Tavily API. Uses stdlib `urllib.request` only (no extra deps). Enable with
  `WEB_SEARCH_ENABLED=true` and `WEB_SEARCH_API_KEY=tvly_...` env vars. `WEB_SEARCH_MAX_RESULTS`
  controls result count (default 5). When enabled, the CLI wires it alongside `FilesAdapter` via
  `CompositeRetrievalAdapter` so local corpus and web search are both queried. The adapter is
  graceful: every method catches all exceptions and returns `Observation(kind="error")` rather than
  raising. Exported from `quest_ai_runner.adapters` as `WebSearchAdapter`.
- **`derive_capabilities` web detection**: when a `WebSearchAdapter` with a configured API key is
  in the retrieval stack (directly or inside a `CompositeRetrievalAdapter`), `derive_capabilities`
  now correctly reports `web: true` even without a deep runner. This lets the Quest backend route
  web-research tasks to a runner that has shallow web search but no subprocess deep-runner.

### Changed
- **Deep execution now runs on our own goal-verification loop instead of Claude Code's `/goal`.**
  `/goal` re-checks its condition inside the worker every turn (token-heavy) and caps the condition
  at 4000 characters, rejecting a longer one and then running nothing. The worker is now invoked
  headless (`-p`) with the done-standard given as plain context; after it runs, the brain verifies
  the done-standard AT THE QUALITY BAR via one cheap LLM call (`_verify_goal`), judged through the
  AI rep persona and the GUIDANCE CARDS selected for the input (guidance = the quality standards).
  If unmet, the brief is augmented with why it fell short and what to do next, and it re-runs up to
  `deep_goal_max_iterations` (default 3). `DeepResult.met` now reflects the brain's verification,
  not just the worker exit code, so a silent no-op or a sub-standard result is a confirmed failure.
  Parallel subgoals carry the OVERALL goal in their prompt so each stays aligned with the larger
  goal (hierarchical goal: overall user goal -> subgoals).
- **The goal loop now applies to EVERY input, not just deep execution.** A plain ANSWER is also
  pursued as a goal: when a quality bar is wired (a GuidanceProvider), the written answer is verified
  against the user's goal at that bar and regenerated with steering until it meets the bar or
  `answer_goal_max_iterations` is reached. Every deep process prompt carries the top input-level goal
  (the user's actual request) plus, in a fan-out, the overall goal it serves, alongside its own
  done-standard; retries carry the prior output and the verifier's feedback. New: `run()` /
  `run_stream()` accept a `pending_inputs` callable so new user messages that arrive mid-run are
  folded into the next deep process / answer retry (inert when the consumer does not supply it).
- **The deep loop auto-selects and escalates the worker model, bounded by a token budget.** The
  deep worker is Claude Code (Claude models only); the loop manages a Claude model ladder
  (`QAR_DEEP_MODELS`, default `haiku,sonnet,opus`): start at the fast Anthropic tier, escalate to a
  stronger model each time the goal is not met. An explicit per-task model request (a `model_hint`
  that resolves to a Claude model) or a guidance card `model:` preference pins the model (no
  escalation). The fixed 3-attempt cap is replaced by an overall **token budget**
  (`QAR_GOAL_TOKEN_BUDGET`, default 300k): the worker runs with `--output-format json`, its reported
  tokens/cost are parsed into `DeepResult.tokens`/`cost_usd`, and the loop continues while under
  budget (`deep_goal_max_iterations`, raised to 8, is now a hard safety cap). A non-Claude model id
  is never passed to the Claude Code worker.
- **Mid-run messages are a generic abstraction.** `core/inbox.py` adds `InputInbox` +
  `InMemoryInbox`; the Orchestrator takes an `input_inbox` and `run()` auto-drains the current
  conversation (keyed by quest/conversation/session/user id) when no explicit `pending_inputs` is
  given. `build_orchestrator` wires a default inbox, so any interface (chat, Quest frontend, ...)
  only pushes new messages with `inbox.push(conversation_id, message)` and the brain folds them in.

### Fixed
- **Actionable requests now EXECUTE instead of ending as a proposal.** The cheap planner often
  routes a change request to `action="answer"`, proposes the change in prose, and forgets to set
  `answer_contains_work_to_execute`. The previous escalation nets only matched specific ANSWER
  phrasings (past-tense claims, "I need to update X"), which a model like gemini rarely produces
  verbatim, so a proposal such as "Aligning these to use the same created_at field will guarantee
  ..." matched nothing and the turn finished without doing the work. The orchestrator now also
  escalates by **user-message intent**: `_message_requests_change()` (imperative change verbs +
  bug/wrongness signals, excluding pure how/what/why questions) detects an actionable request from
  the stable message; when a deep runner is available and nothing executed, it runs a deep step
  whose brief carries the assistant's proposed approach so the run APPLIES it. Emits a visible
  status and logs the escalation. The earlier `_answer_describes_unexecuted_work` net (dropped in a
  refactor that left it defined but uncalled) is also re-wired as a fallback, now recognizing "the
  fix is to update X" / "this needs to be updated". Plain informational answers still do not escalate.
- **Interactive chat no longer crashes on a deep turn.** `InteractiveSession._run_turn` called
  `self._ensure_ai_label()` for a `kind="deep"` result, but that helper lives on `_TurnRenderer`,
  so every deep turn raised `AttributeError: 'InteractiveSession' object has no attribute
  '_ensure_ai_label'` (`OrchestratorResult` never sets `.text` for deep, so the broken branch
  always ran). It now calls `renderer._ensure_ai_label()` and shows the "planned, use /execute"
  hint only when no `DeepResult` actually executed, so it no longer misfires after a real run.
- **Quest decision summaries are capped at the 4000-char condition limit.** A verbose planner
  question/clarification could produce a decision summary that Quest stores as a goal CONDITION
  (max 4000 chars), so the POST was rejected with "Goal condition is limited to 4000 characters
  (got NNNNN)" even when the user's input was short. `QuestClient.create_decision` now truncates an
  over-long summary at the single boundary to Quest, so any caller's summary is bounded.

### Changed
- `FileContextStore.bootstrap()`: now uses an LLM (via the wired `ModelProvider`) to identify semantic topic cards across the codebase — a topic can span files from completely separate directories. The number of cards reflects the natural structure of the codebase, not a preset range. Without a provider, bootstrap is a no-op (cards accumulate via `record()` instead).
- **Bootstrap Stage 3 now dedups via keyword-clustering + an LLM merge decision** (replaces the old Jaccard file-overlap merge). Cards that share at least 2 keywords are clustered transitively (union-find); each multi-card cluster gets ONE LLM call asking which cards describe the same concept and should be merged. New cards are also deduped against the cards already on disk, folding a duplicate new card into the existing card's id rather than writing a divergent one. With no provider it falls back to a keyword-union merge (Jaccard >= 30%).
- **`FileContextStore.bootstrap()` is now INCREMENTAL.** It diffs the walked files against the existing cards: only files referenced by NO card (uncovered) drive a fresh 3-stage LLM fan-out, and only cards whose pinned files changed (stale) are regenerated. When everything is covered and unchanged the bootstrap is a no-op returning 0. (A second `bootstrap()` over an unchanged corpus therefore returns 0, not the card count.)
- `_walk.py`: add `site-packages` to `_BASE_SKIP_DIRS`
- Orchestrator: context assembly now runs in a background thread concurrent with the instant-ack; collected with a 3 s timeout so corpus search never blocks interactive responses
- Orchestrator: emits `"searching corpus…"` status when a context assembler is wired
- **`TurnMemory` replaced by `TurnContextStore` + `CompositeContextAssembler`.**
  Conversation turns are now stored as card files (the same format as `FileContextStore` file
  cards) under `.quest-context/turns/` and retrieved by the same IDF keyword-overlap stack.
  This means turn history benefits from semantic retrieval when a `VectorContextAssembler` is
  also wired. `TurnMemory` (keyword-only, in-memory) has been removed entirely.
  `InteractiveSession` now wires a `TurnContextStore` into the orchestrator's
  `context_assembler` automatically: if a `context_assembler` is already set the session wraps
  it with a `CompositeContextAssembler([existing, turn_store])`; otherwise it sets the
  `TurnContextStore` directly. A single-turn `_last_transcript()` buffer continues to provide
  immediate conversational continuity for the current pair. The orchestrator's `record()` call
  now includes `"response": res.text` in the outcome dict so `TurnContextStore.record()` can
  write complete turn cards. `/clear` resets the single-turn buffer (turn store cards are
  durable and are not cleared). `docs/context-assembly.md` updated to match.
- **`_run_deep` now receives `gathered` and includes the brain's specific reads in
  `context_preamble`.** When the orchestrator reads files before deciding to go deep, those
  reads (in `gathered`) are now forwarded into the deep runner's `context_preamble` alongside
  any `rep_preamble`, so the subprocess does not have to re-discover what the brain already
  found. The preamble is built as `rep_preamble` (if any) followed by a `--- RELEVANT CONTENT
  FOUND BY THE BRAIN ---` section rendered from `gathered`. The `wants_preamble` gate now fires
  whenever the runner accepts `context_preamble` (not only when `rep_preamble` is set), so
  gathered-only runs also benefit. `_guard_turn` also threads `gathered` through to its
  remediation `_run_deep` call.

### Added
- **`quest-ai-runner bootstrap` CLI subcommand.** Builds or refreshes the context card store for a corpus on demand: `--corpus PATH` (default `QAR_CORPUS_ROOT` or cwd) and `--cards-dir PATH` (default `QAR_CONTEXT_CARDS_DIR` or `<corpus>/.quest-context`). Uses the env-selected model provider, runs the incremental bootstrap, and prints the card count.
- **Bootstrap version metadata + auto re-index.** A successful `bootstrap()` writes a `bootstrap_meta.json` sidecar to the cards dir recording the bootstrap algorithm version, card count, and UTC completion time. On startup, `_bootstrap_if_needed` compares the stored version against the current `_BOOTSTRAP_VERSION`: when the stored cards were built by an older algorithm it re-indexes the corpus in the background (chat stays available immediately) instead of only refreshing stale cards.
- **`quest-ai-runner chat` — a polished interactive (attended) session.** A multi-turn REPL over
  the orchestrator brain that streams every `ProgressEvent` to the terminal in real time, keeps a
  rolling transcript so follow-ups share context, and attaches results to a Quest goal with
  `--goal-id` (or `/goal <id>` mid-session). The UX is built for daily use: an animated spinner runs
  on a background thread while the brain works; "chatter" (plan/read/replan/status/exec) collapses
  onto that ONE updating line so it stays subtle instead of scrolling; partial reply chunks type out
  in place under a bold `AI` label; the user's message is echoed under a `You` label; and a faint
  rule separates turns so history stays scannable. Key handling matches what people expect: ESC
  cancels the current turn while it streams (a raw-stdin watcher flips a `threading.Event` the stream
  loop checks), Ctrl+C clears the input line without exiting, and Ctrl+D exits. A short header shows
  the goal id (when set) and the shortcuts. The session lives in `quest_ai_runner.interactive`
  (`start_interactive` / `InteractiveSession`). The `[tui]` extra now also pulls in `rich` (alongside
  `prompt_toolkit`) for the rendering; both are OPTIONAL — the session degrades to plain
  `input()`/`print()` (no spinner animation, best-effort keys) when neither is installed, via the
  `_HAS_PROMPT_TOOLKIT` / `_HAS_RICH` pattern. Generic and consumer-free: it only talks to the brain
  through `run_stream`.
- **`quest-ai-runner send "<task text>"`** — enqueue a new AI task from the CLI and print its id.
  Routes to `QUEST_TEAM_ID` by default (override with `--team-id`), optionally attaches to a goal
  (`--goal-id`) and schedules a future run (`--at <ISO-8601 UTC>`); omit `--at` to run at the next
  poll.
- **Broken-promise guard — post-turn honesty check (auto-remediate then verify).** The
  `Orchestrator` now durably captures per-turn EXECUTION FACTS (which mutating deep actions ran and
  whether each SUCCEEDED or FAILED, from `DeepResult.met` plus `EVENT_EXEC` phase ticks) onto
  `OrchestratorResult.execution_record`. At turn finalization it guards ANSWER replies: a cheap
  STRUCTURAL gate (`text_claims_action`) engages only when the reply asserts a completed or imminent
  action (so plain informational turns pay ZERO model cost), then a focused verification call
  (`verify_supported`) decides whether the execution record backs the claim. On a mismatch it
  AUTO-REMEDIATES with ONE safe re-run, but ONLY when nothing actually executed this turn (no
  success AND no failure recorded) — an action that already ran, succeeded or failed, is NEVER
  re-run, since host actions are not guaranteed idempotent (the double-mutation safeguard). If still
  unmet, the reply is rewritten to be honest (`honest_rewrite`, no false success, no em dashes) and
  the result is flagged `claim_corrected` / `partial`. `TaskExecutor._report` maps a
  `claim_corrected` background-task answer to `needs_you` instead of `done`. Tunable and ON by
  default (`OrchestratorConfig.verify_claims=True`, `max_remediations=1`); the guard NEVER raises
  (any failure leaves the turn unchanged). New module `quest_ai_runner.core.guard`
  (`ExecutionRecord`, `ExecutionFact`, `text_claims_action`, `classify_exec_phase`,
  `verify_supported`, `honest_rewrite`, and the centralized `VERIFY_CLAIM_PROMPT` /
  `HONEST_REWRITE_PROMPT`). App-agnostic; shared by live chat and background tasks (one Orchestrator).

### Fixed
- **Image describe-fallback could be handed a foreign model id.** `Orchestrator` now accepts a
  `vision_model` and threads it into `prepare_attachments`, so a consumer wiring a SEPARATE
  vision describer (a vision-capable `vision_provider` distinct from a text-only answering
  provider) can name the model that describer should use. Previously the describe-fallback reused
  the ANSWERING model id, which is foreign to a distinct describer (e.g. a tier-alias or Gemini
  answer id passed to an Anthropic describer would fail), so images degraded to "could not be
  transcribed" notes instead of being read. Backward compatible: when `vision_model` is unset the
  prior behavior (reuse the answering model id) is unchanged, which is correct when the describer
  IS the answering provider.

### Added
- **Optional `GuidanceProvider` adapter — retrievable USE-CASE-SPECIFIC instructions.** A host app
  can now keep its ALWAYS-ON core prompt small by moving instructions that apply to only SOME inputs
  (product facts, feature-flow guides, behavior policies) out of the static prompt and into a
  retrievable corpus of opaque "guidance cards" (`GuidanceCard{id, title, relevance, body}`). The
  new `GuidanceProvider` role (Protocol + `GuidanceProviderBase` ABC, all methods "never raise")
  exposes `list()` (cheap catalog: id+title+relevance, no body), `read(id)` (one card with body, or
  None), and an optional `select(user_message, *, k, meta)` for semantic pre-selection. When wired
  via `RunnerConfig.guidance_provider` (or `Orchestrator(guidance=...)`), the orchestrator calls
  `select()` ONCE before planning and PREPENDS the chosen cards as an "APPLICABLE GUIDANCE" block to
  `context_view` (the same compose order the `ContextAssembler` uses), and the planner gains two
  discovery verbs, `list_guidance` / `read_guidance`, that flow through the SAME observation
  machinery as a read. A `read_guidance` for a card already pre-selected this turn returns a short
  de-dupe note instead of re-injecting the body. Cards are OPAQUE to the brain — it stays
  app-agnostic. Purely additive: a consumer that supplies no `GuidanceProvider` sees byte-for-byte
  today's behavior (no block, no new verbs in effect). `OrchestratorConfig.guidance_topk` (default
  3) tunes how many cards are pre-selected.
- **Three-arm context engine + compounding task memory.** Beyond the keyword `FileContextStore`,
  the runner now orients an agent semantically. The bootstrap extracts the code's OWN docstrings
  (no LLM) for rich summaries (measured: keyword routing 13/53 -> 53/93 on the eval set).
  `VectorStore` is a new pluggable adapter with a default local-filesystem Qdrant
  (`QdrantVectorStore`, `[qdrant]` extra, embedder pluggable: `bge-small` 53/80, `bge-base`
  60/86, SOTA API higher); `BM25ContentStore` (`[bm25]` extra) searches the actual file CONTENT
  (exact identifiers/phrases, parallel multi-query). `HybridContextAssembler` fuses keyword +
  vectors (union recall 93). A `VectorContextAssembler` records `task -> context` associations
  (the headline compounding memory): recency-weighted (yesterday outranks a year ago),
  merged/deduped by task slug, capped with oldest-first eviction. Cold-start seeds docstring
  cards into the vector store on first use, auto-updated (re-embed only changed). A confidence
  gate falls back to plain Claude Code when nothing is confident (never-worse). `RunnerConfig`
  gains `vector_store` (default becomes hybrid when set). UX: `OrchestratorConfig.instant_ack`
  (a concurrent one-second acknowledgment, no added latency) and `AssembledContext.sources`
  (transparency: which adapter surfaced what), emitted as a status. Multi-tenant `scope`
  threaded for the Quest org/team/quest path. Honest eval (routing + Claude Code A/B + LLM
  judge) on a repo copy, metrics in the README and `evaluation/`.
- **Opt-in reps run tasks AS their Quest persona by DEFAULT.** The rep-sync capability is still
  OFF unless a consumer supplies `RunnerConfig.rep_sync_resolver`, but the moment it is on, the
  default does the complete thing with NO extra glue: the poller resolves the rep, PULLS its Quest
  profile into the local skill file before the run, builds a per-run preamble from that file's
  MANAGED sections (persona + learned corrections, composed with the runner's context doctrine via
  `core.context_doctrine.compose_deep_preamble`), and injects it into the deep run so the task runs
  AS that rep. New `RunnerConfig.rep_sync_direction` (`"pull"` default | `"push"` | `"both"`) gates
  the sync: `pull` = Quest -> skill file before the run (Quest is the source of truth at execution
  time, no push-back); `push` = skill file -> Quest AFTER the run only (no pre-run pull, so no
  persona injection); `both` = pull then push. `validate()` reports an unknown value. Push-back and
  the pre-run pull are both best-effort: a sync failure is logged and NEVER fails the task.
  `TaskExecutor.execute(task, *, rep_preamble=None)` threads the preamble into
  `Orchestrator.run(rep_preamble=...)`, which forwards it to the deep run ONLY for a `DeepRunner`
  whose `run_goal` accepts a `context_preamble` kwarg (older runners are untouched, mirroring the
  `emit` opt-in). `SubprocessGoalRunner.run_goal` now accepts an optional per-call `context_preamble`
  that overrides its configured base preamble for that run. The brain stays generic: it only passes
  a string through; the persona content comes from the consumer's Quest profile. No
  `ContextAssembler` is required: a consumer that sets only `rep_sync_resolver` gets the rep's
  persona in the run. Fully additive: a consumer with no resolver, and any existing caller, sees
  IDENTICAL behaviour to before.
- **Context handling is ON BY DEFAULT.** `RunnerConfig.context_assembler` now defaults to an
  `_AUTO` sentinel: leaving it unset makes `build_orchestrator` wire a default `FileContextStore`
  (cards under `context_cards_dir`, or `<corpus_root|cwd>/.quest-context`) so the runner grounds on
  reusable context out of the box. Pass an instance to customize, or `None` to disable. A new
  `RunnerConfig.context_cards_dir` sets the default store's location. `Orchestrator.run()` gains a
  `context_meta` parameter, and threads it plus `quest_id` to `assemble()`/`record()` so a
  multi-tenant assembler (e.g. a Quest-backed one serving many users) can scope per user/team/quest.
- **Fifth adapter role: `ContextAssembler` (PRE-FLIGHT CONTEXT)** -- an optional adapter that is
  called ONCE, guaranteed, before the plan->gather loop starts. `assemble(task_text)` returns an
  `AssembledContext` (context_view string + optional model_tier_hint + card_ids + stale list); the
  orchestrator injects `context_view` into the planner prompt and, when no explicit `model_hint`
  was passed by the caller, applies `model_tier_hint` as the run's model override. `record(task_text,
  outcome)` is called after the run as a best-effort write-back (never raises). The adapter is
  wired via `RunnerConfig.context_assembler` and forwarded to `Orchestrator(context_assembler=...)`;
  omitting it gives exactly the prior reactive-gather behaviour. The Protocol (`ContextAssembler`,
  structural) and the ABC (`ContextAssemblerBase`, nominal) live in `core/adapters.py`; the value
  object (`AssembledContext`) is a plain dataclass alongside them. Reference implementation:
  `adapters.FileContextStore` -- a stdlib-only ContextAssembler backed by per-card JSON files
  (one file per task slug); selects cards by keyword overlap with the task, checks pinned file
  freshness via sha256 + mtime (and optionally git blob SHA), renders a context_view string, and
  writes back an upserted card on each run. Atomic writes (tmp + os.replace). No third-party deps.
- **Centralized prompt doctrine (`core/context_doctrine.py`)** -- `SUFFICIENCY_GATE` and
  `MODEL_TIER_GATE` are module-level constants woven into `PLANNER_PROMPT` at module load, so the
  sufficiency checklist (read-before-acting, cheap-pass discipline, context-dry signal) and
  model-tier escalation rule (haiku/sonnet/opus; escalate one tier on a failed verification) are
  always applied without duplicating text. Both constants contain NO literal `{`/`}` characters,
  so they concatenate safely into the `.format()`-able prompt string. `DEEP_CONTEXT_DOCTRINE`
  combines both gates into a block suitable for prepending to a deep-runner's `context_preamble`;
  `compose_deep_preamble(base, assembled)` builds the full preamble in one call (doctrine + base +
  optional assembled context_view), so spawned deep agents obey the same discipline as the
  orchestrator. The `PLANNER_PROMPT` constant is now assembled at module load from named parts
  (`_PLANNER_HEAD`, `_PLANNER_ACTIONS`, `_PLANNER_TAIL`) plus the two gate strings, keeping the
  `.format()` call in `_plan()` unchanged.

- **Multimodal (image) + file-attachment support (the runner owns multimodal)** — the text
  provider does not do multimodal, so the runner does. A new standard handler
  `core/attachments.py` (`prepare_attachments(attachments, *, model, provider, vision_provider,
  vision_model, max_attachment_bytes)`) takes in-memory attachment items
  (`{filename, mime_type, data: bytes, kind: "image"|"file"}` — the SAME shape for chat uploads
  and panel context-docs) and, per the answering model/provider, EITHER passes an image NATIVELY
  (base64 content block) when the model is vision-capable and the provider can send blocks, OR
  DESCRIBES it with a separate vision-capable provider (centralized `DESCRIBE_PROMPT`), OR
  extracts a non-image file's text best-effort by type (txt/md/csv/json/code direct; pdf/docx via
  an optional light extractor if installed; any other type accepted with a clear binary note —
  never raises). Returns `native_blocks` (for the answer) and `text_context` (for the planner +
  grounding). A new vision-capability seam `model_registry.is_vision_capable(model)` (keyed by
  model FAMILY via `VISION_FAMILY_PATTERNS`: Claude 3.x/4.x, Gemini 1.5/2.x/3.x, gpt-4o/4.1/
  o-series → vision; default False) is the ONE place capability is decided. `AnthropicProvider.
  answer()` now passes a content-block LIST through to the SDK unflattened (plain-string path
  unchanged); `ClaudeCliProvider` degrades a block list to text and never crashes (and declares
  `supports_native_images = False`). `Orchestrator.run()`/`run_stream()` take a new optional
  `attachments` list, fold the prepared text into the planner CONTEXT, and ride native image
  blocks on the final answer message; the Orchestrator takes an optional `vision_provider` for the
  describe-fallback (wired from `RunnerConfig.vision_provider`). Per-attachment 50 MB cap
  (`config.MAX_ATTACHMENT_BYTES` / `attachments.DEFAULT_MAX_ATTACHMENT_BYTES`); attachments are
  processed CONCURRENTLY. Offline tests cover the capability map, the SDK image passthrough, the
  CLI degrade, native vs describe vs extract vs oversize vs unknown-binary, and the orchestrator
  threading.
- **Deep-run escalation marker (`QAR-ESCALATED: <decision_id>`)** — a spawned deep worker that
  raises a human-only decision mid-run (via whatever escalation mechanism its consumer preamble
  provides) can now report it back to the runner by printing `QAR-ESCALATED: <decision_id>` on its
  own line. `SubprocessGoalRunner` parses the marker (new `ESCALATION_MARKER` /
  `extract_escalation_id` in `core/goal_runner.py`, exported from `core`) and returns
  `DeepResult(met=False, decision_id=...)` regardless of exit code, so the executor reports the
  task as `needs_you` with the decision linked — the ask surfaces in the consumer's UI attached to
  the paused task instead of the task closing as done/failed. `GoalRunner.run` also normalizes
  `met=True` + `decision_id` to not-met so a custom runner can never report a paused run as done.
  Workers that never print the marker behave exactly as before. Offline tests cover marker parsing
  (last-marker-wins, bare marker ignored), the subprocess runner, the normalization, and the
  executor's `needs_you` report + chat post.
- **Configurable planner tier + answer timeout (env)** — the CLI now reads two optional env vars so
  a consumer can tune the brain without code: `QAR_PLANNER_TIER` sets the model tier for the planner
  step that picks read/answer/deep (default stays the cheap `haiku`; raise to e.g. `sonnet` when
  tasks are mostly real work so answer-vs-deep routing is decided by a more capable model), and
  `QAR_ANSWER_TIMEOUT` raises the per-call wall-clock cap for the `claude_cli` planner/answer backend
  above its conservative 180s default (headless completions over a large corpus can exceed it).
  Both are plumbed through existing config (`RunnerConfig.orchestrator.planner_tier`,
  `ClaudeCliProvider.timeout_seconds`); absent = prior behavior.
- **Per-run model hint** — callers can now pass an opaque `model_hint` string to
  `Orchestrator.run()` and `Orchestrator.run_stream()` to override the tier used for answer and
  deep steps in that run. The hint is consumer-defined: a tier name, or any string the consumer's
  `ModelRegistry` understands. The registry resolves it exactly as it resolves a planner-chosen
  tier, so unknown values degrade gracefully to the "sonnet" default rather than raising (note: the
  default registry resolves only the tier names, so a raw model id needs a registry that
  understands it). The hint applies to answer and deep steps only; the planner's own cheap calls
  stay on the configured planner tier. Absent/`None` means exactly the prior behavior. `TaskExecutor.execute()`
  automatically surfaces a `"model"` field on the task document as the `model_hint`, so a stored
  per-task model override reaches the orchestrator with no extra code in the consumer's poller.
  Precedence inside one run: `model_hint` > `plan.model_tier` (planner's own choice) >
  `default_tier` (compile-time default per step kind). Seven new offline unit tests cover: hint
  reaches `provider.answer`, absent hint leaves planner tier unchanged, hint on a deep step,
  unknown hint degrades gracefully, executor task-model field is forwarded, explicit `None` is
  treated as absent, and the full poller path.
- **Discovery contract on retrieval adapters** — `RetrievalAdapter` gains four optional discovery
  methods (`list_sources`, `describe_source(name, path=None)`, `list_operations`,
  `describe_operation(name)`) that make a source self-describing, so the planner asks what exists
  instead of needing a static schema blob in its prompt. The pattern is cheap-then-drill-down: a
  LIST call returns names plus one-liners, a DESCRIBE call returns detail for one item. All four
  return `Observation(kind="query")` and never raise; `RetrievalAdapterBase` ships benign
  "nothing to discover" defaults so existing subclasses keep working unchanged. The orchestrator
  dispatches the four read specs via `getattr`, so a structural adapter that predates the methods
  degrades to a "discovery not supported" observation (back-compat), and discovery specs fan out
  in parallel with the rest of the `reads[]` step. Reference implementations: `FilesAdapter`
  enumerates readable files (capped at 500) and returns a markdown heading outline per file;
  `CachedDbAdapter` accepts optional `sources` / `operations` / `describe` metadata at
  construction and falls back to inferring a schema from a sample row. 15 new offline tests in
  `tests/test_discovery.py`.
- **Stop re-sending unchanged transcript + context to the planner on re-plan steps** — within a
  single run the recent `transcript` and the static `context_view` never change between steps, yet
  the loop re-sent BOTH in full to the cheap PLANNER on every re-plan step (the prior wave only
  leaned out `gathered`). New additive `OrchestratorConfig.planner_abbreviate_repeat_context`
  (default `False` → byte-for-byte current behavior): when enabled, step 1 still sees the FULL
  transcript + context_view, and later re-plan steps swap them for a short "(unchanged since step
  1 — already provided)" reference note, so the planner focuses on the NEW `gathered` observations
  it's there to react to. The final ANSWER path is untouched — it always grounds on the full
  transcript + context_view, so answer grounding is never weakened. `_plan` gained a keyword-only
  `step` argument; empty transcript/context are left as-is (no note injected).
- **Resource-aware throttling (graceful pause under system overload)** — a long-running lane can
  now notice host overload and stop taking on NEW work instead of thrashing, resuming automatically
  once resources recover. New stdlib-only `quest_ai_runner/resources.py`: `ResourceLimits` (all
  limits OFF by default; set via `RunnerConfig.resource_limits` or env — `QAR_MAX_MEMORY_PERCENT`,
  `QAR_MIN_FREE_MEMORY_MB` for the remaining-resource form, `QAR_MAX_LOAD_PER_CORE`, plus
  `QAR_RESOURCE_RESUME_MARGIN` hysteresis (default 10%) and `QAR_RESOURCE_CHECK_INTERVAL` re-check
  cadence while paused (default 30s)) and `ResourceGuard` (samples `/proc/meminfo` /
  `os.getloadavg`, optional `psutil` fallback; logs a WARNING naming the tripped limits on entering
  overload and INFO on recovery; an unreadable metric disables its limit, warned once). The pause is
  LOSSLESS by design: the poller gates pickup before discovery (heartbeat still fires so the env
  stays live), re-checks per task mid-batch (deferring before mark/claim so the task re-fires), and
  `run_forever` waits at the guard's shorter re-check cadence while paused so the lane resumes
  promptly. Unclaimed tasks stay queued on the backend; in-flight tasks are never killed. Nothing
  configured → the guard is a no-op (backward compatible).
- **Leaner per-step planner view (older `gathered` compressed)** — the bounded plan→gather→re-plan
  loop re-fed the cheap PLANNER the ENTIRE cumulative `gathered` blob verbatim on every step, which
  grows fast on multi-read runs. The planner now sees a LEANER view: the newest
  `OrchestratorConfig.planner_recent_full` observations in full, and everything older collapsed to a
  one-line summary (source/path + key finding) so the planner still knows the ground it covered and
  won't re-issue the same read. The full `gathered` is unchanged and is still what the final ANSWER
  is synthesized from — only the planner's per-step input is trimmed. Two new config knobs (both
  additive, defaulted): `planner_recent_full` (default 4) and `planner_compress_over` (default 6);
  compression only engages once `gathered` exceeds `planner_compress_over` observations, so short
  runs are byte-for-byte identical to before. New helpers `_render_gathered_for_planner` and
  `_summarize_observation` in `core/orchestrator.py`; `_render_gathered` (full render) is untouched.
- **Task handler stamp + live execution-progress stream** — the runner now records WHO ran a task
  and streams its execution lifecycle to the task, so a Quest task-detail view can show "handled by
  X" and a live progress feed. `QuestClient.claim(task_id, handler=None)` stamps the handler on the
  in-progress PATCH (omitted when None → backward compatible), and the poller resolves the handler
  as the rep slug (basename of the `rep_sync_resolver`'s `skill_dir`, e.g. `"joshua"`/`"subham"`),
  falling back to `RunnerConfig.runner_label` or None. New `QuestClient.report_progress(task_id,
  kind, text=, output=, data=)` POSTs `{kind, ...non-None}` to `/api/assistant-tasks/{id}/progress`
  (kind in started|status|exec|output|done|error) and is best-effort: it never raises, logging a
  warning on failure. `TaskExecutor` emits `started` on pickup, fans each real deep milestone to
  BOTH the originating chat AND the progress stream (kind `exec`), and emits a terminal event from
  the result (done→`done`, needs_you→`done` with a paused note, failed→`error`). All progress posts
  are best-effort and degrade cleanly against an older client without `report_progress`.
- **AI-rep ↔ skill-file sync (`runner.rep_sync`)** — keep a team AI rep's Claude skill file in sync
  with its Quest profile in ONE call: `sync_rep(client, team_id, user_id, skill_dir, direction=...)`
  (and the underlying `pull_rep_to_skill` / `push_skill_to_rep`). Pull renders the rep's `persona`
  and `learned_notes` into the skill file's runner-MANAGED sections (delimited by
  `<!-- QAR:MANAGED:... -->` markers, so any human-authored content is preserved and re-render is
  idempotent); push reads those sections back and PUTs them to the profile. `QuestClient` gained
  `get_ai_profile` / `update_ai_profile` / `add_rep_correction` over
  `GET|PUT /api/teams/{team_id}/members/{user_id}/ai-profile` and
  `POST .../corrections` (reuses the existing urllib client + bearer auth; no new HTTP). The poller
  has an OPT-IN `rep_sync_resolver` on `RunnerConfig`: when set, it pulls the rep's latest profile
  into its skill file right before running that rep's task (best-effort — a sync failure never breaks
  execution). Off by default, so existing lanes are unchanged.
- **Discovery tools on `RetrievalAdapter`** — `list_sources()`, `describe_source(name, path=...)`,
  `list_operations()`, and `describe_operation(name)` let the brain learn what a source of truth
  CONTAINS (collections/doc-sets, their fields) and what OPERATIONS it can call, instead of needing
  a static schema/operation blob pushed into the planner prompt. Each returns
  `Observation(kind="query")`, so results flow into `gathered` through the same path as a read and
  batch in parallel. Two levels each: a cheap LIST (names + one-liners) and a DESCRIBE drill-down.
  Exposed to the planner as four new `reads[]` spec-shapes (`{"list_sources": true}`,
  `{"describe_source": "<name>"}`, `{"list_operations": true}`, `{"describe_operation": "<name>"}`);
  the planner prompt teaches the brain to DISCOVER before guessing, neutrally (no source/operation
  is favored). `RetrievalAdapterBase` ships non-abstract defaults and the orchestrator dispatches
  via `getattr`, so older structural adapters keep working (a discovery spec degrades to a benign
  "not supported" Observation). `FilesAdapter` enumerates readable files + heading outlines;
  `CachedDbAdapter` takes optional `sources`/`operations`/`describe` and otherwise infers fields
  from a sample row.
- **`EVENT_EXEC` + `DeepRunner.run_goal(emit=...)`** — a deep runner can now stream its
  EXECUTION LIFECYCLE (generated code, each execution attempt, its raw output, retries, done/error)
  by emitting `ProgressEvent(type=EVENT_EXEC, data={"phase": ...})`. The orchestrator passes a live
  `emit` callable to runners whose `run_goal` accepts it (decided by signature inspection, so older
  `run_goal(*, goal, brief, model, max_turns)` signatures keep working and are never double-called).
  `EVENT_EXEC` is intermediate texture — NOT in `SURFACING_EVENTS` — so a LIVE run shows it while a
  BACKGROUND (`MilestoneSink`) run drops it, like the other chatter types. This lets an attended
  chat surface live code-execution + retries while a queued task stays quiet.

- **`ClaudeCliProvider`** — a keyless `ModelProvider` that drives the local `claude` CLI headless,
  so the orchestrator's planner/answer calls run on the box's Claude Code **subscription login**
  with no `ANTHROPIC_API_KEY` (the deep-runner already ran keyless this way; now the whole runner
  can). The CLI can't force `tool_choice`, so `plan` instructs the model to emit the `decide`
  tool's JSON and parses it leniently (degrading to a safe `answer` on any failure). `list_models`
  returns `[]` so the `ModelRegistry` uses its fallback tier map; tier ids map to CLI family
  aliases. The spawned process has API-key / session env vars stripped (subscription only).

- **Multi-environment heartbeat** — `RunnerConfig.env_id` (env var `QAR_ENV_ID`) is sent on the
  environment heartbeat so a team can attach SEVERAL runners, each registering as its own
  environment. Omitted = the team's default environment (single-runner deployments are unchanged).

### Changed
- **Deep-step status copy** — the live status emitted when the orchestrator enters the `deep`
  action is now a neutral `"working on this now…"` instead of `"this needs real work — running a
  goal-driven task"`. The old wording read as jargon (and conflated "task" with a consumer's own
  goal objects) when surfaced verbatim in a chat; the neutral phrasing travels better across
  consumers and drops the em dash.
- **CLI** — `QAR_MODEL_BACKEND` (`anthropic` | `claude_cli`) selects the model backend; when unset
  it auto-selects `claude_cli` (keyless) unless `ANTHROPIC_API_KEY` is present. The runner now
  works out of the box on a subscription login with no API key. The CLI also reads
  `QAR_RUNNER_LABEL` and `QAR_ENV_ID` for the heartbeat.

## [0.1.0] - 2026-06-05

Initial public release.

### Added
- **`core`** — the domain-free orchestrator brain: a bounded plan → gather → re-plan →
  answer/deep/confirm loop, with the frozen streaming interface (`Mode`, `ProgressEvent`,
  `StreamSink`, `MilestoneSink`) and the live↔background handoff.
- **`runner`** — the queued-task executor: an event-driven, signature-deduped poller; a claim
  step; an executor that maps results onto the Quest task callback; and a `QuestClient` covering
  discover / claim / report / escalate / loop-close.
- **The four adapter interfaces** — `RetrievalAdapter`, `ModelProvider`, `DeepRunner`,
  `EscalationSink` — plus reference implementations (`FilesAdapter`, `CachedDbAdapter`,
  `AnthropicProvider`, `SubprocessGoalRunner`, `QuestDecisionSink`).
- **`RunnerConfig`** + factory helpers and capability derivation (`web` / `corpus` / `code`).
- **`quest-ai-runner` CLI** — an env-driven console entry point for the poller.
- **Examples** (`examples/`) — a custom consumer, a runnable lane, and a live end-to-end demo.
- **Tests** — offline core-loop, model-registry, mode/streaming, runner (mock client), and
  example wiring tests.

[Unreleased]: https://github.com/SpiritualData/quest-ai-runner/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SpiritualData/quest-ai-runner/releases/tag/v0.1.0
