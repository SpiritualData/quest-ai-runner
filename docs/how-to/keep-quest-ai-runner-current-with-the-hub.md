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
