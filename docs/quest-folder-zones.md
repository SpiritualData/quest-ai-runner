# Quest folder zones: keeping "who decided this?" answerable

A quest with a local working folder ends up holding two kinds of material: what the person said,
and what the AI came up with. Both are markdown. Both are confident. Nothing on the page records
which is which.

The failure that follows is not dramatic, which is why it survives so long:

1. A run analyses the work and produces something reasonable (a gap list, a risk register, a
   proposed re-sequencing).
2. The next run reads that file. It is in the folder, it is specific, it is clearly the product of
   real work. The run treats it as the brief.
3. The run after that builds on the resolutions.
4. Ten runs later the plan rests on a premise the person never agreed to. When they finally read
   it, they say, correctly, "all of that is from AI, you should be confirming every point with
   me."

Nothing malfunctioned. The record simply never distinguished a proposal from an instruction.

## A short map, with the detail in the files it governs

`ensure_folder_zones(folder, quest_id=None, quest_name=None)` runs on every pull. The folder's
always-loaded `CLAUDE.md` gets a short managed block (about a dozen lines): an opening line naming
the quest when the caller knows it (the pull passes the quest id and its outcome as the name; with
no id the line is omitted), then `## What's here`, one bullet per item with what it is plus its
single rule or a "read when" clause:

| Item | Rule in the map |
| --- | --- |
| `QUEST_SYNC.md` | the quest's live state; `QAR:MANAGED` blocks are overwritten on every pull; add bullets under "Notes to push to Quest" to send findings up; the `quest-ai-runner sync-quest-folder <quest_id> . --direction both` command |
| `ESOI_SPEC.md` | the quest's ESOI specification (problems, ideals, solutions, implementations, metrics), one section per record headed `### S: <name> (<recordId>)`; written by the pull inside a `QAR:MANAGED:spec` block, your text outside it survives; `sync-quest-folder --no-spec` skips it; a pull that renders it byte-identical does not touch the file, and a poll with no un-synced bullet and no edited goal block sends nothing to Quest |
| `human_context/` | the person's own words, verbatim only; their Quest notes arrive under `human_context/from_quest/` |
| `ai_driven/` | the AI's workspace; everything is a proposal until the ledger says otherwise |
| `ai_driven/source_of_truth/` | the current answer per topic, updated in place; read its README before researching |
| `ai_driven/provenance_ledger.md` | read before building on anything an AI proposed; only `approved` rows are settled |
| everything else | joint work product; say what you changed and why |

The detail lives in the file each rule governs, each under its own managed markers so a re-render
updates it in place and never touches human or row content:

- the status table and the add-a-row / approve-only-with-a-quote rules: a `ledger_rules` block at
  the top of `ai_driven/provenance_ledger.md`. An existing ledger gets the block inserted under its
  title line (or replaced if present); every row and any prose below stays byte for byte.
- the source-of-truth rules: a `source_of_truth_rules` block in
  `ai_driven/source_of_truth/README.md` (created if absent; only the block is replaced if the file
  exists).

None of this is enforced by the filesystem, and it is not meant to be. A run can always write
anywhere. What the module guarantees is that the rule is **scaffolded** (the directories exist, so
where to put a file is a real choice), **stated** (the map, the governed files and the run
context), and **pre-populated** with the one class of human material a run should never have to
transcribe by hand. The wording of each rule exists once, in constants in
`runner/quest_folder_zones.py`; the map, the ledger header, the README block and the run-context
contract all derive from them.

## Capturing their words

Every pull runs `capture_human_input(folder, notes)`, which writes each human-authored Quest note
into `human_context/from_quest/` verbatim, with no wrapping, no summary line, no markdown
normalisation. A file in the human zone whose text has been improved is no longer evidence of
anything.

Two decisions in that function are load-bearing:

- **Idempotent by note id.** The filename derives from the note's id, so calling it on every
  single pull rewrites nothing.
- **An unknown author is never human.** `is_human_note` requires either an explicit human
  `author_kind` or a `source` only a person can post through (mail, sms). An absent or
  unrecognised kind falls to non-human on purpose. A missed capture is recoverable: the note is
  still in Quest and still in the sync file. An AI note filed as the person's words is not, and it
  is unfalsifiable afterwards, because it reads as a quote.

Note that `author_name` is never consulted: a note an AI run posts carries the account owner's
display name, because the API key is theirs.

## Source of truth documents

A different failure: nobody consolidates. A run researches a topic and writes a dated file; a later
run researches it again and writes another, until the working answer to "what does X say" is spread
across a dozen files, none canonical. `ai_driven/source_of_truth/` is the current answer per topic,
refined in place (no date in the filename, check here before researching, named rather than
numbered headings). Its README carries those rules. It is a nested convention inside `ai_driven/`,
not a fourth top-level zone.

## The ledger

`ai_driven/provenance_ledger.md` answers the question the zones alone cannot. A folder can tell you
a file is AI-authored. Only a ledger can tell you the person *read* it, when, and what they said.
Its managed header holds the statuses (`ai_proposed`, `surfaced`, `approved`, `rejected`,
`superseded`, each with "may you build on it?") and the rules: add a row when you propose something
they did not ask for, and move it to `approved` only when you can quote them, with where they said
it. It is scaffolded empty-but-headed, because a pre-filled row would be the library inventing a
decision.

## Configuration

```python
RunnerConfig(
    quest_folder_map={"quest_abc123": "/path/to/folder"},
    quest_folder_sync_direction="both",
    quest_folder_zones=True,   # default
)
```

`quest_folder_zones=False` turns off the scaffold, the capture, and the run-context contract, for a
consumer whose folders are organised some other way. `pull_quest_to_folder(..., zones=False)` does
the same for one call.

The run-context contract is gated on the folder actually being **mapped**, not merely on the
feature being on: a run with no folder, told to check a ledger, goes looking for a file that does
not exist, and a run that cannot find what the prompt promised starts inventing a substitute.

## What a run is told

Short, and built from the same wording as the files above. The prompt's job is to make the run
look at the folder's map and the ledger, and to name the one failure worth interrupting a run over:
building on an `ai_proposed` or `surfaced` row as if it were the brief.
