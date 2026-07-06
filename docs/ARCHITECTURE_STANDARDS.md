# Architecture Standards

The standards every contributor (human or AI) follows when changing `quest-ai-runner`. The first
half is **how the code is organized** (so a change lands in the right place); the second is the
**rules code here must obey**. `CLAUDE.md` is the pre-flight checklist; this is the reasoning. When
the two disagree, `CLAUDE.md`'s hard rules win.

The package is two halves plus swappable implementations. Every module is named for its **role**, not
any domain.

## The two halves

- **`core/` — the brain.** The reasoning engine every consumer shares: a bounded
  `plan → gather → re-plan → answer/deep/confirm` loop. It imports nothing in the package but the
  interfaces it defines. Called *core* because it's the stable center; everything else feeds or
  deploys it.
- **`runner/` — the executor.** The worker Quest lacks: `poll → claim → run → escalate → report`.
  Quest enqueues AI tasks but can't run them; the runner closes that gap.

## The dependency rule (why `core` is `core`)

`core/adapters.py` (a module) defines the **interfaces** — abstract `Protocol`/`ABC` ports the brain
needs. `adapters/` (a package) provides the **concrete implementations**. Dependencies point only
**inward**: `core` declares the boundary and imports none of the fill-ins; `runner`, `adapters`, and
`config` depend on `core`. That's the classic ports-and-adapters shape, and why `core` stays the same
no matter who uses it.

**Rule:** never import from `core/` into `adapters/`, `runner/`, or `config.py`. If the brain seems
to need something concrete, add a new *interface* on `core/adapters.py`, put the fill-in under
`adapters/`, and wire it via `RunnerConfig`.

## Module map

| Module | Role |
|---|---|
| `core/orchestrator.py` | the bounded plan/gather/re-plan/answer loop; emits `ProgressEvent`s |
| `core/adapters.py` | the adapter interfaces + value objects + the streaming sinks |
| `core/model_registry.py` | tier → live top-model id (via a `ModelProvider`) |
| `core/goal_runner.py` | the `/goal --max-turns` contract (`GoalRunner`, `SubprocessGoalRunner`) |
| `core/card_filter.py` | LLM card selection + `_extract_json()` fence-stripping helper |
| `core/guard.py`, `core/inbox.py` | per-turn `ExecutionRecord` (claim honesty is judged in the goal verification); mid-run `InputInbox` |
| `adapters/files_adapter.py` | reference `RetrievalAdapter` over a configured file root |
| `adapters/cached_db_adapter.py` | reference `RetrievalAdapter` over a live DB with a short-TTL cache |
| `adapters/{anthropic,gemini,openai}_provider.py` | reference `ModelProvider`s |
| `adapters/multi_provider.py` | routes a model call to the right provider by model-name prefix |
| `adapters/retry_utils.py` | provider-agnostic retry/backoff + `parse_json_with_retry` |
| `adapters/*_vector_store.py`, `*_context_*` | optional vector/card context assembly |
| `runner/quest_client.py` | the Quest HTTP client (`qsk_` auth) + `QuestDecisionSink` |
| `runner/poller.py` | event-driven, signature-deduped poll loop; bounded concurrency |
| `runner/executor.py` | run one claimed task through the brain → report |
| `config.py` | `RunnerConfig` + `build_orchestrator()` — the one seam for consumer specifics |
| `cli.py` | the `quest-ai-runner` console entry point (env-driven) |

## The bounded brain loop

A fast **planner** (one cheap structured model call) picks the next step from the message plus
everything gathered so far. It returns one action:

- **read** — targeted reads/greps via the `RetrievalAdapter` (concurrent), then re-plan with what it saw.
- **answer** — reply now, grounded in what's gathered (may fan out sub-questions).
- **deep** — author a checkable `goal` + brief and hand it to the `DeepRunner`.
- **confirm** — a human-only/risky step: raise it via the `EscalationSink` and stop.
- **clarify** — ask the user a bounded question when the request is genuinely ambiguous.

The loop is bounded by a max step count plus wall-clock and gathered-size budgets; on the cap it
makes a best-effort grounded answer or escalates.

## The adapter roles

A consumer satisfies these (via `Protocol` or the parallel `*Base` ABC) and wires them through
`RunnerConfig`. First four required; the rest additive — omit one and you get the prior behavior.

| Role | What it does | Required? |
|---|---|---|
| `RetrievalAdapter` | GATHER: `read_section` / `grep` / `query` + discovery (`list_sources`, `describe_*`) | yes |
| `ModelProvider` | the LLM: `plan` / `answer` / `list_models` | yes |
| `DeepRunner` | spawn a bounded `/goal --max-turns` autonomous run, returns a `DeepResult` | yes |
| `EscalationSink` | raise a human-only confirm/decision, returns a decision id | yes |
| `ContextAssembler` | PRE-FLIGHT: assemble task-relevant context once before the loop | optional |
| `VectorStore` | semantic orientation + auto-updating index (heavy deps behind `[qdrant]`) | optional |
| `GuidanceProvider` | use-case-specific instruction cards retrieved on demand | optional |

