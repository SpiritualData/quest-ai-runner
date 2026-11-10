# Digest a large collection for one need

A prompt that holds every entry of a large collection is neither affordable nor readable. The `digest-records-for-a-need` solution (a nested Subconscious solution; its selected arm is `digest_by_need`) lets the
need decide what is read and to what depth: it reads the records one source group at a time with cheap sub-analyses and hands the planner a few
cited findings. The library knows nothing about where records live. The host turns its own collections, notes and insights into source groups.

## The call

```python
import quest_ai_runner
from quest_ai_runner.api import DigestSourceGroup, DigestUnit

log = DigestSourceGroup("collection:training", "Training log", loader=lambda: [DigestUnit(f"entry:{n}", f"Entry {n}", text) for n, text in rows])
context = quest_ai_runner.run(
    "digest-records-for-a-need",
    {"task_text": "What has this person already tried that bears on running without pain?", "meta": {"source_groups": [log], "background": "Run a marathon"}})
```

No arm is named: the selected arm answers. `choose-the-context` hands a request that brings `source_groups` to this solution, so a caller of that line gets the same answer.

`run` builds the arm with the model seat the lane already has (`provided={"vendor-model-caller": ...}` supplies another); every cap has a default and
`meta` overrides only what differs (`max_analyses`, `window_chars`, `concurrency`, `direct_chars` and the rest of `DEFAULTS` in
`quest_ai_runner/the_intelligence/subconscious/digest_by_need.py`). The answer is the hub's `AssembledContext`:

| Field | What it holds |
|---|---|
| `context_view` | The reduced text: findings per group in group id order, each with `[sources: ids]`. Never the records. |
| `card_ids`, `card_metadata` | The cited record ids and their titles. |
| `sources` | One `coverage` entry (`analyses` made, windows `reused`), then one entry per group with `status` and `reason`. |
| `partial` | True when any group was `failed` or `skipped`. |

Statuses: `analysed`, `cached`, `nothing_relevant`, `failed`, `skipped` (over the budget of sub-analyses, or no model) and `direct` (a body of at most
`direct_chars` characters and `direct_records` records per group is handed on as it is, with no model call).

## What is guaranteed in code

- A finding keeps only ids that were in the window it read; an invented id is dropped, and a finding left with no id is dropped.
- The reduce step is plain code (group id order, clipped to `digest_chars`). A model pass over the findings was left out: the hub's
  `consolidate_context` chooses card items by id and would add a call per run and could drop a citation.
- A digest is cached under the need (`need_key`, else a hash of its text), the background and the content of the window. An unchanged window is never
  read again, another need makes another digest from the same records, and a changed record re-reads only its window. Pass any `DigestCache`
  (`get` and `put`) as `meta["cache"]` to keep digests somewhere durable; the default lives as long as the arm.
- A failed window is not cached, so the next call retries only it.
- The prompts are `digest-records-for-a-need.digest-head` (rules, need and background: the cached prefix) and `digest-records-for-a-need.digest-group` (the group's
  records: the volatile tail), built with `PromptLayers` and free of em dashes.

## Measuring it

`esoi run digest_by_need --impl digest_by_need` scores the seeded cases (sixty entries of which three matter, another need over the same records, one
group that fails) with a stubbed model, so it costs nothing. `--impl whole_collection` is the floor: every record in one prompt. Unit tests are
`tests/test_digest_by_need.py`. A live run with a real cheap model is a decision, not part of the check.
