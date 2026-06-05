# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`ClaudeCliProvider`** — a keyless `ModelProvider` that drives the local `claude` CLI headless,
  so the orchestrator's planner/answer calls run on the box's Claude Code **subscription login**
  with no `ANTHROPIC_API_KEY` (the deep-runner already ran keyless this way; now the whole runner
  can). The CLI can't force `tool_choice`, so `plan` instructs the model to emit the `decide`
  tool's JSON and parses it leniently (degrading to a safe `answer` on any failure). `list_models`
  returns `[]` so the `ModelRegistry` uses its fallback tier map; tier ids map to CLI family
  aliases. The spawned process has API-key / session env vars stripped (subscription only).

### Changed
- **CLI** — `QAR_MODEL_BACKEND` (`anthropic` | `claude_cli`) selects the model backend; when unset
  it auto-selects `claude_cli` (keyless) unless `ANTHROPIC_API_KEY` is present. The runner now
  works out of the box on a subscription login with no API key.

## [0.1.0] - 2026-06-05

Initial public release.

### Added
- **`core`** — the domain-free orchestrator brain: a bounded plan → gather → re-plan →
  answer/deep/confirm loop, with the frozen streaming interface (`Mode`, `ProgressEvent`,
  `StreamSink`, `MilestoneSink`) and the live↔background handoff.
- **`runner`** — the queued-task executor: an event-driven, signature-deduped poller; a claim
  step; an executor that maps results onto the Quest task callback; and a `QuestClient` covering
  discover / claim / report / escalate / loop-close.
- **The four adapter interfaces** — `RetrievalAdapter`, `ModelProvider`, `DeepRunner`,
  `EscalationSink` — plus reference implementations (`FilesAdapter`, `CachedDbAdapter`,
  `AnthropicProvider`, `SubprocessGoalRunner`, `QuestDecisionSink`).
- **`RunnerConfig`** + factory helpers and capability derivation (`web` / `corpus` / `code`).
- **`quest-ai-runner` CLI** — an env-driven console entry point for the poller.
- **Examples** (`examples/`) — a custom consumer, a runnable lane, and a live end-to-end demo.
- **Tests** — offline core-loop, model-registry, mode/streaming, runner (mock client), and
  example wiring tests.

[Unreleased]: https://github.com/SpiritualData/quest-ai-runner/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/SpiritualData/quest-ai-runner/releases/tag/v0.1.0
