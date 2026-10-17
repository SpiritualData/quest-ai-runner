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
$PY evaluation/components/run.py esoi_behavior --impl reference_good   # offline validity: must pass all 42
$PY evaluation/components/run.py esoi_behavior --impl reference_bad    # offline validity: must fail all 42
$PY evaluation/components/run.py esoi_behavior --impl standing_instructions --max-calls 42   # live, costs model calls
$PY -m pytest evaluation/components/test_components.py                  # offline, no model calls
```
Flags: `--only ID,ID`, `--max-calls N` (aborts when model calls exceed N), `--out path`.
`QAR_EVAL_MODEL` (default `haiku`) sets the model for every implementation, so they compare like for like.
Each run writes `/tmp/qar_components/<component>_<impl>.json` and prints the model-call count. `--record` also appends a row to the committed `results.csv` (with `git_sha`, `cases_sha` and `model`); `leaderboard.py` ranks only rows with the same `cases_sha`. Which implementation is "selected" is whatever `docs/structure/solutions.csv` says, not a label in this file.

## Components and their functions

| Component | Function | Scored by |
| --- | --- | --- |
| `classification/` | `classify(message, context) -> {"route": answer\|tool\|deep, "tool": name}` | route (and tool) match `expect` in `cases.json` |
| `quest_ops/` | `run_operation(message, world) -> {"reply"}` | the in-memory quest's state afterwards, plus no unrequested writes |
| `context_selection/` | `select(task, corpus) -> [file paths, best first]` | the labelled file's rank: top-1, top-3 (or within the solution's declared `budget`) |
| `staleness/` | `changed(before, after) -> {stale paths}` | exact set of edited or deleted files; precision and recall over all cases |
| `recipe_fast_path/` | `handle(message, world) -> {"reply", "exit_reason"}` | the fake tool world's recorded calls: right tool and args on a repeat, no lookalike write on a near miss, no tool on chat |

| `judges/` | `judge(kind, args) -> dict` for overseer parsing and digest, reach verdict and block, quest ranking, sufficiency gate | declarative `expect` per case (`expect.py`); per-kind pass counts |
| `overseer_signals/` | `signal(digest) -> {"signal", "reason", "hint"}` on 16 labelled scenarios | digest shows the label's evidence (offline); signal in `want` (live) |
| `deep_run/` | `contract(kind, args) -> dict` for the deep-run rules (review parse and fail-open, markers, envelopes, headless prompt, met-vs-limit, the goal loop) | declarative `expect`; zero worker, zero model |
| `deep_review/` | `review(elapsed, idle, tail) -> {"stop", "reason"}` on 39 labelled live logs | `stop` equals the label; false stops counted apart from missed stops |
| `reply_guards/` | `guard(kind, args) -> dict` for the link guard, link extraction, recipe match and learn, anticipation chips, match and weight | declarative `expect`; fake HTTP probe and fixed clock |
| `model_providers/` | `call(kind, args) -> dict` for prefix routing, quota tier fallback, retry, dry-run tokens, price lookups, estimate arithmetic | declarative `expect`; fake and dry-run providers |
| `web_search/` | `search(case) -> {"urls", "text", "error", ...}` through one backend over fixture payloads | gold page in top k, cap, dedupe, tags, summary, failover, cache; tokens added |
| `card_quality/` | `select(query, world) -> {"view", "titles"}` after learning three tasks | labelled collection resolved, no spraying, no leak |
| `claude_code_ab/` | `locate(case) -> {"answer", "turns"}` for seven "which file" tasks | labelled path in the answer |

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
* `staleness/` is zero-model and builds its own tiny trees in `/tmp`. Two of its cases are known gaps of
  the current store, kept on purpose: an edit that restores the mtime is missed (`_current_sha` trusts a
  matching mtime for speed), and a deleted file is not reported stale.
* `recipe_fast_path/` spends real model calls (`QAR_EVAL_MODEL`). One solution instance sees the whole
  run in order, so the recipe store learns between cases; the scorer sends each tool's TEACH message once
  before its first ask. `--only` selects a subset (the old 1 / 10 / 30 subsets are in
  `../recipe_fast_path_eval.py`). It was ported without a live model run in the session that wrote it
  (no `claude` CLI there): the world, scoring and wiring are covered offline, the model path is not.

## Offline arms, live arms, and what each number means

These nine components were added together (results in `results.csv`). Each has an OFFLINE arm that is recorded and a
LIVE arm that is built but not recorded, because a live run spends model tokens and spend needs approval. The
`claude` binary is not on PATH on this host but it does exist at `~/.local/bin/claude`, which the claude_cli
provider finds on its own, so a live run is possible once someone approves the (cents-level) spend; run it with
`--record`. One unrecorded `card_quality` live run was made by mistake while testing the port (see below).

| Component | Recorded (offline) | Not recorded (live) | Honest limits |
| --- | --- | --- | --- |
| `judges` | `qar_judges` 60/61 vs `trust_raw` 15/61: the deterministic halves of the four judges with a scripted provider | what the models decide: `overseer_signals --impl live_model` | JUD-029 is a real gap kept on purpose: a 900-char digest budget sheds PRIOR ESCALATIONS before RECENT CONVERSATION |
| `overseer_signals` | `digest_probe` 16/16: the digest shows the draft, the refused proposal, the corrections and the operations each label turns on | `live_model` (16 small calls): signal in `want` | the offline arm cannot say whether the judge is RIGHT, only that it can see the evidence |
| `deep_run` | `goal_contract` 52/52 vs `naive` 14/52: met-vs-limit, goal-loop stop rules (exhausted, deferred, decision, cap), escalation and resume markers, envelopes, headless prompt | none needed (no model in these rules) | the real Orchestrator loop runs with a scripted runner and verifier; no worker is started |
| `deep_review` | `rule_floor` 38/39 | `live_reviewer` (39 one-shot `claude -p` calls) | the rule floor was written with the cases in view: an optimistic ceiling for a rule, not a blind result |
| `reply_guards` | `qar_guards` 54/54 vs `passthrough` 18/54: fabricated vs real links, extraction, recipe near misses and scope fence, chips and weights | the replay engine through a model is `recipe_fast_path` (unchanged, waived) | the HTTP probe and clock are faked |
| `model_providers` | `multi_provider` 54/56 vs `single_provider` 19/56 | latency and pass rate per tier (needs real calls) | two real gaps pinned: `pricing.py` prices `claude-opus-4`/`-4-8` at $3 but the `opus` family at $15 (MPR-080, MPR-101); the table is input tokens only |
| `web_search` | all five backends 15/15; mean observation about 147 to 162 tokens | live relevance and freshness (vendor keys) | payloads are WRITTEN fixtures in each vendor's response shape, not recordings; the operator tool `scripts/web_search_bench.py` is reused for measurement |
| `card_quality` | `seeded_keyword_store` 2/5, `seeded_keyword_open` 1/5 | `live_updater_hybrid` (model-learned cards, Voyage vector arm, embedded Qdrant) | the offline arm measures selection and fresh resolution, not the model's learning of cards; a mistaken live run on 2026-10-15 scored 5/5 (two runs, about $0.08 CLI-reported) but is NOT in `results.csv` |
| `claude_code_ab` | `idf_hint` 5/7: what the warm hint would contain | `cold_claude`, `warm_claude` | the old script's warm hint was always empty (no model provider means zero cards) so it fell back to the correct answer; the component takes its hint from `idf_hint` instead |

`expect.py` is the shared declarative check (`eq`, `contains`, `not_contains`, `max_len`, `min_len`, `approx`) used by
the case-driven components, so a new case is data only. Offline tests: `test_core_adapter_components.py`
(it pins each component's known-gap case ids, so a fix shows up as a test to update).

Legacy scripts: `overseer_signals_eval.py`, `deep_review_eval.py` and `card_quality_eval.py` are thin wrappers over the
live arms. `eval_vs_claude_code.py` stays as the live engine `claude_code_ab` imports.
`chat_quest_ops_routing_eval.py` is not ported: its 50 cases and in-app arm need the dev Quest backend, not only a model.

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
The old end-to-end harness (`evaluation/qualitative/`, needs a dev Quest world) stays for system-level checks.

Every implementation measures live code or says it does not (check S12 in `docs/structure/README.md`): it
imports or drives `quest_ai_runner`, or declares `ALTERNATIVE = True` (or a docstring opening `ALTERNATIVE`,
`BASELINE`, `ABLATION`, `FLOOR`). A row recorded without `cases_sha` shows as `unverified` in `leaderboard.py`
and is never compared against a verified one (check S16).
