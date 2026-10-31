# Corpus playbooks: feeding distilled judgment to the runner

A consumer's corpus usually contains far more history than any model can load: git scar tissue,
reverted approaches, architecture contracts, debugging playbooks. The cheapest way to make every
run smarter is to distill that history once into a small set of **playbook files** and let the
runner's two knowledge channels pick them up. This costs nothing at runtime and upgrades both the
shallow loop and deep runs.

## The two channels

1. **Shallow loop (context cards).** `FileContextStore` bootstraps cards from any markdown in the
   corpus root with no LLM call: the first heading and first paragraph become the card summary,
   and IDF-weighted keyword overlap surfaces the card when a task's text matches. So a playbook
   whose first paragraph names the component it covers ("Load before changing code in `<repo>`:
   architecture contract, debugging triage, known dead ends") is automatically retrievable by the
   orchestrator. Keep playbooks in non-hidden paths if you want this channel; re-run `bootstrap`
   after adding them.

2. **Deep runs (Claude Code).** `SubprocessGoalRunner` spawns Claude Code with
   `cwd = QAR_DEEP_WORKING_DIR`, so the worker inherits whatever project memory that directory
   provides (`CLAUDE.md`, `.claude/skills/`). Playbooks written as Claude Code skills
   (`.claude/skills/<name>/SKILL.md` with a `description` that states when to load them and when
   NOT to) are offered to every deep worker automatically. A pointer section in the working
   directory's `CLAUDE.md` makes them discoverable even to models that don't browse the skill
   list.

Use both: one file can serve both channels if it lives under the deep working dir and its first
paragraph doubles as retrieval bait, or you can keep skills in `.claude/skills/` and add a plain
markdown index inside the corpus for the card channel.

## Authoring rules that keep playbooks load-bearing

- **One playbook per component**, distilled, not exhaustive: architecture contract (the few
  load-bearing decisions and why), how to run and verify, a symptom → where-to-look triage table,
  and failure archaeology (reverts and dead ends mined from git history, so no one re-fights a
  settled battle).
- **Say when NOT to use it.** A playbook that gets loaded for the wrong component is negative
  context. Put the scope boundary in the description/first paragraph.
- **Verify before you state.** Wrong runbooks are worse than none; every command and path should
  be checked against the repo at authoring time, and entries dated so staleness is visible.
- **Prune without mercy.** Playbooks load into context; every non-load-bearing line makes runs
  dumber. Prefer fewer, shorter files over complete coverage. When a battle is settled or
  re-settled, update the playbook in the same change.
- **Nothing consumer-secret.** Playbooks describing a private corpus live in that corpus, never
  in this repo (hard rule #1). This document describes the mechanism only.
