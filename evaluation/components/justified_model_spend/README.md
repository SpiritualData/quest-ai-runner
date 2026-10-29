# justified_model_spend (QME8)

The metric `metric-justified-model-spend`: the share of AVERAGE model spend, over the measured prompts, that sits on prompts that pass.
A prompt passes when it is a registered spec, its owner metric has a current row, its call beats its ablation on the owner's cases
(strictly more right, a tie fails), and its maximum dollars per turn and seconds per call stay inside its own declared budget.
`cases.json` has one entry per measured prompt; `score.unmeasured()` lists every registered prompt with no entry. Run it:
`PYTHONPATH=../evolvable-structure/implementations/python python3 -m esoi run justified_model_spend --impl spec_registry_ablation`.

## Restoring it in one step (the daily pass)

Owner rows go stale after any commit under `quest_ai_runner/` or `evaluation/components/`, and QME8 then reads 0.0. One command re-records
every owner arm and the ablation arm, in order, each under `flock /tmp/qar-heavy.lock`:

    python3 evaluation/components/justified_model_spend/record_owner_arms.py --plan      # print the commands, run nothing
    python3 evaluation/components/justified_model_spend/record_owner_arms.py             # floors, then subscription arms, then the ablation arm
    python3 evaluation/components/justified_model_spend/record_owner_arms.py --api       # also the four paid fast-tier arms (cap: --api-usd-cap, default 0.05)

The order and arguments are `owner_arms.json` (tier `offline`, `subscription` for `claude -p`, `api` for a paid fast-tier call). Run it on a
committed tree: it refuses a dirty one (`--allow-dirty` overrides; `--wait-clean SECONDS` waits for other agents' edits to be committed), because a row recorded on uncommitted code is stale at birth. Each arm goes
through `measure.py`, which records the results row and the per-call trace in `evaluation/traces/justified_model_spend/` together; a paid arm
that makes no model call records nothing (`--min-calls`) and stops the pass. Narrow a pass with `--tier` or `--only component/impl` (the ablation
arm is then skipped). Rebuild the trackers afterwards with `esoi tracker build`. A new measured prompt is a `cases.json` entry plus its owner arms
in `owner_arms.json`; `test_record_owner_arms.py` fails when a case names an arm the manifest does not list.

## Production latency

Eval traces are labelled `eval-trace`. The runner also persists every model call it makes, tagged with the prompt spec id, in a JSON-lines call
log (`usage.CallLog`, next to the usage file, `QAR_CALL_LOG_PATH`). `production.py [--spec watch-a-run.overseer]` prints calls and the average and
maximum seconds, tokens and dollars per call from it, and says so (exit 1) when the log holds none.

## Prompts with no case

Why each is still unmeasured (none has an owner arm that runs the shipped prompt on labelled cases with a no-call ablation):

- `verify-goal.verdict`: the goal verifier runs inside a deep run's goal loop; its owner QME2 measures delegated tasks meeting their goal (whole runs), not the verdict call, and a verdict needs a finished piece of work to judge. Needs a labelled work-product set.
- `route-a-message.cascade-review`: off by default (`planner_cascade`); a review of a low-confidence routing decision needs labelled decisions with their gathered context, and no component has them.
- `judge-a-decision.intent-directive`, `.mode-release`, `.landed-work-denial`: the owner `judges` arm runs them under a scripted provider only; no live labelled cases yet.
- `feed-context-sources.*`, `ground-in-intent.*`, `replay-known-operations.fill-args`, `search-cards-and-specs.pool-judge`, `select-context.*`, `select-a-rep.explicit-ask`: the owner components score the surrounding rule or a whole chat turn (`intent_grounding`, `feed_context_sources`, `select_a_rep`), not the one call, and have no ablation with the call dropped.

The planner and the answer prompt are not registered prompt specs; the planner is measured only as the ablation of `operate-a-quest.pre-route` (`quest_ops/qar_orchestrator`).

Listed by `score.unmeasured()` and in the QME8 note, never guessed. A prompt is added only with a labelled owner arm whose no-call ablation can be
run on the same cases; the planner and the answer prompt are not registered prompt specs yet (the baseline of unmigrated call sites in
`tests/test_prompt_specs.py`), so they cannot be listed here.
