# Per-component QAR evaluation

Each major component runs and is scored on its own, from one command, with no dependence on the
other. A solution is ONE function behind a fixed signature, so comparing completely different
solutions or frameworks is writing another implementation, never changing the harness.

```
PY=.venv/bin/python3        # from the quest-ai-runner repo root
$PY evaluation/components/run.py classification --impl qar_planner      # selected per solutions.csv
$PY evaluation/components/run.py classification --impl single_prompt    # alternative
$PY evaluation/components/run.py classification --impl keyword_rules    # zero-call floor
$PY evaluation/components/run.py quest_ops --impl qar_orchestrator      # selected per solutions.csv
$PY evaluation/components/run.py quest_ops --impl direct_tool_call      # alternative
$PY evaluation/components/run.py context_selection --impl hybrid_union    # selected per solutions.csv, zero model calls
$PY evaluation/components/run.py context_selection --impl blind_grep           # zero-model floor
$PY evaluation/components/run.py context_selection --impl vector_bge_small     # local embedder, offline, ~90 s
$PY evaluation/components/run.py context_selection --impl hybrid_union         # the LIVE HybridContextAssembler (keyword + vector arms), ~10 s with a warm embed cache
$PY evaluation/components/run.py staleness --impl store_sha256                 # selected per solutions.csv, zero model calls
$PY evaluation/components/run.py staleness --impl mtime_size                   # baseline
$PY evaluation/components/run.py recipe_fast_path --impl recipe_replay         # selected per solutions.csv (real model)
$PY evaluation/components/run.py recipe_fast_path --impl planner_only          # baseline (real model)
$PY evaluation/components/run.py recipe_fast_path --impl recipe_replay_offline # OFFLINE arm: rule model instead of the claude CLI, recorded
$PY evaluation/components/run.py recipe_fast_path --impl planner_only_offline  # offline baseline
$PY evaluation/components/run.py feedback_loop --impl ledger_guidance          # live ledger + guidance provider, scripted model
$PY evaluation/components/run.py feed_context_sources --impl update_engine     # live UpdateEngine quest channels, fake Quest client
$PY evaluation/components/run.py receive_input_mid_run --impl in_memory        # live inbox wired into the live Orchestrator
$PY evaluation/components/run.py esoi_behavior --impl reference_good   # offline validity: must pass all 60
$PY evaluation/components/run.py esoi_behavior --impl reference_bad    # offline validity: must fail all 60
$PY evaluation/components/run.py esoi_behavior --impl standing_instructions --only ESOI-002,ESOI-020 --max-calls 8   # chat arm + model judge, costs cents
$PY evaluation/components/run.py esoi_behavior --impl insitu_standing_instructions --only ESOI-002,ESOI-020           # real deep-run prompt, tools off
$PY evaluation/components/run.py esoi_judge --impl model_judge         # the judge's own agreement with labelled replies (120 cheap calls)
$PY -m pytest evaluation/components/test_components.py                  # offline, no model calls
```
Flags: `--only ID,ID`, `--max-calls N` (aborts when model calls exceed N), `--out path`.
`QAR_EVAL_MODEL` (default `haiku`) sets the model for every implementation, so they compare like for like.
Each run writes `/tmp/qar_components/<component>_<impl>.json` and prints the model-call count. `--record` also appends a row to the committed `results.csv` (with `git_sha`, `cases_sha` and `model`); `leaderboard.py` ranks only rows with the same `cases_sha`. Which implementation is "selected" is whatever `docs/structure/solutions.csv` says, not a label in this file.

## Components and their functions

