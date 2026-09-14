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

Built in: `reflections`, `insights`, `quest_notes`, `goal_updates`, `drive_comments`,
`drive_changes`, `inbound_mail`. A consumer adds its own with `engine.register(MySource())`, and
every caller of the engine sees it.

`goal_updates` is the per-goal check-in thread: what the person wrote on ONE goal of this quest
(what they read, did or found), as opposed to `quest_notes`, which is what they wrote to the
quest as a whole. It is always on, like `reflections` and `insights`, because it is card-scoped
by construction: an update lives on a goal of this quest, so it cannot carry another card's
material and needs no relevance judgment. It does not track asks; a check-in is somebody saying
where things stand, not a question owed an answer.

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
      {"source": "drive_changes", "folder_id": "1kEc..."},
      {"source": "inbound_mail", "mailbox": "support@example.org"}
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
created. The three may be combined; a file reached by more than one route is read once.

**`owner` takes one address or a list of them**, because one person is several Google accounts: a
work domain, an old personal address, a second one some document happened to be created under.
Which address owns which document is not something anybody keeps track of, so a spec that could
name only one made every document under the others invisible.

**`{"source": "inbound_mail", "mailbox": "support@example.org"}`** reads what arrived at a
mailbox since the watermark, one row per message, needing an `InboundMail` client supplied once by
the consumer (`UpdateEngine(inbound_mail=...)`; declaratively, `RunnerConfig.inbound_mail_auth`,
shaped like `drive_comments_auth`: a service-account file, the subject to impersonate, and the
`allowed_addresses` this credential may actually read -- domain-wide delegation can otherwise
impersonate any mailbox in the domain, so that list is the only code-side limit on which one gets
read). It can never be pointed at a quest's own reply mailbox (`ai@...`, `ai+q-...@...`): that
address is already polled by a consumer's own inbound-reply service on its own schedule, and a
second poller on the same mailbox ingests every reply twice. A card naming it gets a reported
source error instead, never a silent double-ingest (`adapters.inbound_mail.refuses_as_inbound_mailbox`).

**Every capture is its own row, and a tag never gates delivery.** The captures arrive one update
each, each with its own ref, so the relevance judge decides on each one and the receipt answers
for each one in the person's own words. `{"source": "insights", "categories": ["PhD"]}` FLAGS the
captures the person tagged that way as waiting on an answer ("is this one yours?"); the others are
delivered all the same. A fixed string rule that gated delivery would silently lose every capture
whose wording it did not anticipate, which is what hard rule #3 in `CLAUDE.md` forbids. The only
thing that sets a capture aside is the judge (below), and any failure of the judge keeps everything.

## The receipt

Surfacing context is only half the loop. A person who leaves a comment has no way to know whether
the run that followed used it, and "I read your note" buried in three paragraphs is not an answer.

So every offered update carries a short stable ref (`[U1]`), the composed block asks the run to
close with one line per ref, and the executor turns the run's OWN answer into a fixed block on the
result:

```
Context updates taken into account:
[U1] 2026-09-09 · reflection · Quest reflections -> steered the day's focus
[U2] 2026-09-09 · capture · Quest insights · "idea for the construct weighting" · needs an answer -> folded into the method plan
[U3] 2026-09-08 · comment · Chapter two · "cite this" · needs an answer -> answered in the doc
[U4] 2026-09-09 · note · Dissertation · "do the survey lineage first" · needs an answer -> done first, as asked
```

Every offered ref appears whether or not the run mentioned it, and a ref the run said nothing about
reads as `no note from the run`. That is deliberately not a second model call grading the first: the
run that used the material is the only thing that knows how it used it.

The manifest is parsed back out of the task's own text (`parse_manifest`), which is what lets the
receipt be rendered by whoever finishes the run without a bundle object travelling through the
queue.

## Wiring

On by default. The poller builds one engine per lane and hands it to BOTH the autopilot pass and
the task executor, so a batch the pass composes and a task somebody delegated from chat are handed
the same material: the block goes into the batch text in the first case and into the task's
context view in the second (a batch whose text already carries a block is not collected again),
the watermark moves once the run has had it, and the result carries the receipt either way.

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
| `context_updates_judge_admission` | `True` | off skips the admission judgment; whatever cleared a source's own deterministic filter (e.g. `is_bulk_mail`) reaches a run and the asks record unfiltered |
| `context_updates_admission_tier` | `"fast"` | model tier for the admission judgment (see below) |
| `context_updates_relay_log_path` | `None` | "already relayed onto Quest" record; defaults to beside the lane's own state file |
| `drive_comments` | `None` | a `DriveComments` client; without it the two Drive sources contribute nothing |
| `inbound_mail` | `None` | an `InboundMail` client; without it `inbound_mail` contributes nothing |
| `inbound_mail_auth` | `None` | declarative form: `{service_account_file, subject, allowed_addresses, scopes}`, resolved into `inbound_mail` by `resolve_config_objects` |

