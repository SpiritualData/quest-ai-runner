# /joshua review 2 (Opus): QAR adaptable structure, after review 1 was applied

Run on branch october after commit bc4fca5. Verdict: the registry describes QAR accurately at the file level, but QAR is not yet adaptable: selection is by hand, measured implementations are not always the live code, and config leaks into core. `check_structure.py` passes while these gaps exist. Review 1 is `joshua_review_2026-10-15.md`.

## Review 1 items: status found
- 4 live_impl/config_key: open. 5 layer direction: half done (top-level modules, runner<->config cycle, tools/ unchecked). 6 port discovery: Protocol only, new rows were boilerplate. 7 prompt size: row only, no harness. 8: columns only (git_sha/cases_sha empty, leaderboard ignores cases_sha, 17 of 28 solutions unlinked from components).

## New gaps, ranked (size; offline)
1. Eval measures code that never runs live: context_selection hybrid_union re-implements fusion, live HybridContextAssembler differs. Add S12: each impls/*.py wraps the live class or is marked alternative. (medium; yes)
2. Selection not justified by its own scoring: top-3 vs 6-file budgets both 11/15, cost $0. Put context tokens in compared cost; held-out cases missing for context_selection, quest_ops, staleness. (small; yes)
3. Config does not enter only through RunnerConfig: core reads QAR_* env in 10 files, adapters in 25; 173 vars in code vs 120 documented. S13 no os.environ in core with waivers, then migrate. (check small, migration large; yes)
4. Selection is hand-written if/elif per port, not data. One resolve(port, name) table keyed by solutions.csv ids. (medium; yes)
5. "invariant" is a free pass: need invariant_reason; autopilot and context-updates demoted to partial, linked to assemble-a-prompt. (small; yes)
6. Build prompt_size harness (max/p95 chars, duplicate-block count per assembly point, fixed fixture, hard threshold). (medium; yes)
7. README/CLAUDE.md drift: "four adapter interfaces", adapters list stale, "RunnerConfig only", S7 scope, results location, evaluation/components/README "current" labels. (small; yes)
8. Splits hug the size limit: config.py 1,472 of 1,500 lines, still holds resolvers; runner<->config cycle. (medium; yes)
9. S5 checks a docstring exists, not that it states a role. (small; yes)

## Do now (offline check and CSV work)
1, 2, 5, 7, S6 top-level and runner->config rules, cases_sha enforcement, reverse solution-link check. Items 3, 4, 6, 8 need a design pass first.
