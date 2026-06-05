# Contributing to quest-ai-runner

Thanks for your interest in improving `quest-ai-runner`! This project is the domain-free
orchestrator **brain** (`core`) plus the queued-task **executor** (`runner`) for Quest AI tasks.
Contributions of all kinds are welcome — bug reports, docs, tests, new adapters, and features.

By participating you agree to abide by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Ground rules (please read first)

This library is deliberately **generic**. The single most important design rule:

> **No consumer-specific logic lives in `quest_ai_runner/`.** No org names, user ids, team ids,
> emails, API keys, or absolute filesystem paths — anywhere in the package, the tests, or the
> git history. Everything specific comes from a consumer's `RunnerConfig` (see `examples/`).

If your change would hardcode any of the above, route it through config/adapters instead. See
[`CLAUDE.md`](CLAUDE.md) for the full list of what must never be committed.

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[anthropic,dev]'
python -m pytest -q          # no network, no API key needed — uses stubs + a mock client
```

The test suite runs the core loop against a stub `ModelProvider` + stub `RetrievalAdapter`, and
the runner against a mock `QuestClient`, so it is fully offline.

## Making a change

1. **Open an issue first** for anything non-trivial, so we can agree on the approach.
2. Create a feature branch off `main`.
3. Keep changes focused and the diff small. Match the surrounding code's style and comment density.
4. **Add or update tests** for any behavior change. The bar: a reviewer can verify the change from
   a test, offline.
5. Update docs (`README.md`, docstrings, `examples/`) when behavior or the public API changes.
6. Add a line to `CHANGELOG.md` under the `Unreleased` heading.
7. Run `python -m pytest -q` and make sure it's green.
8. Open a pull request using the template; link the issue it closes.

## What we look for in review

- **The generic boundary holds** — no consumer specifics leaked into the library.
- **The four adapter interfaces** (`RetrievalAdapter`, `ModelProvider`, `DeepRunner`,
  `EscalationSink`) stay clean and minimal; new capabilities go behind an interface.
- **The frozen public API** in `quest_ai_runner.core` (`Mode`, `StreamSink`, `MilestoneSink`,
  `ProgressEvent`, `Orchestrator`) is not broken without a clear, discussed reason.
- Tests cover the change and run offline.

## Reporting bugs & requesting features

Use the GitHub issue templates. For anything security-sensitive, do **not** open a public issue —
see [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the
[Apache License 2.0](LICENSE), the same license that covers this project.
