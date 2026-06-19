# Architecture Standards

The standards every contributor (human or AI) follows when changing `quest-ai-runner`. The first
half describes **how the code is organized** (so you put a change in the right place); the second
half is the **prescriptive rules** that code here must obey. `CLAUDE.md` is the short pre-flight
checklist; this is the reasoning behind it. When the two ever disagree, `CLAUDE.md`'s hard rules win.

`quest-ai-runner` splits into two halves and a set of swappable implementations. Every module is
named for its **role in the architecture**, not for any domain.

## The two halves

> the orchestrator **brain** (`core`) + the queued-task **executor** (`runner`)

- **`core/` — the brain.** The irreducible reasoning engine every consumer shares: a bounded
  `plan → gather → re-plan → answer/deep/confirm` loop. It depends on nothing else in the package
  except the interfaces it defines. It is called *core* because it's the stable center — everything
  else exists to feed it (`runner`, `adapters`, `config`) or deploy it.

- **`runner/` — the executor.** The worker Quest is missing: `poll → claim → run → escalate →
  report`. Quest can enqueue AI tasks but cannot run them; the runner closes that gap.

## The dependency rule (why `core` is `core`)

There is both a `core/adapters.py` **module** and an `adapters/` **package**. The distinction is
the whole design:

- **`core/adapters.py`** defines the **interfaces** — abstract `Protocol`/`ABC` *ports* the brain
  needs (`RetrievalAdapter`, `ModelProvider`, `DeepRunner`, `EscalationSink`, and the optional
  `ContextAssembler`, `VectorStore`, `GuidanceProvider`).
- **`adapters/`** provides **concrete implementations** of those ports (`FilesAdapter`,
  `CachedDbAdapter`, `AnthropicProvider`, `GeminiProvider`, `MultiProvider`, the vector/card
  stores, …) — the swappable parts.

So the dependency arrows all point **inward** toward `core`: `core` declares the abstract boundary
and imports none of the concrete fill-ins; `runner`, `adapters`, and `config` depend on `core`.
That inward-only dependency is exactly why `core` is the core — it's the part that stays the same no
matter who uses it. (This is the classic ports-and-adapters / hexagonal shape.)

**Rule:** never add an import from `core/` to `adapters/`, `runner/`, or `config.py`. If the brain
seems to need something concrete, it needs a new *interface* on `core/adapters.py` instead, with the
concrete fill-in living under `adapters/` and wired in through `RunnerConfig`.

## Module map

| Module | Role |
|---|---|
| `core/orchestrator.py` | the bounded plan/gather/re-plan/answer loop; emits `ProgressEvent`s |
| `core/adapters.py` | the adapter interfaces + value objects + the streaming sinks |
| `core/model_registry.py` | tier → live top-model id (via a `ModelProvider`) |
| `core/goal_runner.py` | the `/goal --max-turns` contract (`GoalRunner`, `SubprocessGoalRunner`) |
| `core/card_filter.py` | LLM card selection + `_extract_json()` fence-stripping helper |
| `core/guard.py`, `core/inbox.py` | broken-promise guard; mid-run `InputInbox` |
| `adapters/files_adapter.py` | reference `RetrievalAdapter` over a configured file root |
| `adapters/cached_db_adapter.py` | reference `RetrievalAdapter` over a live DB with a short-TTL cache |
| `adapters/anthropic_provider.py`, `gemini_provider.py`, `openai_provider.py` | reference `ModelProvider`s |
| `adapters/multi_provider.py` | routes a model call to the right provider by model-name prefix |
| `adapters/retry_utils.py` | provider-agnostic retry/backoff + `parse_json_with_retry` |
| `adapters/*_vector_store.py`, `*_context_*` | optional vector/card context assembly |
| `runner/quest_client.py` | the Quest HTTP client (`qsk_` auth) + `QuestDecisionSink` |
| `runner/poller.py` | event-driven, signature-deduped poll loop; bounded concurrency |
| `runner/executor.py` | run one claimed task through the brain → report |
| `config.py` | `RunnerConfig` + `build_orchestrator()` — where a consumer supplies everything specific |
| `cli.py` | the `quest-ai-runner` console entry point (env-driven) |

