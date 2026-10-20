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

`spec-selection` (records in `esoi/`): `lexical-vector-llm` is selected, `lexical-only` is the baseline. The metric
`metric-spec-selection` runs `evaluation/components/spec_selection/` over `fixture_index.json`, the real output of
`esoi spec index` for the esoi repository's own records, so a change to the index format fails there. Its judge is scripted
and declared simulated: it measures the pipeline (narrowing, fusion, batching, dropping invented ids, surviving failures), not
a model's taste. Refresh the fixture with `esoi spec index --out evaluation/components/spec_selection/fixture_index.json` (the
cases name real record ids, so check them after).

It is the slice source that `iterate-by-spec` and `ground-in-intent` were waiting for; neither calls it yet. Wiring it into a
run's context is a decision for those solutions' own metrics, not assumed here.
