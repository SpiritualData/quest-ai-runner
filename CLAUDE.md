# CLAUDE.md — working rules for this repository

This file orients any contributor (human or AI) working in `quest-ai-runner`, and encodes the
**hard rules** that keep this public, open-source repository clean. Read it before making changes.

## What this project is

`quest-ai-runner` is a **domain-free** library: the orchestrator **brain** (`quest_ai_runner.core`)
plus the queued-task **executor** (`quest_ai_runner.runner`) for Quest AI tasks. It is published as
open source (Apache-2.0) and is meant to run for *any* org, not one specific deployment.

- `core/` — the brain: a bounded plan → gather → re-plan → answer/deep/confirm loop. Imports
  nothing about Quest, any database, any org, or any user. Depends only on the four adapter
  *interfaces* it defines in `core/adapters.py`.
- `runner/` — the executor: poll → claim → run → escalate → report.
- `adapters/` — reference *implementations* of the interfaces (`FilesAdapter`, `CachedDbAdapter`,
  `AnthropicProvider`).
- `config.py` — `RunnerConfig`: the single place a consumer supplies everything specific.
- `examples/` — runnable reference consumers (env-driven, no real data).
- `docs/` — tutorial + how-tos + architecture.

Module names describe **architectural role**, not domain. "core" is the stable center everything
depends on; "runner" is the executor; "adapters" are the swappable implementations. See
`docs/architecture.md`.

## 🔒 Hard rule #1 — NOTHING consumer-specific or secret goes in the repo

This repo is public. The following must **never** appear in `quest_ai_runner/`, `tests/`,
`examples/`, the docs, or **the git history**:

- API keys or tokens of any kind (Quest `qsk_...` keys, `sk-ant-...`, bearer tokens, passwords).
- Real user ids, team ids, org-internal identifiers, or email addresses of real people.
- Absolute filesystem paths from a real machine (e.g. `/home/<someone>/...`).
- Any one org's names, personas, corpora, or business logic baked into the library.

All of that is **consumer config**. It comes in at runtime via `RunnerConfig` and environment
variables — see `examples/custom_consumer.py` and `.env.example`. If a change would hardcode any
of the above, route it through config or an adapter instead.

> Why this file exists: the project was extracted from an internal codebase that *did* contain
> such specifics. They were removed and the git history was reset before publication. Do not
> reintroduce them.

## 🔒 Hard rule #2 — the generic boundary holds

New capabilities go **behind one of the four adapter interfaces** (`RetrievalAdapter`,
`ModelProvider`, `DeepRunner`, `EscalationSink`) or into `RunnerConfig` — never as a special case
inside the brain. The brain must stay ignorant of who is calling it.

Nothing here is frozen. This is pre-release and single-consumer, so the public API in
`quest_ai_runner.core` (`Mode`, `StreamSink`, `MilestoneSink`, `ProgressEvent`, `Orchestrator`) is
free to evolve — change it whenever the design calls for it. Prefer additive, backward-compatible
changes where they're just as clean, but a breaking change is fine when it's the right shape; just
keep it generic (hard rule #2 above), update the `CHANGELOG.md` (Unreleased) and any affected docs,
and keep the tests green.

## Before you commit or push

1. Run the tests — they're offline (no network, no API key):
   ```bash
   python -m pytest -q
   ```
2. Scan your staged change for secrets/PII before committing:
   ```bash
   git diff --cached | grep -nE 'qsk_[A-Za-z0-9]|sk-ant-|/home/|@[A-Za-z0-9.-]+\.(com|org)|[0-9a-f]{24}' \
     && echo "^^ REVIEW: possible secret/PII — do not commit until cleared" || echo "scan clean"
   ```
   (`[0-9a-f]{24}` catches Mongo-style ObjectIds. A hit isn't always a problem — review it.)
3. Never commit a real `.env`, `qar_state.json`, or any `*_state.json` (all gitignored).
4. Keep `CHANGELOG.md` (Unreleased) and docs in sync with behavior changes.

## Process management

When running `quest-ai-runner poll` or `quest-ai-runner chat` as systemd user services:
- **Restart via systemctl:** `systemctl --user restart quest-ai-runner-*.service`
- **Do NOT kill processes directly** — use systemctl to restart cleanly
- The service file manages lifecycle; always use systemctl for start/stop/restart

## Conventions

- Match the surrounding code's style, naming, and comment density.
- Commit in this repo only; write clear, scoped commits.
- Tests for any behavior change, and they must pass offline.