| Component | Function | Scored by |
| --- | --- | --- |
| `classification/` | `classify(message, context) -> {"route": answer\|tool\|deep, "tool": name}` | route (and tool) match `expect` in `cases.json` |
| `quest_ops/` | `run_operation(message, world) -> {"reply"}` | the in-memory quest's state afterwards, plus no unrequested writes; the opt-in dev arm (`dev_run.py`) scores route and real dev-Quest state on 58 chat cases |
| `context_selection/` | `select(task, corpus) -> [file paths, best first]` | the labelled file's rank: top-1, top-3 (or within the solution's declared `budget`) |
| `staleness/` | `changed(before, after) -> {stale paths}` | exact set of edited or deleted files; precision and recall over all cases |
| `recipe_fast_path/` | `handle(message, world) -> {"reply", "exit_reason"}` | the fake tool world's recorded calls: right tool and args on a repeat, no lookalike write on a near miss, no tool on chat |
| `chat_quality/` | `converse(case) -> record` (the metric of `answer-a-turn`) | the dev Quest chat's judged pass flag per case (opt-in, SKIPs offline): routing, grounding on pivot facts, database side effects, rubric; the offline arm `reference_replies` checks that the schema validates and the deterministic pre-checks accept a reference reply and reject an empty one |

| `feedback_loop/` | `replay(steps) -> {"turns": [guidance shown per turn]}` for a synthetic correction sequence (serves `store-feedback` and `guide-by-feedback`, many-to-one) | each relevant turn shows its stored correction (repeat-correction rate), irrelevant ones stay out; floor `no_memory`, baseline `dump_all` |
| `feed_context_sources/` | `feed(kind, world) -> {"offered", "needs_response"}` for the quest channels (notes, goal updates, decisions) | declarative `expect`; broken baseline `dump_all` in `broken/` |
| `receive_input_mid_run/` | `receive(kind, args) -> dict` for mid-run messages through the inbox and the live Orchestrator, plus the inbox port contract | message reaches the next decision once, to the right conversation, never lost; broken baselines `no_inbox` and `shared_list` in `broken/` |
| `judges/` | `judge(kind, args) -> dict` for overseer parsing and digest, reach verdict and block, quest ranking, sufficiency gate | declarative `expect` per case (`expect.py`); per-kind pass counts |
| `overseer_signals/` | `signal(digest) -> {"signal", "reason", "hint"}` on 16 labelled scenarios | digest shows the label's evidence (offline); signal in `want` (live) |
| `deep_run/` | `contract(kind, args) -> dict` for the deep-run rules (review parse and fail-open, markers, envelopes, headless prompt, met-vs-limit, the goal loop) | declarative `expect`; zero worker, zero model |
| `deep_review/` | `review(elapsed, idle, tail) -> {"stop", "reason"}` on labelled live logs (the metric of `detect-a-stalled-run`) | `stop` equals the label; false stops counted apart from missed stops |
| `reply_guards/` | `guard(kind, args) -> dict` for the link guard, link extraction, recipe match and learn, anticipation chips, match and weight | declarative `expect`; fake HTTP probe and fixed clock |
| `model_providers/` | `call(kind, args) -> dict` for prefix routing, quota tier fallback, retry, dry-run tokens, price lookups, estimate arithmetic | declarative `expect`; fake and dry-run providers |
| `web_search/` | `search(case) -> {"urls", "text", "error", ...}` through one backend over fixture payloads (the offline invariant of search-the-web) | gold page in top k, cap, dedupe, tags, summary, failover, cache; tokens added |
| `web_search_live/` | the same result dict from the PRODUCTION `WebResearchAdapter` built from the environment, through `scripts/web_search_bench.py`, cache bypassed (opt-in: `QAR_EVAL_LIVE_SEARCH=1` and a configured backend, else SKIPPED) | an answer phrase in the observation the planner would see, no error, unique urls; spend counted in real backend calls at the list price in `pricing.py` |
| `card_quality/` | `select(query, world) -> {"view", "titles"}` after learning three tasks (the outcome arm of select-context: choosing cards for a query) | labelled collection resolved, no spraying, no leak |
| `persist_cards/` | `persist(kind, args) -> dict` through a `CardRepository`: roundtrip, overwrite, loss at volume and across pages, duplication, delete, concurrent writers, restart, change stamp, tolerance; one case set run on the filesystem, embedded Qdrant and Quest API backends (fake Quest card API) | declarative `expect`: every case must hold on every backend (no lost or duplicated card, same behaviour) |
| `claude_code_ab/` | `locate(case) -> {"answer", "turns"}` for seven "which file" tasks (the second outcome arm of `select-context`) | labelled path in the answer |
| `retrieval/` | `retrieve(question, corpus) -> {"ids", "texts"}` through a complete retrieval STACK (the composite and its floor) over a synthetic handbook | recall@3 of the labelled document, and its answer phrase in the text the adapter read |
| `retrieval_sources/` | the same function through ONE source adapter alone (files, DB rows, MCP, Notion, Drive, Chat, remote files); same cases (a link to `retrieval/cases.json`), policy and scorer | the same bar per source; an invariant arm (`invariant.txt`): listed, never ranked |
| `conversation_recall/` | `recall(query, user, world) -> {"ids"}` through the live `SessionFileConversationStore` | labelled past conversation returned, none of another user's |
| `recent_context/` | `recall(case) -> {"ids"}` through the live `FileRecentContextStore` and `filter_relevant` | right cards warm-started, no stale or cross-quest card |
| `vector_recall/` | `search(query, corpus) -> {"ids"}` through the live embedded `QdrantVectorStore` | labelled card in the top 3 (mostly paraphrases), and p50/p95 query milliseconds at a fixed corpus size against the caps in the case data |

