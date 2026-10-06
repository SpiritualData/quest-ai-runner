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
| `must_use_pivots[]` | no | names from `world.PIVOTS` the case REQUIRES. Every one must come back `used`, or `judge.case_passed` fails the case whatever the score. Only list a fact the assistant has to retrieve itself: if the user's own message states it, using it proves nothing |
| `bonus_pivots[]` | no | pivots that earn credit and are reported, but never fail the case. Where the old rubrics said "bonus", "optionally" or "may mention", the pivot belongs here |
| `pivots_change_answer` | no | require at least one must-use pivot to come back `changed_answer`. Defaults to true for `implicit` (that dataset exists to test exactly this), false elsewhere; set it true on any case whose point is that the fact changes the answer |
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

* `entity` `quest_field`: `match {quest_key}`, `field` one of `outcome`, `current_state`,
  `acceptance_criteria`, `preferences`, `timeline_days`, `measurable_outcomes`. Those are the five
  fields the chat's `update_quest_fields` helper accepts plus the measurable-outcome list;
  `purpose` is NOT writable by the AI (the helper rejects the key), so never assert one
* `entity` `quest_note`: **refused by the loader.** This surface has no add-note helper and `notes`
  is not one of the five raw collections it may touch, so a note write can never happen. Assert on
  the reply (and on the honest alternative: `current_state`, a goal update, a collection entry)
* `entity` `goal`: `match {quest_key, name_contains?, where?}`, `field` one of `name`, `period`,
  `completed`, `criteria`, `description`, `scheduled_date`, `deadline`. For a NEW goal use
  `description`, not `criteria`: `create_goal(quest_id, name, description, target_date, ...)` is
  the only helper, it has no `criteria` parameter, and it always files the CURRENT MONTH period.
  Never assert a week period on a goal the AI created
* `entity` `entry`: `match {collection_key, where?}`, `field` is a collection field id (see `world.COLLECTIONS[...]["fields"]`; habit rows use `completed`, `entry_date`)
* `entity` `task`: a task queued on a world quest or carrying the tag, `field` `text`
* `op`: `contains` (default, case-insensitive), `equals`, `number_close` (5 percent), `gte`, `lte`, `regex`, `exists`, `not_contains`
* `only_new` (default true for entry, quest_note, goal, task): the matched value must also appear in the before/after diff, so a seeded value cannot satisfy a write the AI never made

## precheck

`{"reply_contains_all": [...], "reply_contains_any": [...], "reply_not_contains": [...], "reply_regex": [...], "reply_regex_forbidden": [...], "code_contains_all": [...], "code_contains_any": [...], "code_not_contains": [...], "tools_called": [...], "tools_not_called": [...], "pivot_values_in_reply": {"BUDGET_OVERRUN": ["total_usd"]}}`

All are hard failures except `pivot_values_in_reply` (advisory). An unknown key is a load error, so
a typo cannot leave a case looking strict while asserting nothing. The in-app chat acts by
generating Python against Quest helpers, so a "tool call" is visible as text in `code_contains_*`
(for example `add_collection_entry`), while `tools_called` only sees named tool frames. Use
`code_contains_any` when several helpers would each be a correct route, and `reply_regex_forbidden`
to catch the dishonest claim ("I have added a note") that a diff-based assertion cannot see.

## What this surface can and cannot do: never author a case it cannot pass

`world.CHAT_CAPABILITIES` is the verified list (from quest-backend's `_SANDBOX_HELPER_NAMES` and
`app/prompts/ai_commands.yaml`), and the judge is given it with the ground truth. The traps that
already cost a round of cases:

* **No quest-note write**, so "add a note" cases can only test the honest substitute.
* **`purpose`, `strategies` and anything outside the five allowed fields are rejected** by
  `update_quest_fields`.
* **A quest with `autopilot.mode == "off"` refuses every AI field write**, even one the user asked
  for outright, and files a decision-request instead. `world.AUTOPILOT_BY_QUEST` puts four quests
  on `act` for this reason and leaves `family` off on purpose, so `family` field cases must expect
  a proposal and an honest "not applied", never a write.
* **A new goal is always in the current month and has no criteria field** (see `expect_writes`).
* **No mail helper.** No case in this suite may instruct an outbound send: the eval runs against
  the dev team's real recipients, so `code_not_contains: ["send_quest_email"]` guards the email
  cases and the correct behaviour is a draft in the reply.
* **No web-search helper** in the code path, and **`create_assistant_task` is on the allow-list but
  unexercised.** Where a capability is unverified, assert softly (`"hard": false`) and put the
  weight on a rubric item that fails a FALSE CLAIM: an honest "I cannot" passes, a claimed action
  with nothing in the diff does not.

## Writing good cases

* Explicit: say what the user means by a quest-relative phrase ("this quest", "my Run Log").
* Implicit: never name the quest; the message must be answerable only by choosing the right quest
  (use a pivot or a domain cue: "my knee", "the supplier", "Dr. Okafor"). Include a few ambiguous
  cases where the right behaviour is to ask which quest, and say so in the rubric.
* Use the PIVOTS: a case where the correct answer CHANGES because of a seeded fact is the point
  of the exercise. Ask for advice a pivot-blind answer would get wrong.
* Check what the world actually contains with `python3 evaluation/qualitative/world.py show`.
