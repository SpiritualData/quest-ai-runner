# Selecting the intent specifications an input concerns

When a new request or input arrives, a run should act by the specifications that concern it: the problems it shows, the
ideals that decide what "better" means, the solutions and metrics already defined. `core/spec_selection.py` finds them in an
ESOI specification index. The index itself is built without AI by the `esoi` library (`esoi spec index --out spec_index.json`,
see its `docs/explanation/finding-specifications.md`); this module is the AI system on top of it, because vector search and
model calls belong here and not in the library.

## The pipeline

| Stage | Does | Cost |
|---|---|---|
| 1. Lexical | field-weighted TF-IDF over the index's own terms and document frequencies | none |
| 2. Semantic (optional) | `vector_search(text, k) -> [entry id]`, supplied by the consumer over the same entries; fused with stage 1 by reciprocal rank | the consumer's |
| 3. Final choice | candidates' one-line descriptions are cut into batches of `batch_size`, one cheap model call per batch, all in parallel | one call per batch, one call's latency |
| 4. Links | each selected record brings up to `expand` linked records, marked in `linked` | none |
| 5. Read in full | the caller reads tier 2 of only the returned ids | the caller's |

```python
from quest_ai_runner.core.spec_selection import load_spec_index, select_specs

index = load_spec_index("spec_index.json")          # refuses a version it does not understand
chosen = select_specs(task_text, index, provider=provider, model="fast", vector_search=my_search)
chosen.ids         # best first, then linked records
chosen.linked      # linked id -> the selected record it came in through
chosen.failed_batches, chosen.calls
```

Safe by construction: an id the model returns that was not offered is dropped; a batch whose call or parse fails keeps its
candidates in fused order; a failing vector search leaves the lexical answer; with no provider the fused candidates are the
answer. A request that shares no word with any record and has no vector search selects nothing and spends nothing.

## It is a solution with alternatives

`spec-selection` (records in `esoi/`): `lexical-vector-gated` is selected (chosen by the metric's balanced score), `lexical-vector-llm` (batched judging), `lexical-vector` (no model) and `lexical-only` are the alternatives and baselines. The metric
`metric-spec-selection` runs `evaluation/components/spec_selection/` over `fixture_index.json`, the real output of
`esoi spec index` for the esoi repository's own records, against labelled requests: the records each concerns, a ceiling on
how many ids may come back, and requests that concern nothing. The metric is real. The judge is the runner's own model client
making real cheap-model calls (`ClaudeCliProvider` on the logged-in `claude`, model `haiku` unless `QAR_EVAL_MODEL` says
otherwise) and the vector search is real local bge-small embeddings; nothing is scripted, a missing `claude` or embedder fails
loudly instead of scoring a stand-in, and the arm is `PAID`, so `esoi run --stale` asks before running it. The request kinds
are `direct` (the record's own words), `paraphrase` and `solution` (other words, so lexical alone misses), and `none`
(a model that returns everything, or the baseline that always returns something, fails). Scripted-judge checks of the
plumbing (batching, invented ids, a failed call, links) are unit tests in `tests/test_spec_selection.py` and are not the
metric. Refresh the fixture with `esoi spec index --out evaluation/components/spec_selection/fixture_index.json` (the cases
name real record ids, so check them after).

The metric ranks by a balanced score of accuracy (70%), cost (15%) and time (15%) with the budgets in `esoi.toml` `[balance.spec_selection]`. On 76 labelled requests (2026-10-20) the gated arm scored 0.885 (67/76, about $0.0001 and 2 s a request), batched judging 0.418 (21/76: on the larger index it returned crowds), lexical plus vector 0.816 and lexical only 0.788.

## Day-to-day use (`core/repo_specs.py`)

A run working in a repository with an `esoi.toml` (or `QAR_SPEC_ROOT`) gets four read-only tools, `find_specs`, `related_specs`, `intent_specs` and `show_spec` (`core/tools_repo_specs.py`). The index is cached and rebuilt only when a record file changes, a local vector search is added when `fastembed` is installed, and the run walks links on demand instead of reading every record. The same functions run free from a shell: `python -m quest_ai_runner.core.repo_specs find|related|intent|show`.

A deep worker calls them from its own shell, a new process that may start in another folder and so would not find the repository
(`unknown tool 'find_specs'`). The brief's command therefore carries the root the run's process found, `python -m
quest_ai_runner.tools --spec-root <folder> call ...` (the flag sets `QAR_SPEC_ROOT` for that one call), and the four tools are
not named in a brief at all when no root is found (`ToolSpec.needs_spec_root`): a brief never names a tool its worker could not call.

It is the slice source that `iterate-by-spec` and `ground-in-intent` were waiting for. `iterate-by-spec` calls it, lexical only
and for deep goals only (`core/spec_slice.py`); `ground-in-intent` does not. A record is otherwise not part of the context a run
is assembled with: it reaches a run only through the tools above. Searching cards and records in one pipeline, with a record as
a read-only view of its row, is the planned solution `search-cards-and-specs` (`esoi/solutions.csv`), decided by its own metric.
