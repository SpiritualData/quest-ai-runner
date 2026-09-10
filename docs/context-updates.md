# Automated context updates

*What has changed since an assistant last looked at this work?* One engine answers that question
for every channel, and every caller asks the same engine.

Module: [`runner/context_updates.py`](../quest_ai_runner/runner/context_updates.py).
Drive channel: [`adapters/drive_comments.py`](../quest_ai_runner/adapters/drive_comments.py).

## The problem it exists to fix

Every context channel in this library was built the same way and then wired by hand, twice.
`runner.reflections` reads what the person wrote, `runner.insights` reads what they captured,
`executor._build_context_view` reads a quest's notes and run history, and `AutopilotPass` read two
of those again with their own caching and their own slot in the composed brief. Each was a good
module. Together they meant that adding a THIRD channel was not "register a source", it was "write
a module, write a fetch method on two callers, write a render function, add a parameter to the
composer, and remember to tell the run to look at it".

So people did the last part in the prompt instead. A quest's standing instructions grew lines like
"check the comments on the doc" and "look at my insights tagged X": a person hand-maintaining, in
prose, the retrieval plan the context engine was supposed to own.

## The shape

- A **`ContextSource`** answers one question: given a card and a watermark, what has arrived since?
- An **`UpdateEngine`** holds the registry, resolves which sources a given card uses **from data the
  card carries**, runs them, and returns a **`ContextUpdates`** bundle.
- A **`Watermarks`** file holds one "last looked" stamp per *(card, source)* pair, so channels that
  move at different speeds stay independent.

Built in: `reflections`, `insights`, `quest_notes`, `drive_comments`, `drive_changes`. A consumer
adds its own with `engine.register(MySource())`, and every caller of the engine sees it.

## What a card watches, as data

A quest says what it watches in its own `autopilot.context_sources` list. No code names a source for
any particular quest:

```json
{
  "autopilot": {
    "mode": "act",
    "context_sources": [
      "quest_notes",
      {"source": "insights", "categories": ["PhD"]},
      {"source": "drive_comments", "folder_id": "1kEc..."},
      {"source": "drive_changes", "folder_id": "1kEc..."}
    ]
  }
}
```

A bare string is shorthand for `{"source": "<name>"}`. `UpdateEngine.describe_sources()` returns the
vocabulary, so an assistant writing a spec can discover the names instead of guessing one that
silently does nothing. An unknown name is reported as a gap in the bundle, never raised.

**Reaching the documents: `folder_id`, `owner`, or `file_ids`.** A folder listing only reaches files
whose *folder* the credential is on, and that is often not how an assistant's documents are shared:
they are created by the assistant's account and filed into the person's folder, so the credential
ends up on each *document* and not on the folder around them. The folder query then returns nothing
while every document is readable. `{"source": "drive_comments", "owner": "assistant@example.org"}`
is the query that works there, and unlike a `file_ids` list it keeps working as new documents are
created. The three may be combined; a file reached by more than one route is reported once.

**A narrowing spec never drops anything.** `{"source": "insights", "categories": ["PhD"]}` PROMOTES
matching captures into this card's own refs; the full unfiltered capture block still flows exactly
as before. The person's tag steers attention, it never gates delivery. A fixed string rule that
gated delivery would silently lose every capture whose wording it did not anticipate, which is what
hard rule #3 in `CLAUDE.md` forbids.

## The receipt

Surfacing context is only half the loop. A person who leaves a comment has no way to know whether
the run that followed used it, and "I read your note" buried in three paragraphs is not an answer.

So every offered update carries a short stable ref (`[U1]`), the composed block asks the run to
close with one line per ref, and the executor turns the run's OWN answer into a fixed block on the
result:

```
Context updates taken into account:
[U1] 2026-09-08 · comment · Chapter two · needs an answer -> answered in the doc
[U2] 2026-09-09 · note · this quest -> not used
```

Every offered ref appears whether or not the run mentioned it, and a ref the run said nothing about
reads as `no note from the run`. That is deliberately not a second model call grading the first: the
run that used the material is the only thing that knows how it used it.

The manifest is parsed back out of the task's own text (`parse_manifest`), which is what lets the
receipt be rendered by whoever finishes the run without a bundle object travelling through the
queue.

## Wiring

On by default. The poller builds one engine per lane and hands it to the autopilot pass:

