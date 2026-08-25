# GOALS.md — the quest's plan, in the folder, editable both ways

`QUEST_SYNC.md` answers *what is this quest and what has been said about it*. It does not answer
*what are all my goals*. Its state block carries one outcome; its `next_steps` block carries the
two or three things to do now. The plan itself — the ladder of quarter, month, week and day goals
— lives only in Quest.

A folder without it grows a substitute. `goals.yaml`, `todos.md`, `today_tasks.md`, whatever that
consumer invented. Each one drifts from Quest the moment either side changes, each one is
different, and none of them is what the next consumer's folder looks like.

So: one standard file, and real two-way sync.

```python
pull_quest_goals(client, quest_id, folder)                     # Quest -> GOALS.md
push_goals_to_quest(client, quest_id, folder)                  # GOALS.md -> Quest
sync_quest_goals(client, quest_id, folder, direction="both")   # push, THEN pull
```

## The file

One managed block, the same HTML-comment markers the rest of the package uses, so prose outside it
survives every re-render.

```markdown
<!-- QAR:MANAGED:goals START -->
## Goals

_3 goal(s) across 2 period(s); 1 completed._

### Quarter
**Q3 2026 (Jul - Sep)** <!-- period:2026_Q3 scope:quarter -->
- [ ] <!-- id:goal_00c5922b --> Secure commitment from all committee members (due 2026-09-30)
- [x] <!-- id:goal_bfeda075 --> Finish the concept paper
<!-- QAR:MANAGED:goals END -->
```

The period key rides in a comment next to the human label because a bullet typed under that
heading has to be creatable, and `Q3 2026 (Jul - Sep)` is not what the API takes.

Completed goals are kept and ticked rather than dropped. A plan that silently loses its finished
rows reads, a quarter later, as though the work was never scheduled — and it would also make the
file disagree with Quest about what exists, which is what breaks push.

## The three edits that push

| Edit | Effect |
| --- | --- |
| Tick a box on an open goal | The goal is completed |
| Change the text after an id | The goal is renamed |
| Add a bullet with **no** id under a period heading | A goal is created in that period, and the bullet is stamped with its new id |

Stamping is what makes a repeated push a no-op — the same trick `quest_folder_sync` uses for note
bullets.

Two things deliberately do not happen:

- **Un-ticking does not reopen a goal.** Reopening is a real decision with its own endpoint, and
  inferring it from a missing `x` would turn any rendering hiccup into a silent state change.
- **A bullet with no id under no period heading is skipped, loudly.** There is no honest guess for
  where it belongs, and filing it somewhere arbitrary is worse than not filing it.

## Push before pull

`direction="both"` pushes first, which is the opposite order to `quest_folder_sync`. The reason is
worth stating because it is easy to "fix" back: the edits live *inside* the managed block, and a
pull regenerates that block from Quest. Pulling first would erase the tick before it was ever
sent. Pushing first sends it, and the pull that follows brings back Quest's own view of what just
happened — so the round trip corrects itself instead of losing work.

## The quest state block pushes too

`QUEST_SYNC.md`'s own goal block is editable, within limits that come from the API rather than
from us. `push_quest_state` runs automatically on `direction="both"`.

| Field | Pushes? |
| --- | --- |
| `**Goal:**` (outcome) | Yes |
| `**Status:**` (completed) | Yes |
| `**Current state:**` | **No** — in no role's write scope server-side |
| `**Accepted strategies:**` | **No** — objects with ids and acceptance flags that bare titles cannot reconstruct |

The two that cannot push are **reported**, not dropped: the result's `unwritable` list names them,
so a caller can say so out loud. Someone who retypes their current state deserves to be told it
did not land, rather than discovering it next week.

One more trap the code handles: the role-scoped write endpoint reports a partial refusal in the
response **body** (`{"ok": false, "blocked": ["outcome"]}`), not as an HTTP status. A caller that
only watches for exceptions believes every field landed. `push_quest_state` surfaces `blocked` and
`held` instead.

## Configuration

```python
RunnerConfig(
    quest_folder_map={"quest_abc123": "/path/to/folder"},
    quest_folder_sync_direction="both",   # goals follow this
    quest_goal_sync=True,                 # default
)
```

The poller syncs goals alongside the folder, in its own try/except. That separation is deliberate:
a quest whose goals cannot be listed (an older backend, a permission gap) must still get its
`QUEST_SYNC.md`, and one guard around both would lose that.
