# The fast edit runner (and quest-ai-runner's write boundary)

`FastEditRunner` lands a bounded file edit in **one model call**, applied in process, instead of
spawning a full autonomous agent. It is the first rung of the deep-runner ladder, it is **off by
default**, and turning it on is the same act as granting this library write access to your files.

This page covers what it is, the opt-in write model, the containment and recovery guarantees, when
it is chosen versus escalating to the full deep runner, and the attribution for the vendored
SEARCH/REPLACE matcher.

---

## Why it exists

The reference deep worker (`core.goal_runner.SubprocessGoalRunner`) spawns Claude Code with
`claude -p`: a full agent, up to `--max-turns 30`, with an hour-long timeout. That is the right
tool for open-ended work and an absurd one for "fix the stale status line in that doc".

The deeper problem is not cost, it is **wasted work by construction**. By the time the brain
decides to execute, it has already assembled the relevant context: cards, targeted reads, the
conversation slice. Spawning a fresh agent throws all of that away and pays a second time for it to
be rediscovered from scratch. Measured against a one-line edit, the subprocess path costs roughly
6-10x the wall-clock time (a handful of sequential turns plus agent startup, versus a single round
trip) and about 10x the tokens.

`FastEditRunner` takes the other path: hand the model the context QAR already has plus the current
content of the candidate files, ask for the edit in one call, apply the result, return.

---

## The opt-in write model

Everything about this is gated on one thing: whether you handed the library a `FileWriter`.

```python
from quest_ai_runner.adapters import FilesWriter
from quest_ai_runner.config import RunnerConfig, build_orchestrator

cfg = RunnerConfig(
    retrieval=...,
    model_provider=...,
    corpus_root="/path/to/corpus",
    file_writer=FilesWriter("/path/to/corpus"),   # <- this line, and only this line
)
orch = build_orchestrator(cfg)
```

With that field left at its default `None`:

- no object in the wired brain can modify a file (every reference `RetrievalAdapter` is read-only);
- `resolve_deep_runner_ladder` returns a **one-rung** ladder holding exactly the deep runner you
  already had, and the goal loop treats a one-rung ladder exactly as it treated a single runner
  before ladders existed;
- behaviour is unchanged, byte for byte, from what shipped before this feature.

With it set, the ladder becomes `[FastEditRunner, SubprocessGoalRunner]`. Note what does **not**
change: `cfg.deep_runner` is still a single runner, because consumers and both chat UIs read that
field to decide whether execution is available. The ladder is the orchestrator's business.

There is no environment variable that turns this on, and no "auto" tri-state. A consumer either
constructed a writer or it did not.

---

## The write boundary

`FilesWriter` is the only component in the library that writes into a consumer's corpus. Everything
else that opens a file for writing writes QAR's own state (context cards, conversation archives,
bootstrap metadata).

**Containment.** Every path goes through `adapters.files_adapter.resolve_in_tree` — deliberately
the *same function* the read adapter resolves through, because two independent implementations of
one security boundary is itself the risk: they drift, and only one of them gets the fix. It joins a
relative path onto the root, `Path.resolve()`s it (which both normalizes `..` and **follows
symlinks**), and then tests containment with `resolved.relative_to(root)`, catching the `ValueError`
rather than comparing path strings. So all of these are refused, with the file outside the root
provably untouched:

| Attempt | Result |
|---|---|
| `../../../etc/passwd` | refused (traversal normalized before the test) |
| a symlinked *directory* inside the root pointing outside it | refused (resolution follows the link first) |
| a symlinked *file* inside the root pointing outside it | refused |
| an absolute path outside the root | refused |
| an absolute path inside the root | allowed |
| a path that does not exist yet | **allowed** — this is the ordinary create case |

**Secret refusal.** A write refuses exactly what a read already refuses: `.env*`, `*.key`, `*.pem`,
and anything whose name contains `secret` / `credential` / `password`. Being inside the root is not
enough to be writable.

