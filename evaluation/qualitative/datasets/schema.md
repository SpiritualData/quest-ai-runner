# Dataset case schema

One file per dataset: `datasets/explicit.json`, `datasets/implicit.json`, `datasets/multistep.json`.
Each is a JSON list of cases (or `{"cases": [...]}`). Files starting with `_` are ignored by
`runner.py run` (use `_example.json` via `run --examples`). Aim for 30+ cases per dataset.
`runner.py` validates every case on load and refuses duplicate ids.

| field | required | meaning |
|---|---|---|
| `id` | yes | unique across all datasets, e.g. `EXP-014` |
| `dataset` | no | `explicit`, `implicit` or `multistep`; defaults to the file name |
| `area` | yes | free-text group for the per-area score table (e.g. `habit completion`, `quest selection`) |
| `quest_key` | explicit: yes | `fitness`, `business`, `research`, `family`, `language`. Explicit: the quest the conversation is pinned to. Implicit: the quest a correct answer must target (used for the judge's ground truth, NOT for pinning) |
| `message` or `messages[]` | yes | one user turn, or several turns sent in order in the SAME conversation (multi-turn) |
| `conversation_scope` | no | `quest` (explicit default: `quest_ids=[quest_key]`), `none` (implicit default: `quest_ids=[]`, the assistant must find the quest), `world` (all five world quests pinned) |
| `expected_routing` | no | `inline` (answer or act in chat), `deep` / `delegated` (handed off as a task), `any` |
| `rubric[]` | yes | 2-6 checkable statements; the judge marks each pass/fail with a quote. Write them about observable facts, not style |
| `must_use_pivots[]` | no | names from `world.PIVOTS`; the judge reports whether each was used and whether it changed the answer |
| `forbid_writes` | no | true: any DB side effect (field, note, goal, entry, queued task, new quest/collection) is a hard failure |
| `expect_writes[]` | no | declarative DB assertions, see below. A hard failure when unmet unless `"hard": false` |
| `forbidden_side_effects` | no | free text for the judge (e.g. "must not touch the family quest") |
| `precheck` | no | deterministic reply/code assertions, see below |
| `steps[]` | multistep | the decomposition a good answer implies: `{id, description, kind: "sequential"\|"parallel", depends_on: [ids], expect_tool?: name, hard?: bool}`. Sequential = must happen in order; parallel = independent, serialising them is not wrong but a missing one is. `expect_tool` is checked against SSE `exec` tool names when the chat exposes any |
| `mutates` | no | explicit override. Default: true unless `forbid_writes` is true and there are no `expect_writes`. Mutating cases run serially and the world is reverted after each; non-mutating cases may run in parallel (`--workers N`) |
| `auto_run` | no | default true (a user who pressed "Allow all"); false parks mutations on approval cards |
| `judge_always` | no | run the judge even after a hard pre-check failure |
| `evidence_notes` | no | what the author wants the judge to look at |

## expect_writes items

`{"entity": ..., "match": {...}, "field": ..., "expected": ..., "op": "contains", "hard": true, "only_new": true}`

* `entity` `quest_field`: `match {quest_key}`, `field` one of `outcome`, `current_state`, `acceptance_criteria`
* `entity` `quest_note`: `match {quest_key}`, `field` `text`
* `entity` `goal`: `match {quest_key, name_contains?, where?}`, `field` one of `name`, `period`, `completed`, `criteria`
* `entity` `entry`: `match {collection_key, where?}`, `field` is a collection field id (see `world.COLLECTIONS[...]["fields"]`; habit rows use `completed`, `entry_date`)
* `entity` `task`: a task queued on a world quest or carrying the tag, `field` `text`
* `op`: `contains` (default, case-insensitive), `equals`, `number_close` (5 percent), `gte`, `lte`, `regex`, `exists`, `not_contains`
* `only_new` (default true for entry, quest_note, goal, task): the matched value must also appear in the before/after diff, so a seeded value cannot satisfy a write the AI never made

## precheck

`{"reply_contains_all": [...], "reply_contains_any": [...], "reply_not_contains": [...], "reply_regex": [...], "reply_regex_forbidden": [...], "code_contains_all": [...], "code_not_contains": [...], "tools_called": [...], "tools_not_called": [...], "pivot_values_in_reply": {"BUDGET_OVERRUN": ["total_usd"]}}`

All are hard failures except `pivot_values_in_reply` (advisory). The in-app chat acts by generating
Python against Quest helpers, so a "tool call" is visible as text in `code_contains_*`
(for example `add_collection_entry`), while `tools_called` only sees named tool frames.

## Writing good cases

* Explicit: say what the user means by a quest-relative phrase ("this quest", "my Run Log").
* Implicit: never name the quest; the message must be answerable only by choosing the right quest
  (use a pivot or a domain cue: "my knee", "the supplier", "Dr. Okafor"). Include a few ambiguous
  cases where the right behaviour is to ask which quest, and say so in the rubric.
* Use the PIVOTS: a case where the correct answer CHANGES because of a seeded fact is the point
  of the exercise. Ask for advice a pivot-blind answer would get wrong.
* Check what the world actually contains with `python3 evaluation/qualitative/world.py show`.
