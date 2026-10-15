# Per-component QAR evaluation

Each major component runs and is scored on its own, from one command, with no dependence on the
other. A solution is ONE function behind a fixed signature, so comparing completely different
solutions or frameworks is writing another implementation, never changing the harness.

```
PY=.venv/bin/python3        # from the quest-ai-runner repo root
$PY evaluation/components/run.py classification --impl qar_planner      # current
$PY evaluation/components/run.py classification --impl single_prompt    # alternative
$PY evaluation/components/run.py classification --impl keyword_rules    # zero-call floor
$PY evaluation/components/run.py quest_ops --impl qar_orchestrator      # current
$PY evaluation/components/run.py quest_ops --impl direct_tool_call      # alternative
$PY evaluation/components/run.py context_selection --impl hybrid_union    # selected (see docs/structure/solutions.csv), zero model calls
$PY evaluation/components/run.py context_selection --impl blind_grep           # zero-model floor
$PY evaluation/components/run.py context_selection --impl vector_bge_small     # local embedder, offline, ~90 s
$PY evaluation/components/run.py context_selection --impl hybrid_union         # IDF + vector union, ~90 s
$PY evaluation/components/run.py staleness --impl store_sha256                 # current, zero model calls
$PY evaluation/components/run.py staleness --impl mtime_size                   # baseline
$PY evaluation/components/run.py recipe_fast_path --impl recipe_replay         # current (real model)
$PY evaluation/components/run.py recipe_fast_path --impl planner_only          # baseline (real model)
$PY -m pytest evaluation/components/test_components.py                  # offline, no model calls
```
Flags: `--only ID,ID`, `--max-calls N` (aborts when model calls exceed N), `--out path`.
`QAR_EVAL_MODEL` (default `haiku`) sets the model for every implementation, so they compare like for like.
Results go to `/tmp/qar_components/<component>_<impl>.json`; the summary line prints the model-call count.

## Components and their functions

| Component | Function | Scored by |
| --- | --- | --- |
| `classification/` | `classify(message, context) -> {"route": answer\|tool\|deep, "tool": name}` | route (and tool) match `expect` in `cases.json` |
| `quest_ops/` | `run_operation(message, world) -> {"reply"}` | the in-memory quest's state afterwards, plus no unrequested writes |
| `context_selection/` | `select(task, corpus) -> [file paths, best first]` | the labelled file's rank: top-1, top-3 (or within the solution's declared `budget`) |
| `staleness/` | `changed(before, after) -> {stale paths}` | exact set of edited or deleted files; precision and recall over all cases |
| `recipe_fast_path/` | `handle(message, world) -> {"reply", "exit_reason"}` | the fake tool world's recorded calls: right tool and args on a repeat, no lookalike write on a near miss, no tool on chat |

Add a solution: create `<component>/impls/<name>.py` exposing `build(meter) -> function`, count any
model call with `meter.record()` (or `meter.wrap(provider)`), and run it with `--impl <name>`.
`module:build` also works for a solution that lives outside this repo.
A component's score module may also define `aggregate(rows) -> str`; `run.py` prints it as the headline
(top-1 / top-3, precision / recall, correct per group). A solution that legitimately returns more than
the usual top 3 (the hybrid union) declares it with `select.budget = N`, and the scorer reports both.

Notes on the three newer components:

* `context_selection/` is zero-model. The corpus (`corpus.py`) is the repo checkout itself, read-only
  (`QAR_EVAL_CORPUS_ROOT` points it elsewhere), with one deterministic card per `.py` file built by the
  store's own extractors, because the production bootstrap needs a model. `vector_bge_small` and
  `hybrid_union` need the `BAAI/bge-small-en-v1.5` model in the fastembed cache (`/tmp/fastembed_cache`)
  and run with `HF_HUB_OFFLINE=1`; if it is missing they fail loudly instead of scoring 0%.
* `staleness/` is zero-model and builds its own tiny trees in `/tmp`. Two of its cases are known gaps of
  the current store, kept on purpose: an edit that restores the mtime is missed (`_current_sha` trusts a
  matching mtime for speed), and a deleted file is not reported stale.
* `recipe_fast_path/` spends real model calls (`QAR_EVAL_MODEL`). One solution instance sees the whole
  run in order, so the recipe store learns between cases; the scorer sends each tool's TEACH message once
  before its first ask. `--only` selects a subset (the old 1 / 10 / 30 subsets are in
  `../recipe_fast_path_eval.py`). It was ported without a live model run in the session that wrote it
  (no `claude` CLI there): the world, scoring and wiring are covered offline, the model path is not.

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
