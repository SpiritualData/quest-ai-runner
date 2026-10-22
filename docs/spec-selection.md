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

## Solution tags

A solution may carry tags (the `tags` column of `esoi/solutions.csv`, ids separated by semicolons), a field of the
evolvable-structure template: "What classifications may be necessary to avoid risk?" The list of tags is
`esoi/tags.csv`, the one source of truth: each row is a `tag`, its `meaning` (one sentence a solution can be judged
against, yes or no) and its `problem_ids`, the problems it mitigates. The solution that owns the list is
`tag-solutions`.

**The risks a tag mitigates are problems, linked, not prose.** The skill names four structural risks, recorded here
as problem records under `prob-solution-not-identified`:

- R1 (`esoi-r1-duplicate-solution`): adding a new solution that duplicates an existing one.
- R2 (`esoi-r2-overlap-not-leveraged`): not leveraging the benefits of an existing solution, such as partial
  overlap of purpose.
- R3 (`esoi-r3-co-change-missed`): not being aware of solutions that need to change definition or implementation
  due to something else changing.
- R4 (`esoi-r4-solution-misused`): using a solution when it should not be for the particular use case.

Every tag links at least one problem: one of R1 to R4, and the problems of this project (or another intent) whose
risk it mitigates. The explanation of a risk is written on the problem, not on the tag. R1 to R4 belong to ESOI
itself; they are recorded in this project until the evolvable-structure library defines them and lets another
project's records link them.

**A solution's connected problems are tags too, but not enough on their own.** The lookup reads a solution's tags
as its `tags` column plus its `problem_ids`; the problem ids are never copied into the column. A tag is still
needed where the solutions that must be consulted together do not share a problem, which is the usual case for
R3 (all the solutions that read the card format) and R4 (the solutions whose misuse is costly).

`python scripts/esoi_rules.py <tag, problem or solution id> ...` lists the solutions that carry any of them, through
either route; a solution id stands for its own tags.

**What each tag is for** (the R it mitigates is the first link of its row): `prompt`, `specifications`,
`measurement` and `memory` mitigate R1, a second prompt path, records path, measuring path or memory path;
`spending`, `model-calls`, `messages-to-people`, `external-calls` and `memory` mitigate R2, guards and budgets that
already exist; `card-format`, `model-calls`, `private-data` and `public-surface` mitigate R3, solutions that change
together when a shared shape changes; `irreversible`, `private-data` and `unattended` mitigate R4, solutions whose
Avoid lines are costly to ignore.

- **When a tag exists:** only if it mitigates a risk that is a linked problem, and no existing link, sub-intent or
  problem already identifies the same solutions.
- **The list is flat:** no tag has a parent.
- **Retirement:** when the problems it links are solved, or another mechanism (a link, a problem, a rule) now
  identifies the same solutions.

**How a tag is created, with its back-fill.**

1. The author of a solution (an AI or a person) first picks from the existing list.
2. If none fits, propose a tag with its one-sentence meaning and the problems it mitigates (R1 to R4 and any
   problem of this or another intent).
3. Back-fill: judge every existing solution against the meaning, yes or no. The judgement needs a model or a person,
   so this is a procedure, not a script.
4. Commit the new row in `esoi/tags.csv` and every back-filled solution row together.
5. Whenever a solution is written that fits no tag, ask whether an earlier solution carries the same risk. That is
   the same back-fill.
6. Retiring a tag removes it from every solution in the same commit.

The AI adds and retires tags itself. Rule Q1 (`scripts/esoi_rules.py`, run by `esoi check`) fails what can be
checked without judgement: a tag a solution uses that is not listed, a listed tag with no meaning, a listed tag that
links no problem or links an id that is not a problem record, and a listed tag used by no solution or by every
solution (it classifies nothing). A tag on one solution only is printed as a note, not a failure: it still lets the
author of a second solution find the first (R1, R2), but the back-fill is worth a second look. Whether a tag
mitigates its risk is measured by `metric-tag-solutions` (planned, unmeasured): on the labelled requests of
`search-cards-and-specs`, the share where the right solution is missed without the tag. In that search, tags are
planned as a cheap filter or boost, unmeasured.