**Recoverability.** Before an existing file is replaced, its current content is copied to a backup,
and a backup that was asked for but could not be written **refuses the overwrite** rather than
proceeding — silently proceeding would convert a recoverable edit into a destructive one.

Backups live *outside* the corpus, under `~/.quest-ai-runner/file-backups/` (override with
`QAR_FILE_BACKUP_DIR` or the `backup_dir=` argument), named `<epoch-ms>__<flattened-rel-path>`. Two
reasons they are not `.bak` files alongside the original: a backup inside the tree gets indexed as
corpus content and read back as if it were real, and it shows up as an untracked file in the
consumer's own version control.

**Why not just rely on git?** Because this library cannot know that a given corpus root is under
version control at all. A synced quest folder, a Drive mirror, or a plain documentation directory
frequently is not, and "git will save you" is not an assumption it is entitled to make on the
consumer's behalf. A consumer that *does* control a clean git tree can pass
`backups_enabled=False` and rely on it.

**Other refusals:** binary extensions, content over `max_write_bytes` (2 MB by default), and
creating a file at all when constructed with `allow_create=False`.

Every one of these is a `WriteResult(ok=False, error=...)`, never an exception. `ok=False` always
means the file on disk is unchanged.

---

## What the runner may edit

Only files that were **already in this turn's context**. Candidate paths are read out of the goal,
the brief, and the context preamble; each is then resolved through the writer's boundary and must
exist as a readable file under the size cap. That candidate set is the allow-list, enforced again
at apply time: an edit block naming anything else is refused, so the model cannot widen its own
blast radius.

If no candidate is found, the runner does nothing at all, spends no model call, and returns
`met=False`. Doing nothing is always available to it, and is the failure mode by design.

