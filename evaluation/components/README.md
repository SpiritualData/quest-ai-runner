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

Add a solution: create `<component>/impls/<name>.py` exposing `build(meter) -> function`, count any
model call with `meter.record()` (or `meter.wrap(provider)`), and run it with `--impl <name>`.
`module:build` also works for a solution that lives outside this repo.

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
