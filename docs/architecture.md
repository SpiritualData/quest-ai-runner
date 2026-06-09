# Architecture & naming

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
  needs (`RetrievalAdapter`, `ModelProvider`, `DeepRunner`, `EscalationSink`).
- **`adapters/`** provides **concrete implementations** of those ports (`FilesAdapter`,
  `CachedDbAdapter`, `AnthropicProvider`) — the swappable parts.

So the dependency arrows all point **inward** toward `core`: `core` declares the abstract boundary
and imports none of the concrete fill-ins; `runner`, `adapters`, and `config` depend on `core`.
That inward-only dependency is exactly why `core` is the core — it's the part that stays the same no
matter who uses it. (This is the classic ports-and-adapters / hexagonal shape.)

## Module map

| Module | Role |
|---|---|
| `core/orchestrator.py` | the bounded plan/gather/re-plan/answer loop; emits `ProgressEvent`s |
| `core/adapters.py` | the four interfaces + value objects + the streaming sinks |
| `core/model_registry.py` | tier → live top-model id (via a `ModelProvider`) |
| `core/goal_runner.py` | the `/goal --max-turns` contract (`GoalRunner`, `SubprocessGoalRunner`) |
| `adapters/files_adapter.py` | reference `RetrievalAdapter` over a configured file root |
| `adapters/cached_db_adapter.py` | reference `RetrievalAdapter` over a live DB with a short-TTL cache |
| `adapters/anthropic_provider.py` | reference `ModelProvider` (Anthropic SDK) |
| `runner/quest_client.py` | the Quest HTTP client (`qsk_` auth) + `QuestDecisionSink` |
| `runner/poller.py` | event-driven, signature-deduped poll loop; bounded concurrency |
| `runner/executor.py` | run one claimed task through the brain → report |
| `config.py` | `RunnerConfig` — where a consumer supplies everything specific |
| `cli.py` | the `quest-ai-runner` console entry point (env-driven) |

## The bounded brain loop

Each request runs a bounded loop. A fast **planner** (one cheap structured model call) auto-decides
the next step from the message + everything gathered so far. The planner returns one of four
actions:

- **read** — targeted partial reads/greps via the `RetrievalAdapter` (run concurrently), appended
  to what's gathered; the loop re-plans with what it just saw.
- **answer** — reply now, grounded in what's gathered (may fan out sub-questions).
- **deep** — author a concrete, checkable `goal` + brief and hand it to the `DeepRunner`.
- **confirm** — a human-only/risky step: raise it via the `EscalationSink` and stop.

The loop is bounded by a max step count plus wall-clock and gathered-size budgets; on hitting the
cap it makes a best-effort grounded answer or escalates.

## The generic boundary

A consumer supplies all specifics through `RunnerConfig`: which Quest key, which adapters, which
corpus, which deep-runner, which model provider, decision routing. The library hardcodes none of it.
That's what lets the same engine serve in-process chat, an integrating org, and a single-user lane
unchanged. See [writing-a-consumer.md](writing-a-consumer.md) and [adapters.md](adapters.md).

## Glossary

These two terms appear throughout the docs and are worth keeping distinct:

**Brain** (`core/`, specifically `core/orchestrator.py`). The in-process, synchronous, domain-free
reasoning loop. It takes adapters, runs the bounded `plan → gather → re-plan → answer/deep/confirm`
cycle for a single request, and returns an `OrchestratorResult`. It knows nothing about tasks,
polling, databases, or any specific org — that is the point. The brain can be imported and called
directly by a backend or chat handler with no poller in the picture.

**Runner** (`runner/`, the "lane"). The executor Quest is missing: a `poll → claim → run →
escalate → report` loop that runs as a persistent service (or a one-shot `--once` cron). The
runner uses the brain to handle each claimed task, then reports the result back to Quest. "Runner"
and "lane" are used interchangeably: the lane is the deployment unit (a systemd service, a cron
job) that executes the runner loop. The brain is what the runner calls; the runner is how
the brain is deployed in the background.

In short: the **brain** reasons; the **runner** executes and reports.