Add a solution: create `<component>/impls/<name>.py` exposing `build(meter) -> function`, count any
model call with `meter.record()` (or `meter.wrap(provider)`), and run it with `--impl <name>`.
`module:build` also works for a solution that lives outside this repo.
A component's score module may also define `aggregate(rows) -> str`; `run.py` prints it as the headline
(top-1 / top-3, precision / recall, correct per group). A solution that legitimately returns more than
the usual top 3 (the hybrid union) declares it with `select.budget = N`, and the scorer reports both.

Notes on the three newer components:

* `context_selection/` is zero-model. The corpus (`corpus.py`) is the repo checkout itself, read-only
  (`QAR_EVAL_CORPUS_ROOT` points it elsewhere), with one deterministic card per `.py` file built by the
  store's own extractors, because the production bootstrap needs a model. `vector_bge_small` (a baseline) and
  `hybrid_union` (which wraps the live `HybridContextAssembler`, `FileContextStore` and `VectorContextAssembler`, with only the embedder and an in-memory cosine store local) need the `BAAI/bge-small-en-v1.5` model in the fastembed cache (`/tmp/fastembed_cache`)
  and run with `HF_HUB_OFFLINE=1`; if it is missing they fail loudly instead of scoring 0%.
* `staleness/` is zero-model and builds its own tiny trees in `/tmp`. One of its cases is a known gap of
  the current store, kept on purpose: an edit that restores the mtime is missed (`_current_sha` trusts a
  matching mtime for speed); a deleted file is reported stale.
* `recipe_fast_path/` spends real model calls (`QAR_EVAL_MODEL`). One solution instance sees the whole
  run in order, so the recipe store learns between cases; the scorer sends each tool's TEACH message once
  before its first ask. `--only` selects a subset (the old 1-case smoke is `--only RH1`; the 10-case
  subset is `--only RH1,RH2,RN1,RN2,RT1,RS1,RG1,NM1,NM3,U1`; all 30 is no `--only`). It was ported without a live model run in the session that wrote it
  (no `claude` CLI there): the world, scoring and wiring are covered offline, the model path is not.

## Offline arms, live arms, and what each number means

These nine components were added together (results in `results.csv`). Each has an OFFLINE arm that is recorded and a
LIVE arm that is built but not recorded, because a live run spends model tokens and spend needs approval. The
`claude` binary is not on PATH on this host but it does exist at `~/.local/bin/claude`, which the claude_cli
provider finds on its own, so a live run is possible once someone approves the (cents-level) spend; run it with
`--record`. One unrecorded `card_quality` live run was made by mistake while testing the port (see below).