```python
engine = build_update_engine(cfg, quest_client, state_path="qar_state.json")
bundle = engine.collect(quest, card_id=quest_id)
text = bundle.as_prompt_block()
...        # hand `text` to the run
bundle.mark_seen()
```

`RunnerConfig` fields, all optional:

| field | default | what it does |
| --- | --- | --- |
| `context_updates` | `True` | off composes exactly what was composed before this existed |
| `context_updates_state_path` | `None` | watermarks file; defaults to beside the lane's own state file |
| `context_updates_first_look_days` | `14` | how far back a source looks for a card nothing has ever read |
| `drive_comments` | `None` | a `DriveComments` client; without it the two Drive sources contribute nothing |

Env equivalents: `QAR_CONTEXT_UPDATES`, `QAR_CONTEXT_UPDATES_STATE_PATH`,
`QAR_CONTEXT_UPDATES_FIRST_LOOK_DAYS`.

The Drive channel needs a token, and minting one is a deployment concern:

```python
from quest_ai_runner.adapters import DriveComments, service_account_token_provider
cfg.drive_comments = DriveComments(token_provider=service_account_token_provider(...))
```

Reading needs `drive.readonly`; posting a reply needs a write scope (`COMMENT_WRITE_SCOPES`), and a
read-scoped token gets a clean error rather than a silent no-op.

## The rules that hold it together

- **A watermark moves only when the material was delivered.** Never at collection time, never on a
  dry run, and only for the sources that were actually readable, because one API blip must not consume a
  person's comment on the way past.
- **Everything is best-effort except a write.** A source that raises is reported as a failed source
  and the rest of the bundle is delivered. An empty bundle means "nothing new", never a failure.
  `DriveComments.reply()` returns a result object, because an assistant that believes it answered
  somebody when it did not is worse than one that knows it failed.
- **Open threads keep their ref.** The Drive comments source fetches open threads without a time
  filter: a watermark answers "what is new", and an unanswered question is not news after the first
  day but is still unanswered.
- **A comment carries the passage it is anchored to.** Comments are written as deixis ("this is
  unclear", "cite here"), so the quote IS the subject.
- **The cap never drops what somebody is waiting on.** A bundle holds at most 20 updates, newest
  first, and things marked as needing an answer are never the ones evicted.

## Tests

- [`tests/test_context_updates.py`](../tests/test_context_updates.py): the engine, the sources, the
  watermark rules, the receipt.
- [`tests/test_autopilot_context_updates.py`](../tests/test_autopilot_context_updates.py): the pass,
  the executor receipt, the poller/config wiring, and the byte-identical no-engine path.
- [`tests/test_drive_comments.py`](../tests/test_drive_comments.py): the Drive channel, offline.


## Relevance: the engine's job, not the run's

Captures are user-scoped: they arrive from a space covering the person's whole life, so most of
them belong to some other piece of work. Delivered unfiltered, the RUN ends up doing the filtering
out loud, in the output the person reads:

> Passed over: the 9/10 Cornerstone capture (collaboration tracking) isn't this quest's domain.

That line is the context engine's work showing up as the assistant's chatter. So the engine judges
first (`llm_relevance_judge`, on by default, `"balanced"` tier).

**It is a model judgment, never a tag match against the card's name** -- that is hard rule #3, and
the reason `runner/insights.py` refuses to do it in code. The judge is given the card's real
subject matter (name, outcome, description, current state, standing brief) and each capture *with
the person's own tags*, because a one-line outcome names a destination, not a topic. Verified
live: against the outcome alone, a capture the person had tagged for this very work was dropped
from it.

**Card-scoped channels are never judged.** A note on this quest, a comment on a document this card
owns, a collection the card named -- all relevant because of *where* they were written. Judging
them could only ever lose one.

**Every failure keeps everything.** No provider, a timeout, unparsable JSON: the bundle is
delivered exactly as collected. The worst case has to be a noisier brief, never a silently emptier
one.

## Open until answered

A person's note and a document comment stay open until an assistant *answers* them, not until a
watermark passes them. The watermark only labels which are new. Two live failures drove this: a
first look offered ten notes answered days earlier, all marked "needs an answer"; and time
filtering lost an open question for good the moment one pass saw it and did nothing. Bounded at
both ends by `OPEN_ITEM_MAX_AGE_DAYS` and `MAX_OPEN_PER_SOURCE`, so "open" cannot become a backlog.
