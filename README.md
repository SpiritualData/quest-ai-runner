# quest-ai-runner

[![CI](https://github.com/SpiritualData/quest-ai-runner/actions/workflows/ci.yml/badge.svg)](https://github.com/SpiritualData/quest-ai-runner/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

> The orchestrator **brain** + the queued-task **executor** for Quest AI tasks — generic, with no
> consumer-specific logic.

`quest-ai-runner` is the executor Quest is missing: Quest can *enqueue* AI tasks for a team/org but
cannot *run* them — no internal worker, no callback contract, no reference client. This library
closes that gap, generically.

It is **domain-free**. The brain knows how to ground, reason, run to a written `/goal` standard,
confirm-before-act, and escalate. It does **not** know about any specific org, cockpit, or person.
Those specifics are **config + adapters the consumer supplies** via
`quest_ai_runner.config.RunnerConfig`.

> Built and used in production by [Spiritual Data](https://spiritualdata.org); released as open
> source for any team running Quest AI tasks.

## Install

```bash
pip install -e .                       # core + runner are stdlib-only
pip install -e '.[anthropic,dev]'      # add the Anthropic reference provider + pytest
```

## Quickstart

```python
from quest_ai_runner.config import RunnerConfig, build_orchestrator
from quest_ai_runner.adapters import FilesAdapter, AnthropicProvider

cfg = RunnerConfig(retrieval=FilesAdapter("/path/to/docs"),
                   model_provider=AnthropicProvider())
orch = build_orchestrator(cfg)
print(orch.run("What does the pricing doc say?").text)   # grounded answer
```

To run the **executor** lane (poll Quest for due tasks and run them), see
[`docs/quickstart.md`](docs/quickstart.md) and [`examples/`](examples/), or just:

```bash
QUEST_BASE_URL=... QUEST_API_KEY=qsk_... QAR_CORPUS_ROOT=... quest-ai-runner --once
```

## Features

- **Smart Context Selection (TF-DF-IDF sampling)** — Instead of reading all files or sampling randomly, the runner uses a linguistic heuristic to select the *most representative* items from each group. **62% fewer tokens** on typical codebases, zero external dependencies. See [TF-DF-IDF Sampling](docs/TF_DF_IDF_SAMPLING.md) for details.
- **Multi-source retrieval** — Composite retrieval adapter lets you query files, databases, Claude conversations, vector stores, etc., all in one orchestrator.
- **Streaming & live events** — LIVE mode streams partial results to the user; BACKGROUND mode runs detached and reports back. Handoff between the two is seamless.
- **Extensible adapters** — Four clean interfaces (Retrieval, ModelProvider, DeepRunner, EscalationSink) are Protocol-based, so you implement only what you need.
- **Discovery-driven planning** — The brain learns source structure at runtime, never needing a static schema blob in the prompt.

## Documentation

- [Quickstart tutorial](docs/quickstart.md) — install, ground the brain, run the executor lane.
- [TF-DF-IDF Sampling](docs/TF_DF_IDF_SAMPLING.md) — smart context selection: 62% fewer tokens, zero external deps.
- [Architecture & naming](docs/architecture.md) — brain / runner / adapters, and why `core` is `core`.
- [Writing a consumer](docs/writing-a-consumer.md) — wire the library to your own Quest backend.
- [Implementing adapters](docs/adapters.md) — the four interfaces and how to build your own.
- [Streaming & modes](docs/streaming-and-modes.md) — LIVE vs BACKGROUND, sinks, the handoff.
- [Deployment](docs/deployment.md) — cron and systemd.
- [Quest API contract](docs/quest-api-contract.md) — the endpoints the runner speaks.

## The three consumers

1. **Quest in-process chat (via `core`).** Quest's backend (or a standalone-team brain) imports
   `quest_ai_runner.core`, constructs an `Orchestrator` with its adapters, and calls `run(...)`
   per message. No poller, no network beyond the model — just the brain in-process.
2. **An integrating org (via the `runner`).** An org that wants its system to execute Quest AI
   tasks for its teams supplies its Quest API key + adapters (its corpus via `FilesAdapter`, its
   live data via `CachedDbAdapter`) and runs the `Poller`. Spiritual Data's own cockpit is exactly
   this: it grounds on its corpus + live Quest data and runs the same engine.
3. **A single-user personal lane (via the `runner`).** The same engine runs for one person: a
   personal Quest key + a personal corpus root, config-only. The pattern was proven by a bespoke
   personal "watchdog" loop that generalized cleanly into the `Poller` — same poll → claim → run →
   escalate → report loop, just different config.

Because the same engine serves all three, maintenance happens once. Consumers differ only in
*config* — which key, which adapters, which corpus.

## Repo layout

```
quest_ai_runner/
├── core/                      # the domain-free brain (import this in-process)
│   ├── adapters.py            # the four INTERFACES (Protocol + ABC): RetrievalAdapter,
│   │                          #   ModelProvider, DeepRunner, EscalationSink + value objects;
│   │                          #   plus Mode, ProgressEvent/ProgressSink + StreamSink/MilestoneSink/FanoutSink
│   ├── orchestrator.py        # the bounded loop: plan → gather → re-plan → answer/deep/confirm
│   │                          #   emits events to a ProgressSink; run(mode,sink,...) + run_stream(...)
│   ├── model_registry.py      # tier → live-top-model id (pluggable ModelProvider source)
│   └── goal_runner.py         # the /goal --max-turns contract (GoalRunner + SubprocessGoalRunner)
├── adapters/                  # reference RetrievalAdapter / ModelProvider implementations
│   ├── files_adapter.py       # read_section/grep over a configured root (quest-docs + corpus)
│   ├── cached_db_adapter.py   # live DB reads via short-TTL cache (the "no file sync" approach)
│   └── anthropic_provider.py  # model calls + models.list bucketing
├── runner/                    # the EXECUTOR (the watchdog generalized)
│   ├── quest_client.py        # assistant-tasks + team-decisions API, qsk_ auth; QuestDecisionSink
│   ├── poller.py              # event-driven, signature-deduped poll loop; bounded concurrency
│   └── executor.py            # run one claimed task through the brain → report result/decision
├── config.py                  # RunnerConfig (consumer supplies key, adapters, corpus, tuning)
└── cli.py                     # `quest-ai-runner` console entry (poller, env-driven)
examples/                      # runnable reference consumers (env-driven, no real data)
docs/                          # tutorial, how-tos, architecture, deployment
tests/                         # core loop, registry bucketing, runner + examples (offline)
```

## The four adapter interfaces (the generic boundary)

| Interface | Role | Reference impl |
|---|---|---|
| `RetrievalAdapter` | GATHER — `read_section` / `grep` / `query` over the source | `FilesAdapter`, `CachedDbAdapter` |
| `ModelProvider` | the LLM — `plan` / `answer` / `list_models` | `AnthropicProvider` |
| `DeepRunner` | spawn a bounded, goal-driven run (`/goal --max-turns`) | `SubprocessGoalRunner` |
| `EscalationSink` | raise a human-only confirm/decision, return its id | `QuestDecisionSink` |

A consumer satisfies them structurally (they're `typing.Protocol`) or by subclassing the ABCs.

### Discovery: the brain learns source structure at runtime

`RetrievalAdapter` includes four *discovery* methods that make the source self-describing, so
the planner never needs a static schema blob pushed into its prompt. Instead it *asks*:

| Method | Cost | What it returns |
|---|---|---|
| `list_sources()` | cheap | the collections/tables/doc-sets that exist, one line each |
| `describe_source(name)` | drill-down | the fields/types (or heading outline) of ONE named source |
| `list_operations()` | cheap | the callable operations (reads and mutations), one line each |
| `describe_operation(name)` | drill-down | the full signature/usage of ONE named operation |

The pattern is cheap-then-drill-down: the planner calls `list_sources` (or `list_operations`)
first to see what exists, then `describe_*` on the one or two it needs. Both levels batch in
parallel with the rest of the `reads[]` step, so there is no extra round-trip.

The brain is taught *discover before guess*: when the planner does not already know which source
or operation a request needs, it calls `list_sources` / `list_operations` rather than inventing a
shape. This keeps the planner's knowledge current without the consumer having to maintain a static
context blob.

Reference implementations:

- `FilesAdapter` enumerates readable files under the configured root and extracts a markdown
  heading outline per file for `describe_source`.
- `CachedDbAdapter` accepts optional `sources` (a `{name: description}` dict) and `operations`
  (a rendered listing string) at construction time, falling back to schema inference from a
  sample row when these are not supplied.

Older adapters that predate the discovery methods continue to work: the orchestrator dispatches
via `getattr` and returns a benign "discovery not supported" observation when the methods are
absent, so the loop never stalls.

## How a bespoke watchdog generalized

The design was proven by a bespoke single-user "watchdog" poll loop, then generalized into the
library. The mapping (useful if you're porting a similar loop of your own):

| Bespoke watchdog mechanism | Generalized into |
|---|---|
| Poll personal Quest for DUE queued tasks | `Poller.run_once` → `QuestClient.discover_due` (poll-mode floor) |
| `watchdog_state.json` signature dedup | `StateStore` (pluggable; same exactly-once guarantee) |
| Only `queued` fires; assistant PATCHes `in_progress` | `Poller` claim step (`QuestClient.claim`; backend is source of truth) |
| `subprocess.Popen` spawns without blocking | bounded `ThreadPoolExecutor` (`max_concurrent_tasks`) |
| Loads creds, runs the worker with a skill | consumer-supplied `RunnerConfig` (the generic boundary) |
| Priming baselines existing items | `StateStore` records handled signatures |
| Unconfigured key → log + exit 0 | `Poller`/`cli` degrade visibly (never crash) |
| systemd loop + `--once` cron | `Poller.run_forever()` + `quest-ai-runner --once` |

## The Quest contract implemented (`runner/quest_client.py`)

Auth: `Authorization: Bearer qsk_<hex>` (the executor identity).

- **Discover**: `GET /api/assistant-tasks?status=queued&due_before=<ISO-now>`
- **Claim**: `PATCH /api/assistant-tasks/{id}` `{status: in_progress}`
- **Report**: `PATCH .../{id}` `{status: done|needs_you|failed, result, decision_id?}`
- **Escalate** (human-only step): `POST /api/teams/{team_id}/decisions`
  (`default_on_silence=hold`) → returns a `decision_id`, stamped on the task via `needs_you`.
- **Loop-close**: `GET /api/teams/decisions/for-user`, `POST /api/teams/decisions/{id}/resolve`.
- **Identity**: `GET /api/teams/whoami` (validate the key; `quest-ai-runner --check`).

## How quest-backend / cockpit import `core` vs. run the `runner`

- **In-process chat** (Quest backend, cockpit chat): import the brain and call it directly —
  no poller, no Quest task API:

  ```python
  from quest_ai_runner.config import RunnerConfig, build_orchestrator
  from quest_ai_runner.adapters import FilesAdapter, AnthropicProvider

  cfg = RunnerConfig(retrieval=FilesAdapter("/path/to/quest-docs"),
                     model_provider=AnthropicProvider())
  orch = build_orchestrator(cfg)
  result = orch.run("What does the pricing doc say?")   # OrchestratorResult(kind="answer", ...)
  ```

- **Executor lane** (an integrating org's task-runner, or a personal lane): run the poller.

  ```python
  from quest_ai_runner.config import RunnerConfig
  from quest_ai_runner.adapters import FilesAdapter, AnthropicProvider
  from quest_ai_runner.core.goal_runner import SubprocessGoalRunner, SubprocessConfig
  from quest_ai_runner.runner.poller import Poller

  cfg = RunnerConfig(
      quest_base_url="https://api.example.org", quest_api_key="qsk_...", team_id="team_123",
      retrieval=FilesAdapter("/path/to/corpus"),
      model_provider=AnthropicProvider(),
      deep_runner=SubprocessGoalRunner(SubprocessConfig(working_dir="/path/to/corpus")),
  )
  Poller(cfg, state_path="qar_state.json").run_forever()
  ```

  Or via the console entry (env-driven): `QUEST_BASE_URL=... QUEST_API_KEY=qsk_... \
  QAR_CORPUS_ROOT=... quest-ai-runner --once` (cron) or `quest-ai-runner` (service).

## The two product modes (one brain) — the frozen streaming interface

Every run is one of two **modes** (`quest_ai_runner.core.Mode`). The brain emits the **same**
events in both; a **`ProgressSink`** (chosen by mode) decides what surfaces. **The orchestrator
never decides messaging policy** — it only emits `ProgressEvent`s; the sink applies the rule.

| | **LIVE (attended)** | **BACKGROUND (sent-off / scheduled)** |
|---|---|---|
| Who's watching | a human, in real time | nobody |
| Sink | `StreamSink` — forwards **every** event | `MilestoneSink` — surfaces **only** `result` / `decision` / `milestone` / `done` |
| Surfaces | status ticks, plan/replan, reads, reply, confirm | result, decision-request, real milestone (planning/reading chatter **dropped**) |
| Called by | quest-backend / cockpit **in-process** | the **poller's** poll-by-due lane |

The surfacing rule lives **once**, in the sink (`SURFACING_EVENTS = {result, decision, milestone,
done}`), so every consumer inherits identical policy.

**Public API consumers import (this is frozen):**

```python
from quest_ai_runner.core import Mode, StreamSink, MilestoneSink, ProgressEvent
# orch = Orchestrator(...)  (or build_orchestrator(cfg))

# --- LIVE, in-process streaming (quest-backend chat / cockpit) ---
sink = StreamSink(forward=lambda ev: websocket.send(ev))   # ev is a dict
result = orch.run(user_message, mode=Mode.LIVE, sink=sink)  # streams as it works; returns the result
# ...or iterate events as a generator (yields event dicts, then the terminal OrchestratorResult):
for item in orch.run_stream(user_message, mode=Mode.LIVE):
    render(item)            # OrchestratorResult last; dict events before it

# --- BACKGROUND execution (the runner/executor does this for you) ---
sink = MilestoneSink(on_result=..., on_decision=..., on_milestone=..., on_done=...)
result = orch.run(task_text, mode=Mode.BACKGROUND, sink=sink)   # only milestones/result/decision surface

# --- LIVE -> BACKGROUND handoff (consumer disconnected) ---
result = orch.run(msg, mode=Mode.LIVE, sink=live_stream,
                  background_sink=MilestoneSink(...), detach_check=lambda: consumer.disconnected)
# if detach_check() turns True mid-run, the run CONTINUES; the rest of the events (incl. the
# final result) are delivered via background_sink instead of the dropped live stream.
```

A plain `orch.run(msg)` with **no** mode/sink args still works (non-streaming; legacy `status`
callback only). The `runner/executor.py` already runs BACKGROUND with a `MilestoneSink` whose
`on_milestone` posts an optional progress note and whose result/decision flow through the
existing Quest PATCH path — so live and background report **consistently**.

**Live↔Background handoff (both directions):**
- **Live → Background** is **core's** job: pass `background_sink` + `detach_check` to `run`; on
  detach the run finishes and delivers its result via the background path (notify/PATCH), not the
  dropped stream. (v1 mechanism: `core.FanoutSink` flips sinks mid-run.)
- **Background → Live** ("a background result surfaces live when the user returns") is a
  **consumer/UI** concern — the result is already persisted/reported by the background path; the
  UI just reads it back. Core does nothing extra here.

## Scheduling — Quest scheduling **is** the due time; the runner is timing-agnostic

There is **no separate scheduling plumbing**. The poller discovers by
`GET /api/assistant-tasks?status=queued&due_before=<now>`, so:

- **Run now** = Quest stamps `due` = now → discovered on the next scan.
- **Scheduled for T** = Quest stamps `due` = T → **not discovered/claimed until `now >= T`** (a
  future due-time is simply not-yet-due, so it isn't returned by `due_before=now`).
- **Recurring** = Quest re-stamps the next `due` after each completion → it surfaces each period.

The runner only ever asks *"what's due?"*; **Quest scheduling is the timing layer.** (See the
scheduling tests in `tests/test_runner.py`.)

## Install & test

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .            # core + runner are stdlib-only
pip install -e '.[anthropic,dev]'   # add the Anthropic provider + pytest
python -m pytest -q         # tests use stub adapters + a mock Quest client — no live network
```

The tests need no network and no API key: the core loop runs against a stub `ModelProvider` +
stub `RetrievalAdapter`, and the runner runs against a mock `QuestClient`.

## The context engine, and the part that compounds

The core design decision behind the context engine is information-theoretic. Shannon's **Data
Processing Inequality** (1948) proves that every agent-to-agent handoff can only destroy
information, never create it. The naive alternative — a "context finder" agent that summarizes the
corpus and hands a compressed summary to the reasoning agent — is a lossy chain by construction,
regardless of how good the summarizer is. The context engine eliminates that handoff: context
delivery is library code, not an agent call, so the reasoning agent sees the full relevant context,
not a compressed version of it. Under equal compute, a single well-contextualized agent matches or
beats a multi-agent pipeline on every tested model family (Tran & Kiela, Stanford 2026,
arXiv:2604.02460).

The runner orients an agent to the right place before it starts, across three complementary arms:

- **Docstring cards** (zero-dependency): a no-LLM initial pass that indexes the code's **own
  docstrings** (each file's written description), like the index at the back of a book.
- **Semantic vectors** over those summaries (optional Qdrant): "which area is this about?"
- **BM25 over actual content** (optional): "where does this exact identifier or phrase appear?"

The part worth pitching is the one that **keeps getting better on its own**: a **semantic memory of
past tasks and the context they used**. Every completed task records a `task -> context`
association (the task, the files it touched, a short summary), embedded so a **future similar task
retrieves it by meaning**. It compounds where work actually happens, never by summarizing the whole
corpus, and it ages honestly: associations are **recency-weighted** (a similar task from yesterday
outranks one from a year ago), **merged** instead of duplicated, and **capped** with oldest-first
eviction. The longer the system runs, the better it orients, with no manual indexing.

## Evaluation

Routing accuracy (target file in top-1 / top-3), 15 labeled tasks on a copy of this repo. The
big jump is from extracting the code's own **docstrings** for the summaries (free, no LLM):

| Retriever | top-1 | top-3 | notes |
|---|---|---|---|
| grep (term frequency) | 13% | 66% | weak baseline |
| keyword / IDF (symbol names only) | 13% | 53% | the old thin summaries |
| keyword / IDF (**docstring-rich**) | **53%** | **93%** | extract the code's own docstrings, no LLM |
| vector (`bge-small`, 384d) | 53% | 80% | zero-config local floor |
| vector (`bge-base`, 768d) | **60%** | **86%** | a larger local model, still no API key |
| **hybrid (vector + keyword)** | union recall **93%** | | both candidate sets, agent reviews and picks |

The embedder is a pluggable callable: a bigger local model (`bge-base`) already lifts vector
routing from 53/80 to 60/86 with no API key, and a SOTA API embedder lifts it further. The
quality dial is config, not a redesign.

Other measured properties:

| Metric | Value |
|---|---|
| Cold-start bootstrap | ~65 cards, ~240 ms, 0 LLM calls |
| Staleness / auto-update | precision 1.00, recall 1.00, 0 LLM calls; vectors re-embed only changed items |
| Tool-call rounds, Claude Code with a correct grounding vs alone | 1.0 vs 3.0 (3x fewer) |
| Correctness (cold / warm, per-sample LLM judge) | 100% / 100%, 5/5 adversarial never misled |

**What the numbers say honestly.** The summaries are what matter: indexing the code's own
docstrings took keyword routing from 13% / 53% to **53% / 93%** for free, and hybrid surfaces the
right file **93%** of the time. The vector arm is a small-model floor (`bge-small`); a SOTA API
embedder, or a larger one, raises it, and the embedder is a pluggable callable. With the right
grounding surfaced, Claude Code confirms in 1 tool round instead of 3, correctness never regresses,
and a confidence gate falls back to plain Claude Code when nothing is confident, so the layer never
makes a run worse, and the task-memory makes it better over time.

Full methodology, dataset, and honest limitations: see [evaluation/README.md](evaluation/README.md).
Re-run: `python evaluation/eval_deterministic.py` (free) and
`python evaluation/eval_vs_claude_code.py` (spends a little Haiku token, runs on a repo copy).

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) and
[CLAUDE.md](CLAUDE.md) first — the one rule that matters most: **keep the library generic**
(no org names, ids, keys, emails, or absolute paths in the package). By participating you agree to
the [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Found a vulnerability? Please report it privately — see [SECURITY.md](SECURITY.md). Do not open a
public issue for security problems.

## License

Licensed under the [Apache License 2.0](LICENSE). Copyright © Spiritual Data.
