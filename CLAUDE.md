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
`docs/ARCHITECTURE_STANDARDS.md` for the full architecture and the standards code here must follow.

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

## Concurrent AI work — preserve uncommitted changes

Multiple AIs may work on this repo simultaneously. **Never run destructive git operations
(`git restore`, `git reset --hard`, `git checkout .`) on files with uncommitted changes.**

1. Always check `git status` before making changes
2. If a file has uncommitted changes, they're likely in-progress work from another AI
3. Leave uncommitted changes as-is; work around them or wait for them to be committed
4. Only discard changes if you're certain they're yours and no longer needed

This prevents accidentally losing concurrent work when a user interrupts (Ctrl+C) a long-running
edit or task.

## Making LLM calls in this repo

**Always use `MultiProvider`, never call a raw provider directly.**

The deployment uses `MultiProvider` to route model calls to the right underlying provider (Anthropic, Gemini, OpenAI) based on the model name. A raw `AnthropicProvider` does not know about Gemini models and will 404. A raw `GeminiProvider` does not know about Claude models.

The correct pattern in any code that needs to make an LLM call:

```python
# In CLI / entry-point code — call build_orchestrator() first so cfg.model_provider
# gets wrapped with MultiProvider, then use cfg.model_provider for all calls.
from .config import build_orchestrator, _config_from_env
cfg = _config_from_env()
build_orchestrator(cfg)          # wraps cfg.model_provider with MultiProvider in place
provider = cfg.model_provider    # now a MultiProvider — routes by model name prefix

from .core.model_registry import ModelRegistry
model = ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")
result = provider.answer([{"role": "user", "content": prompt}], model=model)
```

**Always use `resolve_tier()` for the model — never hardcode a model name, and never call `list_models()[0]`** (that returns the first model from the Anthropic API which may not be routable by the current provider config).

**Tier guidance:** use `"balanced"` for filtering/judgment tasks (Gemini Flash class), `"fast"` for cheap lookups. `"best"` for high-stakes reasoning. The `model_fallback` config maps these to real model IDs.

**JSON parsing:** LLM responses often include markdown fences. Never call `json.loads(raw)` directly — strip fences first with a helper like `_extract_json()` in `core/card_filter.py`.

## Conventions

- Match the surrounding code's style, naming, and comment density.
- Commit in this repo only; write clear, scoped commits.
- Tests for any behavior change, and they must pass offline.
- **Every file lives in its proper folder, never the repo root.** This applies to ALL files, not
  just tests: library code under `quest_ai_runner/`, tests under `tests/`, runnable reference
  consumers under `examples/`, docs under `docs/`, evaluation harnesses under `evaluation/`. The
  repo root is for project config only (`README.md`, `CHANGELOG.md`, `pyproject.toml`, `.env*`,
  etc.) — do not drop new `.py` files there.
- **Throwaway / scratch / demo scripts go in the gitignored `scratch/` directory** (create it if
  absent; it is in `.gitignore`), never the repo root and never committed. Delete them when done.