## The bounded brain loop

Each request runs a bounded loop. A fast **planner** (one cheap structured model call) auto-decides
the next step from the message + everything gathered so far. The planner returns one of these
actions:

- **read** — targeted partial reads/greps via the `RetrievalAdapter` (run concurrently), appended
  to what's gathered; the loop re-plans with what it just saw.
- **answer** — reply now, grounded in what's gathered (may fan out sub-questions).
- **deep** — author a concrete, checkable `goal` + brief and hand it to the `DeepRunner`.
- **confirm** — a human-only/risky step: raise it via the `EscalationSink` and stop.
- **clarify** — ask the user a bounded question when the request is genuinely ambiguous.

The loop is bounded by a max step count plus wall-clock and gathered-size budgets; on hitting the
cap it makes a best-effort grounded answer or escalates.

## The adapter roles

A consumer satisfies these (structurally, via `Protocol`, or by subclassing the parallel `*Base`
ABC) and wires them through `RunnerConfig`. The first four are required; the rest are additive — omit
one and you get the prior behavior.

| Role | What it does | Required? |
|---|---|---|
| `RetrievalAdapter` | GATHER: `read_section` / `grep` / `query` + discovery (`list_sources`, `describe_*`) | yes |
| `ModelProvider` | the LLM: `plan` / `answer` / `list_models` | yes |
| `DeepRunner` | spawn a bounded `/goal --max-turns` autonomous run, returns a `DeepResult` | yes |
| `EscalationSink` | raise a human-only confirm/decision, returns a decision id | yes |
| `ContextAssembler` | PRE-FLIGHT context: assemble task-relevant context once before the loop | optional |
| `VectorStore` | semantic orientation + auto-updating index (heavy deps behind `[qdrant]` extra) | optional |
| `GuidanceProvider` | use-case-specific instruction cards retrieved on demand | optional |

---

# Conventions code here must follow

These are the rules a reviewer will hold a change to. Most map to a one-line check in `CLAUDE.md`.

## 1. The generic boundary holds (the load-bearing rule)

The brain must stay ignorant of who is calling it. A new capability goes **behind an adapter
interface or into `RunnerConfig`** — never as a special case inside `core/`.

- No org name, persona, corpus, collection name, user/team id, key, or absolute path appears in
  `core/`, `runner/`, `adapters/`, the docs, or the git history. All of it is **consumer config**,
  supplied at runtime via `RunnerConfig` and env vars (see `examples/` and `.env.example`).
- This is what lets the same engine serve in-process chat, an integrating org, and a single-user
  lane unchanged. Prefer additive, backward-compatible changes; a breaking change to the public
  `core` API is fine when it's the right shape, but keep it generic, update `CHANGELOG.md`
  (Unreleased), and keep the tests green.

## 2. Adapters never raise from their public surface

Every adapter method is a boundary the brain trusts not to throw. A `read_section` that can't find
the file returns `Observation(kind="error", error=...)`; a `VectorStore.search` that's down returns
`[]`; a `GuidanceProvider` with no opinion returns `[]`/`None`; a `ProgressSink` swallows forward
failures. Wrap internals in `try/except` and degrade gracefully — a missing or broken adapter must
fall back, not stall the loop. The `*Base` ABCs document this method-by-method; honor it.

## 3. LLM calls: route, resolve, retry, parse

This is the single most common source of bugs in this repo. Four rules, in order:

1. **Route through `MultiProvider`, never a raw provider.** A raw `AnthropicProvider` doesn't know
   Gemini models (and 404s); a raw `GeminiProvider` doesn't know Claude. In entry-point code call
   `build_orchestrator(cfg)` first — it wraps `cfg.model_provider` with `MultiProvider` in place —
   then use `cfg.model_provider` for every call.
2. **Resolve the model by tier, never hardcode.** Use
   `ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")`. Never
   hardcode a model id and never use `list_models()[0]` (that's the first Anthropic id, which the
   current provider config may not route). Tiers: `"fast"` cheap lookups, `"balanced"`
   filtering/judgment (Gemini Flash class), `"best"` high-stakes reasoning.
