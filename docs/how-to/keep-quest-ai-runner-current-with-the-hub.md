# Keep Quest AI Runner current with the hub (for an AI agent)

Use this when an AI agent or a person manages a QAR deployment and wants to notice, and resolve, what the hub of hubs asks of it:
a missing key or package, or a newer selection than the one the deployment runs with. The mechanics are esoi's; this page says
how QAR plugs into them. The agent's loop is in esoi's how-to,
[keep a consumer current with an AI agent](https://github.com/JoshuaMathias/evolvable-structure/blob/main/docs/how-to/keep-a-consumer-current-with-an-ai-agent.md),
and the file formats are esoi's reference pages on requirements and the lock.

## 1. Check the environment

```bash
quest-ai-runner doctor            # add --json for a machine-readable report
```

It lists what QAR's hub assembly needs (variables, API keys, packages, programs, services) with the remedy line under each unmet
item. It is offline, calls no model, and prints names only, never a value. Exit codes: 0 everything met, 1 something required is
unmet (or a seat's selected arm cannot be built), then the lock status below. `quest-ai-runner chat --check` prints the same report.
The terminal shows one line at startup only when something required is unmet.

A key is a step for a person: take the signup link from the report, never invent a key, and never write one into a committed file.
The hub-declared variables in `.env.example` are generated from the hub's declarations
(`python -m quest_ai_runner.hub_requirements --write-env-example .env.example`); QAR's own variables stay in
`esoi/env_descriptions.csv` and the rest of `.env.example`.

## 2. Check for a newer selection

A deployment that follows the hub's latest selections has nothing to check. A production deployment can record its selections in
a lock with `esoi lock --package quest_ai_runner`; whether to lock is the owner's decision, and no lock file is committed here.
With one present, `doctor` (or `quest-ai-runner doctor --lock PATH`) compares it with the hub:

| Exit | Meaning |
|---|---|
| 0 | Up to date |
| 3 | Updates available and all safe |
| 4 | Updates available and at least one blocked by an unmet requirement |
| 2 | Error reading the lock |

Run it on a schedule, for example daily from cron or a systemd timer, and let it post the plan once per change:

```bash
quest-ai-runner doctor --lock esoi.lock.json --note-quest <quest id> --state qar_hub_update_state.json
```

The note carries each update, the evidence for it and the remedy steps in order. This is a script plus a schedule, not a hook
inside the poller or the autopilot pass, because the lock is a per-deployment file and the pass is a model task.

## 3. Resolve

Follow esoi's loop: apply what is safe (`esoi lock --package quest_ai_runner --update --safe`), resolve each blocked update by
its steps (install packages, set variables, ask a person for a key), apply it, then verify with `quest-ai-runner doctor` and the
tests you would normally run, and commit the lock with the evidence line of each change. If a step needs a person and none is
available, leave that solution locked.

## Check that the selections are coherent

```bash
esoi requirements --package quest_ai_runner --coherence --offline
```

Exit 0 means the selected implementations' requirements can coexist. It is part of the checks in `CLAUDE.md`.

## Follow the hub where QAR differs from it

QAR differs from the hub's defaults in a few seats, and each difference is a row of `esoi/overrides.csv` that cites QAR's own problem, ideal
and metric. `cards-from-files` is one: the hub's default builder is `per_file_symbols`, measured as the best free file finder, but QAR
selects its own `qar_topic_bootstrap` (`quest_ai_runner/remember_and_recall/qar_topic_bootstrap.py`), because a QAR card groups what several files
say about one subject and a card per file is a file index. The bootstrap resolves the seat through QAR's Assembler
(`quest_ai_runner/hub.py`, `cards_from_files`), so whatever the hub learns about building cards reaches QAR as a candidate arm, while origin
stamps, scope tags, the card limit, the ceilings, the folder gate and the upload stay QAR's. The upload and the Quest backend write path
refuse a batch of one card per file (`adapters/card_shape_guard.py`, `QAR_MAX_CARDS_PER_FILE` and `QAR_MAX_SINGLE_FILE_CARD_SHARE`), whatever arm
is selected: the guard is a safety net in the write path, not part of the ranking.

**One number decides.** The hub's candidate arms are scored on QAR's own metric, `metric-select-context`
(`evaluation/components/context_selection`), as one balanced score per arm: accuracy over every case (the 63 file-finding cases and the
60 topic cases from QAR's git history), cost (model dollars plus a store charge that grows with the share of the 1,000-card limit the arm's
store fills) and time, with the weights and budgets in `[balance.context_selection]` of `esoi.toml`, fixed before an arm is scored. The
hub's `per_file_symbols` and a one-card-per-folder baseline are arms of that metric beside QAR's own; both are free and run offline with
`esoi run context_selection --impl <arm> --record`.

**The weekly loop.** `esoi follow` (the esoi library's command that checks a child's overrides against the parent's selections with
the child's own metric) is to be run weekly for QAR. It compares the parent's selected arm for each overridden seat with QAR's on this
one number, using QAR's own free stage, and reports one of three things: QAR's arm still wins (the override stands and keeps its reasons),
the parent's arm wins on QAR's number (QAR's `selected` is promoted with `esoi select <seat> --promote`, a release decision for Joshua
when it changes stored cards, card counts or what is uploaded), or the arms are not comparable yet (the free stage cannot run the
candidate; the override stays and says so). Nothing here depends on that command existing: the records it reads are the override reasons
(metric ids) and the recorded rows of the metric, both kept up to date by hand until then.

**No override outlives its measurement.** A row of `esoi/overrides.csv` is kept while the metric it cites has a recorded row showing QAR's
arm at least as good as the parent's on the current cases and code. When the cases, the budgets or the code under either arm change, the
rows are re-recorded for the free arms (`esoi run --stale --plan` names them), and a row with no current measurement is removed or its
seat returns to the hub's default.
