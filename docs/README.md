# quest-ai-runner documentation

Start here. These docs go from "run it in five minutes" to "implement your own adapters and deploy".

## Tutorials

- **[Quickstart](quickstart.md)** — install the library, ground the brain in-process, then run the
  executor lane against a Quest backend.
- **[Your first lane](tutorial-your-first-lane.md)** — from nothing to a working executor lane, and
  what a consumer is NOT supposed to own any more (the CLI/loop shape, `.env` loading, persona
  resolution — those are library concerns; see [`examples/minimal_lane.py`](../examples/minimal_lane.py)).

## How-to guides

- **[Writing a consumer](writing-a-consumer.md)** — wire the generic library to *your* Quest
  backend, corpus, and persona via `RunnerConfig`.
- **[Implementing adapters](adapters.md)** — the four interfaces (`RetrievalAdapter`,
  `ModelProvider`, `DeepRunner`, `EscalationSink`) and how to build your own.
- **[Personas](personas.md)** — run each task AS somebody without writing a resolver: the persona
  registry (both file shapes), the four resolution steps (structured field, LLM-judged explicit
  ask, domain-card dominance, structural fallback), auto-registering an unknown persona as a real
  skill, and why a bare name mention must never activate one.
- **[The ACP deep runner](acp-deep-runner.md)** — the opt-in `DeepRunner` that drives Claude over
  the Agent Client Protocol instead of a one-shot `claude -p`, so a message queued mid-run reaches
  the turn already in progress. How to wire it, the Node >= 22 requirement, and why the default
  path is unchanged.
- **[The fast edit runner](fast-edit-runner.md)** — the opt-in `DeepRunner` that lands a bounded
  file edit in one model call instead of spawning a full agent, and with it quest-ai-runner's
  first write capability: how the opt-in works, what the write boundary guarantees (containment,
  secret refusal, backups), when it escalates to the full deep runner, and the vendored
  SEARCH/REPLACE matcher's attribution.
- **[Deployment](deployment.md)** — run the poller under cron or systemd.
- **[Corpus playbooks](corpus-playbooks.md)** — distill a corpus's history into playbook files the
  shallow loop (context cards) and Claude Code deep runs both pick up automatically.
- **[Guidance cards](guidance-cards.md)** — standing rules that are retrieved per message instead
  of pasted into one always-on prompt: the card format and tag vocabulary, how selection scores and
  scopes them (rep, team, org, global), serving one rules base to many machines from a host
  database, and turning human corrections into cards automatically.

## Explanation / reference

- **[Architecture Standards](ARCHITECTURE_STANDARDS.md)** — the brain / runner / adapters split, why
  `core` is called `core`, and the standards code here must follow.
- **[Streaming & modes](streaming-and-modes.md)** — LIVE vs BACKGROUND, the `ProgressSink`
  discipline, and the live↔background handoff.
- **[Quest API contract](quest-api-contract.md)** — the exact endpoints the runner speaks.
- **[The anticipation engine](anticipation.md)** — learned ask patterns, the objective function and
  online EMA learning, precomputed context, and the opt-in `QAR_ANTICIPATION` flag.
- **[Answer explanation](answer-explanation.md)** — the user-facing "Explain how I got this" panel:
  why it is a second call after the answer, the model-free eligibility gate, and which half of the
  payload is a record rather than prose.
- **[Personal lexicon](personal-lexicon.md)**: ranking one person's distinctive vocabulary by
  TF-DF-IDF, the two background sources and why they are combined with a minimum, and the
  `min_documents` safeguard that stops a mis-recognition feeding back into the recognizer.
- **[Terminal UX prior art](terminal-ux-prior-art.md)** — how Aider, Crush, Codex CLI, Gemini CLI,
  Claude Code, and others solve fixed-bottom input, streaming without scrollback corruption, log
  routing, and mid-turn queuing; which Textual primitives to use for each; and an evaluation of
  what's forkable/vendorable versus reference-only (incl. the ACP protocol for pluggable execution
  backends). Its `prompt_toolkit`/ANSI passages are prior art only: that renderer has since been
  removed and Textual is the one chat UI.

## See also

- [`examples/`](../examples/) — runnable reference consumers.
- [CONTRIBUTING.md](../CONTRIBUTING.md) and [CLAUDE.md](../CLAUDE.md) — how to contribute, and the
  rules that keep the library generic.