3. **Retry transient errors.** Provider calls hit 503/429/timeout. Use the helpers in
   `adapters/retry_utils.py` (the retry decorator; `parse_json_with_retry` when the call must yield
   valid JSON) rather than letting a transient failure bubble up.
4. **Strip fences before `json.loads`.** LLM JSON often arrives wrapped in ```` ```json ```` fences.
   Never `json.loads(raw)` directly — use a helper like `_extract_json()` in `core/card_filter.py`.

```python
from .config import build_orchestrator, _config_from_env
from .core.model_registry import ModelRegistry

cfg = _config_from_env()
build_orchestrator(cfg)                       # wraps cfg.model_provider with MultiProvider in place
provider = cfg.model_provider
model = ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")
result = provider.answer([{"role": "user", "content": prompt}], model=model)
```

## 4. Modes and sinks own all messaging policy

There are **two product modes, one brain**. The orchestrator emits the *same* `ProgressEvent`s in
both; the `Mode` only selects which `ProgressSink` the consumer wires, and **the sink — never the
orchestrator — decides what surfaces**.

- `Mode.LIVE` — a human is attending; `StreamSink` forwards every event in real time.
- `Mode.BACKGROUND` — nobody's watching; `MilestoneSink` drops planning/reading/re-planning/status
  chatter and forwards only the surfacing set (`SURFACING_EVENTS`: result, decision, milestone,
  context, done, tokens).

When you add a new kind of event, add an `EVENT_*` constant in `core/adapters.py` and decide
deliberately whether it belongs in `SURFACING_EVENTS` (always shown) or is LIVE-only texture
(like `EVENT_EXEC`). Don't make the orchestrator branch on mode to decide visibility — that policy
lives in the sink, encoded once, so every consumer inherits it identically.

## 5. Config is the only place specifics enter

`RunnerConfig` is the single seam where a consumer supplies the Quest key, the adapters, the corpus,
the deep-runner, the model provider, and decision routing. Anything environment-specific (a team id,
an executor user, an API key, a collection) is read from env into config — never hardcoded, never
guessed. Document the env var a piece of code reads in a comment at the read site, and fall back
safely when it's unset rather than erroring.

## 6. Tests run offline; secrets never land

- The whole suite runs with no network and no API key: `python -m pytest -q`. A behavior change
  ships with a test that proves it, and that test must pass offline (mock the provider/adapter).
- Before committing, scan the staged diff for keys/PII (the `git diff --cached | grep -nE …` snippet
  in `CLAUDE.md`). Never commit a real `.env`, `qar_state.json`, or any `*_state.json`.

## 7. Concurrency and process hygiene

- Multiple AIs share this working copy. Stage only the files you changed (never `git add -A`), and
  never run destructive git ops (`git restore`, `reset --hard`, `checkout .`) over files with
  uncommitted changes — that's likely another agent's in-progress work.
- The runner deploys as systemd `--user` services. Restart with
  `systemctl --user restart quest-ai-runner-*.service`; don't kill processes directly.

## Glossary

**Brain** (`core/`, specifically `core/orchestrator.py`). The in-process, synchronous, domain-free
reasoning loop. It takes adapters, runs the bounded `plan → gather → re-plan → answer/deep/confirm`
cycle for a single request, and returns an `OrchestratorResult`. It knows nothing about tasks,
polling, databases, or any specific org: that is the point. The brain can be imported and called
directly by a backend or chat handler with no poller in the picture.

**Runner** (`runner/`, the "lane"). The executor Quest is missing: a `poll → claim → run →
escalate → report` loop that runs as a persistent service (or a one-shot `--once` cron). The
runner uses the brain to handle each claimed task, then reports the result back to Quest. "Runner"
and "lane" are used interchangeably: the lane is the deployment unit (a systemd service, a cron
job) that executes the runner loop.

In short: the **brain** reasons; the **runner** executes and reports.

See [writing-a-consumer.md](writing-a-consumer.md) and [adapters.md](adapters.md) for how to wire
your own implementations.
