# The feedback ledger: what was asked, and how far anybody got

**Code:** [`runner/feedback_ledger.py`](../quest_ai_runner/runner/feedback_ledger.py).
**Wiring:** `RunnerConfig.feedback_ledger` (on by default), `feedback_ledger_path`.
**Reads it:** `quest-ai-runner context <quest_id> --tracked`.

[Automated context updates](context-updates.md) answers *what has arrived since an assistant last
looked*. This answers the question that outlives it: **what became of it**.

## The problem

Every channel could say what had arrived. None could say what had been done about it, so "handled"
was inferred from circumstance: an assistant note appearing after a person's note, a reply sitting
under their comment, a run finishing later the same afternoon.

**Replying is not doing.** A person who writes "from now on, put the page numbers in" and gets a
reply that afternoon had their instruction marked handled forever, on the strength of a timestamp,
while the page numbers stayed missing from everything written since.

And the second half of the problem is that one flag cannot express the difference between:

- **a request**, finished once ("add a Status column to the limitations sheet"), and
- **a standing rule**, never finished ("from now on include what you are doing on limitations
  gathering in every report").

Marking a standing rule done the first day it is honoured is exactly how it stops being followed:
the record says handled, nothing raises it again, and the third report drops it with nobody the
wiser.

## The model

One row per ask, whatever channel it arrived on, keyed by card + source + the source's own id.

| | |
| --- | --- |
| **kind** | `request` · `standing` · `context` · `unknown` |
| **state** | `open` · `in progress` · `done` · `in force` · `partially applied` · `needs reapplying` · `declined` · `noted` · `superseded` |
| **still owed** | `open`, `in progress`, `partially applied`, `needs reapplying` |
| **also held** | their words verbatim, who, where, when, how many times a run has acted, the last run's note, the guidance card id, and a bounded history of who moved it and why |

`in force` is deliberately NOT "still owed". A rule that is being followed does not need
re-announcing every morning; it needs to be in the guidance the next run retrieves. It re-enters
the owed set the moment it is only partly applied or has lapsed.

## Who sets a status

**The run does, in the receipt it already writes.** It is the only participant that knows what it
did, and asking a second model to grade the first would be dearer and less true. What changed is
that a receipt line now opens with a **disposition from a listed vocabulary**, so the account can
be recorded rather than only displayed:

```
Context used:
  [U1] done: added both columns; standing: report limitations and case work every time
  [U2] partial: csv drafted, not synced to Sheets yet
  [U3] noted: nothing to do
```

| disposition | what it means | where it leaves the item |
| --- | --- | --- |
| `done` | a one-time request, and you finished it | done, or **awaiting acceptance** on a ledger that requires it (below) |
| `partial` | worked on, not finished | in progress, still owed |
| `standing` | an instruction for future work; followed this time | in force, and written to guidance |
| `standing-partial` | a rule you have only been able to follow in part | partially applied, still owed |
| `asked` | you put a question back to them; you did NOT act on it | awaiting answer, still owed |
| `blocked` | you started and are stuck on something outside your control | blocked, still owed |
| `declined` | deliberately not doing it | declined |
| `noted` | they were telling you something | noted |
| `not used` | did not use it | unchanged |

`blocked` leaves the item's kind alone -- a standing rule that is stuck is still a standing rule --
because "blocked" says nothing about what KIND of ask this is, only that it is stuck.

This is a **choice from a list**, which is what makes reading it back legitimate under hard rule #3:
the run is asked for a structured decision and its answer is recorded, rather than its prose being
scanned for words that look like completion. A disposition the library does not recognise moves
nothing at all, so an item can only ever fail to move, never move wrongly. An unrecorded answer
costs one repeated question; a wrongly recorded one costs the request itself.

**A person outranks a run.** A state a human set is locked, and no run's disposition overwrites it.
Otherwise the ledger is the assistant marking its own homework.

**One paragraph, two asks.** People do not write one ask per note. A line carrying two dispositions
splits the note into two tracked asks, sharing the words they came from, each with its own state
from then on. Without it, a note holding a fix and a rule has to lose one of them: mark it done and
the rule stops existing, mark it standing and the column never gets built.

## A run's `done` is a claim, not a conclusion

For a lane whose own recurring work is the ask, `done` meaning done is fine: the run and the work
are the same party. It is not fine the moment the asks come from PEOPLE (a mailbox, a note, a
comment) and a live product surface is what they read: an assistant that can mark its own homework
finished is exactly the failure this module exists to end, one level up.

So `FeedbackLedger(..., requires_acceptance=True)` changes exactly one thing: a run's `done`
disposition, from anyone other than a person (`by != "person"`), lands the item in `awaiting
acceptance` instead of `done`. Nothing else about the vocabulary moves -- `standing`, `partial`,
`asked`, `blocked` all behave exactly as documented above -- because the claim/conclusion gap only
exists for the disposition that means "finished."

`awaiting acceptance` IS still owed (it is in `OPEN_STATES`), on purpose: a claim is not a
conclusion, and an item that stops being owed on the strength of the claim is the assistant marking
its own homework one step later. The ONLY way out is `accept()`, which refuses to run for anyone but
a person (`by="person"`) and locks the item exactly like `set_state(..., by="person")` does, so a
later run cannot reopen what a person already closed:

```python
ledger = FeedbackLedger(store=..., requires_acceptance=True)
...
ledger.accept(card_id=quest_id, source="quest_notes", item_id=note_id, note="looks right")
```

A ledger with the gate off (the default -- every existing lane) never produces `awaiting
acceptance` at all; a run's `done` still means done, exactly as before this existed.

Two more additions that came in alongside the gate, both in the same spirit of not overstating what
happened: `set_state(..., kind=...)` is how a person turns an untriaged row (`unknown`, `open` -
what an automatic capture creates) into a request, a standing rule, or a `question` -- classifying
is a person's call, and `FeedbackItem.authorizes_execution` answers `False` for `unknown` and
`question` kinds and for `awaiting acceptance` state, so a capture nobody has looked at, or a
question nobody has answered, or a claim nobody has accepted, can never be read as an instruction
to act on its own. And `apply_disposition`/`set_state` take an `evidence: Sequence[str]` (kept as
refs -- a URL, a message id, a commit -- rather than prose a person cannot go and check) and a
`run_id` naming which run made the claim, both surfaced back in `status_line()`.

## What the next run sees

Everything still owed rides into the next collection **as refs alongside the day's news**, marked
`still owed` and carrying its state in its own title:

```
[U1] 2026-09-10 · still owed · this quest · "on the Ragin doc: actually it didn't work..." · needs an answer
    jmathias@... asked for this on 2026-09-10; started, not finished (csv drafted, not synced to Sheets yet)
```

They are refs and not a separate paragraph because a ref is the only handle a run has for
accounting for something: an owed item outside the refs is an owed item nothing can ever close.

Only channels that carry **asks** are tracked: notes and document comments. A reflection, a habit
log and a capture are a person recording their own day, and nothing is owed on them. Getting this
wrong is not cosmetic: the first live run of this tracked the habit log and the daily reflection,
and the next morning both came back as "still owed, needs an answer", which is an assistant asking
a person to answer their own diary.

## The merge with guidance

A standing rule and a [guidance card](guidance-cards.md) are the same thing seen from two sides, so
an item a run marks `standing` is written to whatever guidance store the deployment wired, **in the
person's own words**, with the item id on it. The two halves then do different jobs instead of
duplicating each other:

- the **card** carries the rule into every future run, through retrieval, without being re-delivered
  as news;
- the **ledger** carries whether it is actually being followed, which a card cannot express.

Neither can do the other's job. A card with no status cannot tell you a rule is half-followed; a
ledger with no card cannot get the rule in front of tomorrow's run.

**Only what is standing earns a card.** A one-off carded as a rule ("add a Status column") would be
retrieved into every future run forever, as though it were policy. `guidance_writer_for(manager)`
adapts a `GuidanceCardManager`-shaped store; with none wired, standing items are still tracked, they
just do not become retrievable cards.

## Wiring

On by default. The poller builds one ledger per lane and hands it to the update engine; the executor
folds each finished run's account into it. The store is a JSON file beside the lane's state
(`<state>_feedback.json`), written atomically, exactly like the watermarks: a lane needs no database
to remember that somebody is still waiting.

| field | default | what it does |
| --- | --- | --- |
| `feedback_ledger` | `True` | off records nothing; every channel behaves as it did before this existed |
| `feedback_ledger_path` | `None` | the store; defaults to beside the lane's state file |

Env equivalents: `QAR_FEEDBACK_LEDGER`, `QAR_FEEDBACK_LEDGER_PATH`.

### A consumer whose durable record is a database, not a file

`FeedbackLedger(store=...)` swaps the JSON file for anything satisfying `FeedbackStore` (`load()`
returning the same `{"items": {...}}` shape `_load` already parses, `save(payload)` persisting it):

```python
class MyDatabaseStore:
    def load(self) -> dict: ...       # everything recorded for this scope, or {} for nothing yet
    def save(self, payload: dict) -> None: ...   # replace this scope's record with `payload`

ledger = FeedbackLedger(store=MyDatabaseStore(...), requires_acceptance=True)
```

This exists for exactly one reason: a live, in-process product surface (not a lane, not a poller)
whose durable record already lives in its own database, where this ledger's rows have to sit next
to everything else the product keeps rather than in a file the product cannot query. Two ledger
instances sharing one store (two API workers, a live surface and a background lane) see each
other's writes on the very next read, the same guarantee the file store's flock-and-reload gives
two processes on one path -- a store's own write (a document replace, a transaction) is the
atomicity boundary, so `_exclusive()` skips the OS lock entirely when a store is given. A store that
raises degrades exactly like a missing file: empty on read, a logged and swallowed warning on
write, never an exception into a caller that never expected a ledger call to fail. `path` and
`store` are mutually exclusive; when both are given, `store` wins and `path` is never touched.

```bash
quest-ai-runner context <quest_id> --tracked      # what was asked, and where each one got to
```

## Tests

[`tests/test_feedback_ledger.py`](../tests/test_feedback_ledger.py): the vocabulary, the states, a
standing rule that survives a run writing "done" on it, the person's lock, the two-asks split, the
guidance bridge and its one-off exclusion, what the next run inherits, and the diary rule.

[`tests/test_feedback_ledger_store.py`](../tests/test_feedback_ledger_store.py): the `FeedbackStore`
seam -- a store round-trips items like a file would, two ledger instances sharing one store see
each other's writes (including a run's `done` landing as `awaiting acceptance` for one instance and
a person's `accept()` through the other closing it for both), a broken store degrades to empty
reads and swallowed writes, and `store` wins when both `path` and `store` are given.