Note the shape of that gate: the text scan only *nominates*; the filesystem and the writer's
boundary *decide*. Nothing here gates a safety decision on words the model generated (CLAUDE.md
hard rule #3), and the failure direction is toward the more capable path, not toward acting.

---

## Wire format: chosen by file size

One call emits one format, so the choice is per call, decided by the largest candidate file.

**At or below `whole_file_max_lines` (400): whole-file rewrite.** The model returns the file's
complete new content and applying it is a `write_text`. That apply step *cannot fail to apply* —
there is no matching involved — which for a small documentation fix makes it strictly more reliable
than any matching scheme. The cost is output tokens proportional to file size, which is exactly why
it is bounded by line count. (400 lines is a few thousand output tokens: cents.)

**Above it: SEARCH/REPLACE blocks.**

````
path/to/file.py
```
<<<<<<< SEARCH
the exact existing lines
=======
the lines to replace them with
>>>>>>> REPLACE
```
````

This is the format every current frontier model defaults to, and it is **content-anchored, not
line-number-anchored** on purpose: models are famously unreliable about line numbers, and every
standalone unified-diff library for Python anchors on them. Aider's own ablation measured a **9x
increase in editing errors** when flexible, content-anchored matching was removed.

Applying a block runs a ladder: exact match, then a uniform-indent-drift match (the error models
actually make is a constant indent offset, applied consistently across both halves of the block),
then explicit `...` elision. A block that matches none of them leaves the file untouched and
produces a specific diagnostic: which lines are actually there ("Did you mean…?"), and whether the
replacement text is *already* present, which usually means the edit is already done. That
diagnostic feeds **one** in-process retry; past that, escalating is cheaper than arguing.

If several blocks target one file and any of them fails, the **whole file** is abandoned unwritten.
A partially applied chain is the one outcome worse than no edit at all.

---

## When it escalates, and how

It does not decide. The orchestrator's goal loop already ran an attempt, verified it against the
written done-standard with `_verify_goal`, and retried on failure — escalating up a **model**
ladder. The only change this feature made to that loop is that the **runner** is now resolved as an
ordered list and indexed by attempt with the same `min(index, len - 1)` shape the model ladder
already used:

```
attempt 1 -> ladder[0]   (FastEditRunner)
attempt 2 -> ladder[1]   (SubprocessGoalRunner)
attempt 3 -> ladder[1]   (the terminal rung repeats)
```

So a fast edit that is *insufficient* fails verification exactly the way a weak model's attempt
already did, and the next attempt goes to the full deep runner. Nothing new decides that.

One rule did need widening. "A hard failure with no output is terminal" was written when a goal
only ever had one runner, so "this runner produced nothing" and "nothing more can be tried" were
the same statement. On a ladder they are not: a first rung that declines the goal is precisely the
case the next rung exists for. That failure is now terminal only when there is no further rung.

Two routing decisions deliberately collapse the ladder to one rung, because prefixing them would
override a choice someone already made explicitly:

- a pinned `runner_override` (the queued deferred hand-off), and
- a runner selected by a consumer's `deep_runner_classifier`.

---

## Configuration

```python
from quest_ai_runner.adapters import FastEditConfig, FastEditRunner, FilesWriter

FilesWriter(
    root,
    backup_dir=None,           # default ~/.quest-ai-runner/file-backups, or QAR_FILE_BACKUP_DIR
    backups_enabled=True,      # False only if you have your own recovery story
    max_write_bytes=2_000_000,
    allow_create=True,         # False confines edits to files that already exist
)

FastEditConfig(
    whole_file_max_lines=400,  # at or below this, rewrite whole; above it, SEARCH/REPLACE
    max_target_files=4,        # a fast edit is meant to be bounded
    max_file_bytes=400_000,    # skip candidates larger than this
    max_total_bytes=600_000,   # total file content in one prompt
    tier="quality",            # used only when the orchestrator does not pin a model
    max_retries=1,             # in-process retries fed the match diagnostic
)
```

`tier` is deliberately **not** the cheapest tier. What makes this path cheap is the architecture
(one call instead of a 30-turn agent), not a weak model, and this call decides a real file write.
When the orchestrator pins a model for the attempt (the deep model ladder), that wins.

To wire a non-default config, construct the runner yourself and put it in the ladder via
`RunnerConfig.deep_runner` plus your own composition, or subclass/replace
`config.resolve_fast_edit_runner`.

---

## Vendored code and attribution

The SEARCH/REPLACE parsing and matching in `quest_ai_runner/vendor/aider_editblock.py` is a
**modified copy** of code from [Aider](https://github.com/Aider-AI/aider), Apache-2.0, taken from
`aider/coders/editblock_coder.py` and `aider/coders/wholefile_coder.py` at commit `5dc9490b`
(2026-05-22) and vendored on 2026-08-13. quest-ai-runner is also Apache-2.0, so there is no license
conflict; the attribution is recorded in this repo's `NOTICE` and in that file's header, which also
lists every modification (Apache-2.0 §4(b)).

It is vendored rather than depended on because Aider has never published that layer as a package,
and vendored rather than reimplemented because nearly every line of it absorbs a specific way real
models get the format wrong: the markers are matched with `{5,9}`-repeat regexes because models
miscount `<` and `=` characters; the filename is recovered by walking back up to three lines through
fences because models put it in the wrong place. An approximation would quietly lose exactly that.

The four documented changes from upstream, in short: all Aider runtime coupling removed (the file
is stdlib-only); `replace_closest_edit_distance` dropped because it is unreachable dead code
upstream and copying it would imply a fuzzy rung that does not actually exist; `do_replace` made
pure and renamed `apply_edit`, since in this repo every write goes through the `FileWriter`; and a
whole-file parser ported from `WholeFileCoder.get_edits`.

---

## Tests

- `tests/test_file_write_containment.py` — the security boundary, one case per way out of the root.
- `tests/test_fast_edit_runner.py` — both wire formats, the indent near-miss, a genuine no-match
  that must leave the file byte-for-byte unchanged, the allow-list, and declining.
- `tests/test_fast_edit_ladder.py` — the opt-in wiring and escalation driven through the real
  goal loop.