| Component | Recorded (offline) | Not recorded (live) | Honest limits |
| --- | --- | --- | --- |
| `judges` | `qar_judges`: the deterministic halves of the four judges with a scripted provider (a trusting `trust_raw` is in `broken/`) | what the models decide: `overseer_signals --impl live_model` | JUD-029 pins that a tight digest budget sheds history operations first and prior escalations last |
| `overseer_signals` | `digest_probe` 16/16: the digest shows the draft, the refused proposal, the corrections and the operations each label turns on | `live_model` (16 small calls): signal in `want` | the offline arm cannot say whether the judge is RIGHT, only that it can see the evidence |
| `deep_run` | `goal_contract` (a trusting `naive` is in `broken/`): met-vs-limit, goal-loop stop rules (exhausted, deferred, decision, cap), escalation and resume markers, envelopes, headless prompt | none needed (no model in these rules) | the real Orchestrator loop runs with a scripted runner and verifier; no worker is started |
| `deep_review` | `rule_floor` 38/39 | `live_reviewer` (39 one-shot `claude -p` calls) | the rule floor was written with the cases in view: an optimistic ceiling for a rule, not a blind result |
| `reply_guards` | `qar_guards` (a `passthrough` with no guard is in `broken/`): fabricated vs real links, extraction, recipe near misses and scope fence, chips and weights | the replay engine through a model is `recipe_fast_path` (unchanged, waived) | the HTTP probe and clock are faked |
| `model_providers` | `multi_provider` vs `single_provider`: routing, tier fallback, retry, cost arithmetic | latency and pass rate per tier (needs real calls) | the price table is input tokens only; the opus spellings (MPR-080, MPR-101) now price at the family rate |
| `web_search` | every backend and the chain on the 15 fixture queries | live relevance and spend: `web_search_live` (opt-in, real calls) | payloads are WRITTEN fixtures in each vendor's response shape, not recordings, so this proves identical handling, not relevance; the operator tool `scripts/web_search_bench.py` is reused for measurement |
| `card_quality` | `seeded_keyword_store`, `seeded_keyword_open` | `live_updater_hybrid` (model-learned cards, Voyage vector arm, embedded Qdrant) | the offline arm measures selection and fresh resolution, not the model's learning of cards; an earlier live run was made by mistake and is NOT in `results.csv` |
| `claude_code_ab` | `idf_hint` 5/7: what the warm hint would contain | `cold_claude`, `warm_claude` | the old script's warm hint was always empty (no model provider means zero cards) so it fell back to the correct answer; the component takes its hint from `idf_hint` instead |