Env equivalents: `QAR_CONTEXT_UPDATES`, `QAR_CONTEXT_UPDATES_STATE_PATH`,
`QAR_CONTEXT_UPDATES_FIRST_LOOK_DAYS`. The rest are TOML-file fields only (no env var), the same
convention `context_updates_judge_relevance`/`context_updates_relevance_tier` already follow.

The Drive channel needs a token, and minting one is a deployment concern:

```python
from quest_ai_runner.adapters import DriveComments, service_account_token_provider
cfg.drive_comments = DriveComments(token_provider=service_account_token_provider(...))
```

Reading needs `drive.readonly`; posting a reply needs a write scope (`COMMENT_WRITE_SCOPES`), and a
read-scoped token gets a clean error rather than a silent no-op.

## Where an answer goes

An item is only answered where the person actually reads. A quest with mail switched on sends the
run's **result** to its people with a per-quest reply address, and their replies come back as notes:
that round trip is the conversation, and a note nobody opens is not part of it. So each item's
`how_to_respond` names the channel that reaches ITS reader:

| the item | where the answer goes |
| --- | --- |
| a note, on a quest that mails | the run's result, which is what gets mailed; the note it also keeps is the record |
| a note, on a quest that does not mail | a note on the quest |
| a document comment | a reply on that comment thread, in the document |

**And the same two places count as an answer.** A person's note stops being offered once an
assistant note follows it on the quest **or** a run on that quest delivered its result after it
(`DELIVERED_TASK_STATUSES`, and only a run that actually produced a result). Before that second
half existed, every answer that went out by mail left the note it answered looking untouched, and
the person was asked the same question the next morning, and the morning after.

What a later run can see of its own past answers, which is what makes this work: the result on the
task row (`_fetch_run_history`), the same text rolled onto the autopilot pass that created it, and
the previous period's finished tasks (`_previous_period_summary`). Notes remain the assistant's
durable record on the quest; they are simply no longer the only evidence that somebody was answered.

## Saying what was read

A source reports what it LOOKED at, not only what it offered: `SourceReport.considered` and
`SourceReport.explanation`, set by the source through `CollectRequest.account(...)`. This is not
bookkeeping. "0 found" and "two questions, both already answered in the document" are the same
line to a reader, and the first reading sends somebody hunting a permissions bug that is not there,
which is exactly what happened on 2026-09-12. The channel now says which:

```
drive_comments   0 found (2 thread(s) across 8 document(s), 2 already answered in the document)
drive_comments   0 found (no comments on the 8 document(s) read)
quest_notes      0 found (6 note(s), all already answered)
```

## Looking: `quest-ai-runner context <quest_id>`

Everything above is about context being *delivered* to a run. Asking what a quest's context IS, right
now, is a separate operation, and it is one call:

```bash
quest-ai-runner context quest_1625d9f47a06 --config qar.toml
```

```python
from quest_ai_runner import load_config
from quest_ai_runner.runner.context_updates import collect_quest_context

bundle = collect_quest_context("quest_1625d9f47a06", cfg=load_config("qar.toml"))
print(bundle.as_prompt_block())     # exactly the text a run on this quest would be handed
```

It exists because, before it, the only ways to see a quest's context were to run the thing that
consumes it (an autopilot pass, an executor task) or to rebuild the engine's wiring by hand outside
the library. The first has side effects and a cadence gate that makes it unavailable on the day
somebody most wants to look; the second is a copy of library code living where it cannot be kept in
step. Neither is a thing to hand somebody who wants to look at their own context.

**It is a read, structurally.** The engine is built on a `Watermarks` store constructed with
`read_only=True`, which cannot move a stamp whichever method is called on it, `mark_seen()`
included. So the command has no effect on what the next real run is offered, and running it ten
times is the same as running it once. That is a property of the object rather than a rule about
which method to avoid, because "safe if you don't call the wrong thing" is not something the person
reading the command can verify.

| what you want | how |
| --- | --- |
| the whole picture, as a person reads it | `context <quest_id>` |
| just the block a run receives | `context <quest_id> --block` |
| a report, a dashboard, a test | `context <quest_id> --json` (`ContextUpdates.as_dict()`) |
| a time period instead of "since last delivered" | `--days 7`, or `--since 2026-09-01T06:00:00Z` |
| one channel only | `--source drive_comments` (repeatable) |
| what a card is allowed to name | `context <quest_id> --sources` |

The `--source` filter is how "is the Drive channel actually reaching these documents?" gets a
straight answer: one channel, a window wide enough to contain something, and a per-source line
saying found, set aside, or the error. The same narrowing is `sources=[...]` on `UpdateEngine.collect`.

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
- [`tests/test_inbound_mail.py`](../tests/test_inbound_mail.py): the header pre-filter, the
  admission judge (and its failure mode), idempotent relay, the watermark, and the reply-mailbox
  refusal -- all offline.


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

