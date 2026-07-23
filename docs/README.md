# quest-ai-runner documentation

Start here. These docs go from "run it in five minutes" to "implement your own adapters and deploy".

## Tutorials

- **[Quickstart](quickstart.md)** — install the library, ground the brain in-process, then run the
  executor lane against a Quest backend.

## How-to guides

- **[Writing a consumer](writing-a-consumer.md)** — wire the generic library to *your* Quest
  backend, corpus, and persona via `RunnerConfig`.
- **[Implementing adapters](adapters.md)** — the four interfaces (`RetrievalAdapter`,
  `ModelProvider`, `DeepRunner`, `EscalationSink`) and how to build your own.
- **[Deployment](deployment.md)** — run the poller under cron or systemd.
- **[Corpus playbooks](corpus-playbooks.md)** — distill a corpus's history into playbook files the
  shallow loop (context cards) and Claude Code deep runs both pick up automatically.

## Explanation / reference

- **[Architecture Standards](ARCHITECTURE_STANDARDS.md)** — the brain / runner / adapters split, why
  `core` is called `core`, and the standards code here must follow.
- **[Streaming & modes](streaming-and-modes.md)** — LIVE vs BACKGROUND, the `ProgressSink`
  discipline, and the live↔background handoff.
- **[Quest API contract](quest-api-contract.md)** — the exact endpoints the runner speaks.
- **[The anticipation engine](anticipation.md)** — learned ask patterns, the objective function and
  online EMA learning, precomputed context, and the opt-in `QAR_ANTICIPATION` flag.

## See also

- [`examples/`](../examples/) — runnable reference consumers.
- [CONTRIBUTING.md](../CONTRIBUTING.md) and [CLAUDE.md](../CLAUDE.md) — how to contribute, and the
  rules that keep the library generic.