`expect.py` is the shared declarative check (`eq`, `contains`, `not_contains`, `max_len`, `min_len`, `approx`) used by
the case-driven components, so a new case is data only. Offline tests: `test_core_adapter_components.py`
(it pins each component's known-gap case ids, so a fix shows up as a test to update).

There are no standalone eval scripts left in `evaluation/`: every measurement is a component here (the old
`overseer_signals_eval.py`, `deep_review_eval.py`, `card_quality_eval.py`, `eval_deterministic.py`,
`recipe_fast_path_eval.py` and `eval_vs_claude_code.py` were folded in; the last is now `claude_code_ab/live_support.py`).
The old `chat_quest_ops_routing_eval.py` is gone: its 50 cases (plus its 8 recipe cases) are `quest_ops/dev_cases.json`, run by
the dev arm below.

### `quest_ops` dev arm (opt-in, needs a dev Quest backend)

The offline arms score the in-memory `FakeQuest`. The dev arm sends each message through the real in-app chat
(`POST /api/quest-ai/conversations/{id}/messages/stream`, the surface a subscriber uses, which runs the
production brain) against a disposable fixture quest on the dev backend, and scores two things: ROUTING (a plain
quest operation must not leave the process; a case that needs a machine or files must be delegated) and
EXECUTION (the case's `verify` data, checked against what the app really shows: Today's Actions, the timer
route, entries, goals, notes, the decision ledger; never the reply alone). It is skipped, exit 0, unless
`QAR_EVAL_DEV_ENV_FILE` names the dev lane's env file (with `QAR_EVAL_DEV_HOST`); nothing runs in the default
offline test or run.

```
PY=.venv/bin/python3
$PY evaluation/components/quest_ops/dev_run.py --selftest                 # verifier controls, no chat
$PY evaluation/components/quest_ops/dev_run.py --sample 12 --seed 1       # stratified draw
$PY evaluation/components/quest_ops/dev_run.py --only R1,H1,G3,X1
$PY evaluation/components/quest_ops/dev_run.py --stratum write --auto-run off   # approval-card arm
$PY evaluation/components/quest_ops/dev_run.py --recipes-subset 10 --recipes on # recipe store on
```

* `dev_cases.json`: 58 cases. Each has `id`, `stratum` (`read` 12, `list` 6, `write` 23, `inform` 4, `contrast` 5, `recipe` 8; draw a
  sample by stratum), `area`, `message`, `expect_route` (`inline` or `deep`), a declarative `verify` (`kind` in
  `dev_verify.py`), optional `prelude` turns and a `before` precondition (SC1 adds a goal between two listings;
  T2 starts the timer it will stop; RN2 counts notes), and for writes a `control`: REST steps that make the
  verifier true, used by `--selftest` (it must be False on the untouched fixture and True after the control).
  The 50 chat cases keep the old ids (R1..X5); `RP1..RP6`, `RN1`, `RN2` are the recipe cases, and
  `dev_recipe_subsets.json` holds the old 1, 10 and 30 teach-then-score sets.
* `dev_world.py` (fixture and cleanup, verified gone), `dev_arm.py` (the arm: same `run_operation(message, world)`
  signature, route classification), `dev_verify.py` (verifiers), `dev_run.py` (scoring, sampling, CLI). The
  REST and SSE plumbing is `chat_quality/devclient.py`, not a copy.
* Quest creation is gated for API keys: set `QAR_EVAL_QUEST_ID` (one approved quest, one run) or
  `QAR_EVAL_QUEST_FACTORY` (a command printing a new quest id; `QAR_EVAL_QUEST_FACTORY_CWD`,
  `QAR_EVAL_QUEST_DELETE` with `{quest_id}`), and optionally `QAR_EVAL_CATEGORY_ID`.
* Spend: a pass is real model tokens on dev, so draw a sample and run it once (single passes are noisy; a case
  that flips once is a signal, not proof). One thing the port changed: a case now passes only when routing AND
  execution both pass (the old script printed them as two columns, still in the row).
* Not carried over: the old script's QAR-library arm (`run`, which drove the library Orchestrator with the deep
  runner off) is `quest_ops --impl qar_orchestrator`; its markdown summary writer and `--repeat` passes (loop the command).

