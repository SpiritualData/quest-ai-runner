# Quest folder zones — keeping "who decided this?" answerable

A quest with a local working folder ends up holding two kinds of material: what the person said,
and what the AI came up with. Both are markdown. Both are confident. Nothing on the page records
which is which.

The failure that follows is not dramatic, which is why it survives so long:

1. A run analyses the work and produces something reasonable — a gap list, a risk register, a
   proposed re-sequencing.
2. The next run reads that file. It is in the folder, it is specific, it is clearly the product of
   real work. The run treats it as the brief.
3. The run after that builds on the resolutions.
4. Ten runs later the plan rests on a premise the person never agreed to. When they finally read
   it, they say — correctly — "all of that is from AI, you should be confirming every point with
   me."

Nothing malfunctioned. The record simply never distinguished a proposal from an instruction.

## The three zones

`ensure_folder_zones(folder)` scaffolds them on every pull and writes the rule into the folder's
own `CLAUDE.md` as a managed section, so it reaches any process that opens the folder — including
ones that never went through this library.

| Zone | Holds | Who may write |
| --- | --- | --- |
| `human_context/` | The person's own words and materials | AI may write **only** to record human input verbatim, or when explicitly asked |
| `ai_driven/` | AI self-documentation, analyses it chose to run, scratch, designs nobody asked for | AI writes freely; everything here is a **proposal** |
| everything else | The collaborative work product — the document, the code, the plan | Both, jointly |

None of this is enforced by the filesystem, and it is not meant to be. A run can always write
anywhere. What the module guarantees is that the rule is **scaffolded** (the directories exist, so
where to put a file is a real choice), **stated** (in the folder's `CLAUDE.md` and in the run
context), and **pre-populated** with the one class of human material a run should never have to
transcribe by hand.

## Capturing their words

Every pull runs `capture_human_input(folder, notes)`, which writes each human-authored Quest note
into `human_context/from_quest/` verbatim — no wrapping, no summary line, no markdown
normalisation. A file in the human zone whose text has been improved is no longer evidence of
anything.

Two decisions in that function are load-bearing:

- **Idempotent by note id.** The filename derives from the note's id, so calling it on every
  single pull rewrites nothing.
- **An unknown author is never human.** `is_human_note` requires either an explicit human
  `author_kind` or a `source` only a person can post through (mail, sms). An absent or
  unrecognised kind falls to non-human on purpose. A missed capture is recoverable — the note is
  still in Quest and still in the sync file. An AI note filed as the person's words is not, and it
  is unfalsifiable afterwards, because it reads as a quote.

Note that `author_name` is never consulted: a note an AI run posts carries the account owner's
display name, because the API key is theirs.

## The ledger

`ai_driven/provenance_ledger.md` answers the question the zones alone cannot. A folder can tell you
a file is AI-authored. Only a ledger can tell you the person *read* it, when, and what they said.

| Status | Meaning | Safe to build on? |
| --- | --- | --- |
| `ai_proposed` | An AI run came up with it. They have not seen it. | No — surface it and ask |
| `surfaced` | Put in front of them. No answer yet. | No — silence is not agreement |
| `approved` | They asked for it or said go, quoted in the row | Yes |
| `rejected` | They said no | No |
| `superseded` | Overtaken by a later decision | No — follow the row that replaced it |

It is scaffolded empty-but-headed. A pre-filled row would be the library inventing a decision,
which is the thing this exists to prevent.

Runs are told to move a row to `approved` **only when they can quote the person**, with where they
said it. Their own confidence is not evidence, and neither is silence.

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
not exist — and a run that cannot find what the prompt promised starts inventing a substitute.

## What a run is told

Short, because the full rules are in the folder's `CLAUDE.md` and the run reads them when it gets
there. The prompt's job is to make it look, and to name the one failure worth interrupting a run
over:

> Before you build on any analysis, gap list, plan or initiative that came from an AI run, check
> `ai_driven/provenance_ledger.md` for its status. Only `approved` rows are settled, and an
> approved row quotes the person saying so. If what you are about to build on is `ai_proposed` or
> `surfaced`, it is a suggestion they have not agreed to: say so in your result and ask, rather
> than treating it as the brief.