## Admission: a different question from relevance, never conflated with it

Relevance asks *does this bear on this card*. Admission asks a question that comes BEFORE that
one even applies: *is a real person writing to us at all*, as opposed to bulk or automated mail
that cleared a deterministic header check. The two are kept structurally separate -- a different
class attribute (`judge_admission`, alongside `judge_relevance`, on `_BaseSource`), a different
judge callable (`llm_admission_judge`, alongside `llm_relevance_judge`), a different engine method
(`UpdateEngine._apply_admission`, run before `_apply_relevance`) -- so a source can want one, the
other, both, or neither, and so judging "is this spam" is never accidentally done by asking whether
the mail happens to mention this quest's subject matter (a marketing email addressed to a real
person's real interests can do that by accident; a genuine one-line request can fail to by never
restating what it's about).

`inbound_mail` is the first source to use it, and it is a two-step gate:

1. **A deterministic header check, for free, before any model call**
   (`adapters.inbound_mail.is_bulk_mail`): `List-Unsubscribe`, `List-Id`, `Precedence: bulk`,
   `Auto-Submitted` and the other headers a mail SYSTEM sets about its own automated nature.
   Header checks only -- never a keyword or subject-line rule, which is hard rule #3 exactly as it
   is for the relevance judge: a fixed string list silently misses whatever wording it did not
   anticipate. Mail caught here never becomes a `ContextUpdate` at all, so it is provably
   impossible for it to have cost a model call.
2. **The admission judgment** (`llm_admission_judge`, on by default, `"fast"` tier -- cheaper than
   relevance's `"balanced"`, because the header check already narrowed what reaches it), over
   whatever survived step 1.

**Biased toward inclusion, exactly like the relevance judge, and for a sharper reason.** Any
failure (no provider, a timeout, unparsable JSON) keeps everything, because the costs are not
symmetric: an admitted item that turns out to be nothing costs one line somebody dismisses reading
their asks; a real person's request that is dropped here is invisible, and nobody -- not the
sender, not the account -- will ever know it happened.

## Relaying an admitted item onto the quest

Surfacing an admitted mail item to a run is only half the point: the asks ledger the Quest app
reads ("What You Asked") is backed by quest-backend's own Mongo store, not this library's default
JSON file, so an item a real person sent in has to reach the ACCOUNT's own record, not just one
run's prompt. `ContextUpdate.relay_to_quest` (a class attribute, alongside `judge_admission`, so
any source can opt in) marks a source's admitted items for this; `inbound_mail` is the first.

`ContextUpdates.relay_admitted` posts each one through
`QuestClient.add_quest_note(quest_id, text, relayed_author_email=sender_address)` -- the one write
path that lets an API-key caller attribute a note to a real person's own address rather than to the
AI. The note still records `author_kind: "ai"` (an API key did post it), but `author_name` becomes
`"<email> (relayed)"` and, critically, the note enters the account's asks record under that
address, exactly as an emailed reply to a quest's own mailbox already does.

**Only at delivery, never at collection.** `relay_admitted` runs from `mark_seen`, the same moment
the watermark moves and the local feedback ledger records an ask -- which is what keeps
`quest-ai-runner context <quest_id>` (built on a read-only engine that never calls `mark_seen`) a
genuine read: looking at a quest's context can never itself post a note onto it.

**Idempotent, keyed by (card_id, source, item_id)** -- the same triple the feedback ledger keys its
own rows by -- via `RelayedItems`, a small JSON-backed store beside the watermarks
(`context_updates_relay_log_path`, defaulting next to the lane's state file). A relay is a WRITE
with a real person's name on it, so unlike a watermark, where a re-offered line only costs a
person's patience, losing this record on a restart would turn the same mail into a second note. A
failed relay is logged and simply retried on the next pass (the log only records success).

## Open until answered

A person's note and a document comment stay open until an assistant *answers* them, not until a
watermark passes them. The watermark only labels which are new. Two live failures drove this: a
first look offered ten notes answered days earlier, all marked "needs an answer"; and time
filtering lost an open question for good the moment one pass saw it and did nothing. Both channels
are bounded at both ends by `OPEN_ITEM_MAX_AGE_DAYS` and `MAX_OPEN_PER_SOURCE`, so "open" cannot
become a backlog.

A note newer than the watermark is offered even when an assistant note follows it, once and
unflagged. The watermark moves only when a run was handed the notes, so "newer than it" means no run
has seen it: a run that started before the note arrived and posted its summary an hour later never
read it, and treating that summary as the answer would lose the note for good. Not on a first look,
where "newer than the watermark" is just "recent".

The engine's user-scoped reads (the reflection, the captures) are cached for `CACHE_TTL_SECONDS`
so one pass over every quest reads them once; the poller keeps one engine for its whole life, so
without the expiry a task in the afternoon saw the captures as they stood at six in the morning.