Last measured, 2026-10-06, on dev (the old script, one pass each, a paired 24-case subset): correct 7/24 before the
fixes, 16/24 after one pass and 20/24 after the last fixes and verifier corrections; routing 23/24 then 24/24;
writes 1/12 then 11/12; median turn 16.1 s then 13.6 s (max 497 s then 34 s). The baseline over all 50 was routing
47/50 with only 7/23 writes landing. The lessons that stayed true: judge a write by what the app shows (a habit
"done" must appear in Today's Actions), not the reply; scope habit and collection lookups to the quest; delete the
fixture's conversations (left behind they become context for later runs); and a field write on an autopilot-off
quest legitimately parks on an approval, so it passes when the decision proposes the value and the reply does
not claim it happened. Still open then: one flip of D3 (a reflection with "weekend" handed off as a task),
account-wide rep learned notes colouring other quests' replies, and a raw insert not being safe to retry after a
partial write. Fixes are in quest-backend history.

## Why it cannot start a deep run

* `common.build_offline_orchestrator` sets `cfg.deep_runner = None` (the deliberate "execution
  disabled" value) and refuses to return if a deep runner is wired.
* The Quest URL is `http://quest.invalid`, which never resolves; quest operations run against an
  in-memory `FakeQuest` that sits behind the REAL standard tool catalog (same names, descriptions and
  schemas), with every handler replaced.
* Classification calls only the planning step (`Orchestrator._plan`); nothing it decides is executed.
* Cases that need an external environment or a deep run are not in these sets: the classification set
  only checks that such messages are *routed* to deep.

## Cost

Scoring is deterministic (no judge), so a run costs exactly the implementation's own model calls,
which the harness counts. Cases 23-30 of classification are held out (the keyword baseline was
written seeing 1-22 only), so a baseline gap between the two groups shows over-fitting.
`chat_quality/` is the end-to-end chat metric (formerly `evaluation/qualitative/`) and the metric of the solution `answer-a-turn`:
`converse(case) -> record`, scored from the record's pass flag. It needs a built dev Quest world, so its graded implementation
(`impls/dev_quest_chat.py`) SKIPs offline with exit 0; the full datasets, world lifecycle and judge are driven by
`chat_quality/runner.py` (see its README). The offline arm `impls/reference_replies.py` is recorded over every case in the datasets: it shows the metric is sound.
The graded arm is drawn with `--sample N` and counts its spend when run in process (see `chat_quality/README.md`).

Every implementation measures live code or says it does not (check S12 in `docs/structure/README.md`): it
imports or drives `quest_ai_runner`, or declares `ALTERNATIVE = True` (or a docstring opening `ALTERNATIVE`,
`BASELINE`, `ABLATION`, `FLOOR`, `VALIDITY`). The leaderboard tags these arms and never offers an `ABLATION` or a `VALIDITY` arm as the thing to adopt. A row recorded without `cases_sha` shows as `unverified` in `leaderboard.py`
and is never compared against a verified one (check S16).

## prompt_size: budgets derived from the model limit and cost

`prompt_size/` has `healthy` and `incident` fixtures plus a `production` fixture (PSZ-009 to PSZ-011) that sweeps 20
deterministic samples from a light day to the heaviest seen, so the headline reports a real p95 beside the max. The
sizes (20 context-update refs of about 1.9K chars, standing instructions about 6.8K, goal frame, last run, next steps)
come from sizes measured in the 2026-10-06 incident; the text is generated filler, no real content.

A case passes when its prompt is within the assembly point's BUDGET and holds no duplicated block. The budget is
computed, not typed in beside the number it must beat (an earlier ceiling was raised after a measurement rather
than derived, so it could not show better or worse). `prompt_size/budgets.py` derives it:

```
window_tokens = context window of the CHEAPEST tier in the model registry (prompt_budget.context_window_tokens)
window_bound  = window_tokens * window_share              (deep prompt: window_tokens - WORKER_RESERVED_TOKENS)
cost_bound    = usd_cap_per_run / calls_per_run / price_per_token of the point's own tier
budget_tokens = min(window_bound, cost_bound)
budget_chars  = budget_tokens * CHARS_PER_TOKEN
```

The cheapest tier is the window because a prompt must still fit after a tier downgrade (`TIER_DOWNGRADE_ORDER`).
The planner and the deep prompt are derived this way; the autopilot batch and the context-update bundle are sections
of a deep prompt and take a stated share of its budget. Every constant is read from the code that owns it: the window
and `CHARS_PER_TOKEN` from `core/prompt_budget.py`, the tier models from `core/model_registry.py`, input prices from
`pricing.py`, and `calls_per_run` from `OrchestratorConfig` (`max_steps` for the planner, `deep_goal_max_iterations`
for a goal). Only the shares and the dollar caps are policy; they are in `prompt_size/budget_inputs.json`, each with
the reason for it, and a human changes a budget by changing one of them or the model data, never by raising a limit
to clear a measurement. Each row also reports `headroom`, the share of the budget left unused, and the headline shows
the smallest headroom per point, so a prompt creeping toward its budget is visible before it fails. The budgets are
the assembler's hard caps, not forecasts.

## Retrieval, conversation, recent-context and vector recall (offline, zero model calls)

* `retrieval/` exposes ONE invented 17-document handbook (`corpus.json`) through each source type and asks 21
  fixed questions (`cases.json`, each pinned to the corpus by `corpus_sha`). Every implementation drives the real
  adapter class and fakes only the network edge: `urlopen` for Notion, Drive and Chat, the `open_mcp_session`
  seam for MCP, the transport for `RemoteFilesAdapter` (answered by the real `serve-files` `FileOperations`
  dispatch). The gather policy (`policy.py`: question terms, one adapter search per term, rarity-weighted rank)
  is shared, so a score differs only by what the adapter can find and read. Implementations: `files_adapter`,
  `composite` (selected) and the `first_three` floor are the complete STACKS ranked here. The per-source adapters
  (`files_adapter`, `cached_db`, `mcp_server`, `notion_pages`, `drive_files`, `chat_threads`, `remote_files`) are
  complementary sources, not alternatives, so they run in the separate `retrieval_sources/` arm (same cases, policy
  and scorer; `invariant.txt` makes the leaderboard list them without ranking). Not driven: `quest_retrieval_adapter`, which is keyed by goal and
  quest ids against the Quest API rather than by a question (pinned by `tests/test_quest_retrieval_decisions.py`).
  Honest limits: the MCP number measures plumbing (the fake server does the search); Drive has no search, so its
  policy lists and reads every file; Notion's `grep` sees rows (title, properties), not page bodies; a paraphrase
  with no shared word is a miss for all (RET-019, RET-020). `remote_files` takes about 35 s because the real file
  server runs each search in a child process.
* `vector_recall/` also times queries. A latency case pads the handbook with deterministic filler cards to a fixed
  size, indexes it once (timed apart as `index_ms`), then times every labelled query several times with the model
  embedding the query for real (no text cache). It passes when p50 and p95 are within the caps in the case, which are
  an interactive budget with their basis written in `cap_basis`, not a fitted number. `results.csv` has no latency
  column: the p50/p95 headline is printed by the run and kept in its json; the recorded pass count includes the case.
* `conversation_recall/` covers `remember-conversations`: synthetic session files for two users, `related_slices`
  with the user in scope, and a leak check. `current_slice` and database-backed stores are not driven.
* `recent_context/` covers `recall-recent-context`: ten multi-turn scenarios (follow-up free pass, scope fence
  across quests, unrelated question, lexical reuse). A paraphrase gap is pinned (RCT-010).
* `vector_recall/` covers `index-by-meaning`: the live embedded Qdrant store with the local bge-small model
  (fastembed cache, offline) versus an in-memory cosine baseline, on the same handbook cards. It is its own
  component rather than extra `context_selection` cases because those labels are repo files and rank whole
  pipelines, which would put one arm against the full hybrid under check S10.

## Infrastructure solutions (structure, public surface, examples, tests, docs, hygiene)

Six components score the repo's own plumbing, all offline with zero model calls. Each has a declared baseline.

| Component | Function | Scored by | Broken baseline (`broken/`) |
| --- | --- | --- | --- |
| `check_the_structure/` | `check(kind, args) -> {"exit", "problems"}` | mutation detection: one known violation injected per case into a synthetic fixture repo (and a consumer registry), rule and path must be reported; clean fixtures must not be flagged | `coverage_only` (file coverage alone) |
| `expose_the_public_api/` | `surface(kind, args) -> dict` | documented names import, registrations take effect, front doors and helpers behave, vendored code imports with sockets refusing | `import_smoke` |
| `learn_by_example/` | `run_example(kind, args) -> dict` | each example compiles, imports, runs its offline path in a scrubbed environment, is in the README, holds no secret | `compile_only` |
| `verify_offline/` | `verify(kind, args) -> dict` | `pytest --collect-only` over every test file in 21 shards (zero errors, count floor), seeded broken test folders, size ceiling | `syntax_only` |
| `explain_qar/` | `explain(kind, args) -> dict` | cases generated from the repo by `generate_cases.py` (links, em dashes, index, env variables, subcommands, public names) | `readme_only` |
| `keep_secrets_out/` | `scan(kind, args) -> dict` | planted secrets found, placeholders quiet, the repo clean, the command's exit code | `grep_oneliner` |

These are `invariant` solutions (see below): a rule holds or it does not, so the baseline is not a rival on a leaderboard.

Why `verify_offline` does not run the tests: a full run is slow, parts of the suite reach real model providers, and
one run was killed mid-flight for spend (CLAUDE.md, 2026-09-19). Collection proves every file imports and
parametrises and that no test vanished; whether a test passes is shown by running the files a change touches.

## Invariant solutions and `broken/`

A solution whose `metric_kind` in `docs/structure/solutions.csv` is `invariant` is a rule that holds or does not (a write stays
inside its root, a message is delivered once, a name resolves to one class). Its case suite and recorded run stay, because
they pin the behaviour, but "best" cannot vary, so `leaderboard.py` lists it apart under "Invariant solutions" and never
ranks it, and the tracker labels it `invariant`, not `decided-by-metric`. The floor a first draft would write (naive open
and write, passthrough, flat rate) is not an `impls/` arm: it lives in `<component>/broken/<name>.py`, and
`test_components.py::test_a_deliberately_broken_implementation_fails_the_cases` proves each one fails some of the cases,
which is the only job such a floor has (to show the cases discriminate). Only a floor someone would plausibly ship
(`dump_all`, `most_recent`, `keyword_rules`) remains an `impls/` arm of an `eval` solution.

Every folder here is named by a solution (check S21): the metric of a solution is its `metric_command`; an arm that another
solution reads is declared in that solution's `metric` text (for example `retrieval_sources` under `gather-evidence`,
`claude_code_ab` under `select-context`).

## `esoi_behavior`: the judge, the two settings, and what the numbers can and cannot say

`esoi_behavior` scores whether an AI's reply follows the evolvable-structure (ESOI) principle a case tests
(60 cases, P1 to P13, traps where adding structure is wrong, a quarter held out). Four live arms, all
through the keyless claude CLI at the cheapest tier (`QAR_EVAL_MODEL`, default haiku):

| Arm | Setting | Instructions |
| --- | --- | --- |
| `standing_instructions` / `plain` | chat: the scenario as the user message, a bare completion | the shipped `prompts/evolvable_structure.md` as system text / "You are a helpful assistant." |
| `insitu_standing_instructions` / `insitu_plain` | the deep-run path: the scenario as the TASK of QAR's own `compose_goal_prompt`, run by headless `claude -p` with `--tools ""` (nothing can read, edit or run) | the standing-instructions layer as the deep run composes it / the layer emptied |

**Scoring.** A reply that matches a case's `must_not` regex fails with no model call (the regex is a cheap
prefilter only). Every other reply is graded by the model judge (`esoi_behavior/judge.py`): its rubric is the
case's `expected_behavior` and `failure_mode`, it never sees the regexes or the reference replies, and it
returns pass or fail with a one-line reason. `must_have` is not a gate for a judged arm; the run prints how often
the regex and the judge disagree. The offline arms `reference_good` and `reference_bad` stay regex-only and
free. A judged arm is any implementation whose function carries a `judge` attribute.