---

# Rules code here must follow

Each maps to a one-line check in `CLAUDE.md`.

## 1. The generic boundary holds (load-bearing)

The brain stays ignorant of who calls it. A new capability goes **behind an adapter interface or into
`RunnerConfig`** — never as a special case in `core/`. No org name, persona, corpus, collection,
user/team id, key, or absolute path appears in `core/`, `runner/`, `adapters/`, the docs, or the git
history — that's all **consumer config**, supplied at runtime (see `examples/` and `.env.example`).
This is what lets one engine serve in-process chat, an integrating org, and a single-user lane
unchanged. Prefer additive changes; a breaking `core` API change is fine when it's the right shape —
keep it generic, update `CHANGELOG.md` (Unreleased), keep tests green.

## 2. Adapters never raise from their public surface

Every adapter method is a boundary the brain trusts not to throw: `read_section` returns
`Observation(kind="error", ...)`, `VectorStore.search` returns `[]`, `GuidanceProvider` returns
`[]`/`None`, a `ProgressSink` swallows forward failures. Wrap internals in `try/except` and degrade —
a missing or broken adapter must fall back, not stall the loop. The `*Base` ABCs document this
per-method; honor it.

## 3. LLM calls: route, resolve, retry, parse

The most common source of bugs here. Four rules, in order:

1. **Route through `MultiProvider`, never a raw provider** (a raw `AnthropicProvider` 404s on Gemini
   models, and vice versa). In entry-point code call `build_orchestrator(cfg)` first — it wraps
   `cfg.model_provider` with `MultiProvider` in place — then use `cfg.model_provider`.
2. **Resolve by tier, never hardcode.** Use
   `ModelRegistry(provider, fallback=cfg.model_fallback or None).resolve_tier("balanced")`. Never a
   hardcoded id, never `list_models()[0]` (the current provider may not route it). Tiers: `"fast"`
   cheap lookups, `"balanced"` filtering/judgment, `"best"` high-stakes reasoning.
3. **Retry transient errors** (503/429/timeout) with the helpers in `adapters/retry_utils.py`
   (the retry decorator; `parse_json_with_retry` when the call must yield valid JSON).
4. **Strip fences before `json.loads`** — LLM JSON often arrives in ```` ```json ```` fences. Use a
   helper like `_extract_json()` in `core/card_filter.py`, never `json.loads(raw)`.

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

**Two product modes, one brain.** The orchestrator emits the *same* `ProgressEvent`s in both; the
`Mode` only selects which `ProgressSink` is wired, and **the sink — never the orchestrator — decides
what surfaces**. `Mode.LIVE` → `StreamSink` forwards every event; `Mode.BACKGROUND` → `MilestoneSink`
drops planning/reading/status chatter and forwards only `SURFACING_EVENTS` (result, decision,
milestone, context, done, tokens). A new event gets an `EVENT_*` constant in `core/adapters.py` and a
deliberate choice: in `SURFACING_EVENTS` (always shown) or LIVE-only texture (like `EVENT_EXEC`).
Never branch the orchestrator on mode for visibility — that policy lives in the sink, once.

## 5. Config is the only place specifics enter

`RunnerConfig` is the single seam for the Quest key, adapters, corpus, deep-runner, model provider,
and decision routing. Anything environment-specific (team id, executor user, API key, collection) is
read from env into config — never hardcoded or guessed. Document the env var in a comment at the read
site, and fall back safely when it's unset rather than erroring.

## 6. Tests run offline; secrets never land

The suite runs with no network and no API key (`python -m pytest -q`); a behavior change ships with a
test that proves it offline (mock the provider/adapter). Before committing, scan the staged diff for
keys/PII (the snippet in `CLAUDE.md`). Never commit a real `.env`, `qar_state.json`, or any
`*_state.json`.

## 7. Concurrency and process hygiene

Multiple AIs share this working copy: stage only files you changed (never `git add -A`), and never
run destructive git ops (`git restore`, `reset --hard`, `checkout .`) over uncommitted changes —
likely another agent's WIP. The runner deploys as systemd `--user` services; restart with
`systemctl --user restart quest-ai-runner-*.service`, don't kill processes directly.

---

In short: the **brain** (`core/`) reasons; the **runner** (`runner/`) executes and reports. See
[writing-a-consumer.md](writing-a-consumer.md) and [adapters.md](adapters.md) to wire your own
implementations.