**The judge has its own metric** (`esoi_judge`, 120 cheap calls): it grades each case's `good_reference` and
`bad_reference` and is scored on agreement with the label (good passes, bad fails). Those labels come from the
case author, who also wrote the regexes, so a high agreement shows the judge is not broken and can tell an obvious
good reply from an obvious bad one; it does not show the judge matches Joshua's reading.
**Calibration against Joshua's verdicts is pending his review of `cases.csv` in the sheet:** `reviewer_verdict` is
empty on all 60 cases and nothing here invents one. When he fills it in, add his verdicts as labels and re-run.

**In-situ limits.** The deep worker normally has tools and real files; here it has neither, so the reply is the
stated plan and edits, not a diff, and the judge is told that a stated edit counts as doing it. The lane's org
preamble (context cards, persona) and the goal loop are not reproduced. This is the nearest tool-free
reproduction of the shipped path, not a measurement of real deep runs on real tasks; scoring real diffs from
actual tasks is still open.

**Reading the numbers.** The judge and the replies are the same small model, which may favour its own phrasing,
and one judge wording change moved arm totals by up to four cases of thirty, so differences of a few cases are
noise. Run the two arms of a setting on the same ids, compare paired (cases only one arm passes), and call a
winner only on a clear margin. Recorded rows in `results.csv` are stratified 30-case samples with `--only`.
