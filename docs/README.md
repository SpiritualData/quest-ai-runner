# quest-ai-runner documentation

Start here. These pages follow the [Diataxis](https://diataxis.fr) framework: every page sits in exactly one of four folders, and the repository wiki is generated from this folder (edit pages here, never on the wiki; `esoi docs check` enforces the layout).

## Tutorials

Learn by doing; each one ends with something working.

- **[Quickstart](tutorials/quickstart.md)**: install the library, ground the brain in-process, then run the
  executor lane against a Quest backend.
- **[Your first lane](tutorials/tutorial-your-first-lane.md)**: from nothing to a working executor lane, and
  what a consumer is NOT supposed to own any more (the CLI/loop shape, `.env` loading, persona
  resolution: those are library concerns; see [`examples/minimal_lane.py`](../examples/minimal_lane.py)).
- **[Run it in the terminal against a folder in ten minutes](tutorials/terminal-chat-in-ten-minutes.md)**: install, point the chat at a folder of your notes, ask a question, leave and resume the conversation. No Quest account needed.
- **[Run it in the terminal against a folder in ten minutes](tutorials/terminal-chat-in-ten-minutes.md)**: install, point the chat at a folder of your notes, ask a question, leave and resume the conversation. No Quest account needed.

## How-to guides

Steps for a task you already know you want to do.

- **[The terminal chat](how-to/chat.md)**: talk to the brain in your terminal. Resume past
  conversations (`--resume`, `--continue`), use the `/` command menu, choose the quest a
  conversation is about, and the full list of commands and keys.

## How-to guides
- **[Writing a consumer](how-to/writing-a-consumer.md)**: wire the generic library to *your* Quest
  backend, corpus, and persona via `RunnerConfig`; also covers `decision_assignees` (named
  decision-routing roles) and `load_client`, the lightweight sibling of `load_config` for a
  script, notebook, or one-off lookup that only needs a `QuestClient` (see also the README's
  "Scripting the Quest API" and the `quest-ai-runner quest <method>` CLI subcommand).
- **[Keeping Quest AI Runner current with the hub](how-to/keep-quest-ai-runner-current-with-the-hub.md)**: check the hub's requirements with `doctor`, and notice and resolve a hub update when you lock selections (for an AI agent or a person).
- **[Running a priority dataset on production, safely](how-to/run-a-priority-dataset.md)**: run any chat dataset on the production brain with every write blocked and recorded, and keep one sheet of the latest result per environment.
- **[Keeping the repository clean](how-to/public-hygiene.md)**: the scan that keeps secrets, real machine
  paths and real addresses out of a public repository.
- **[Implementing adapters](how-to/adapters.md)**: the four interfaces (`RetrievalAdapter`,
  `ModelProvider`, `DeepRunner`, `EscalationSink`) and how to build your own.
- **[Digesting a large collection for one need](how-to/digest-a-large-collection.md)**: read a big body of records with cheap sub-analyses and hand the planner a few cited findings (the `digest-records-for-a-need` solution), with a cache, caps and the status of every group.
- **[Direct tools](how-to/tools.md)**: actions the brain calls in one planner step with no deep run
  (standard `send_quest_email`, plus your own tools declared in TOML with when to use and when
  not to), shown only when relevant to the request, and callable from a deep run by shell.
- **[Personas](how-to/personas.md)**: run each task AS somebody without writing a resolver: the persona
  registry (both file shapes), the four resolution steps (structured field, LLM-judged explicit
  ask, domain-card dominance, structural fallback), auto-registering an unknown persona as a real
  skill, and why a bare name mention must never activate one.
- **[The ACP deep runner](how-to/acp-deep-runner.md)**: the opt-in `DeepRunner` that drives Claude over
  the Agent Client Protocol instead of a one-shot `claude -p`, so a message queued mid-run reaches
  the turn already in progress. How to wire it, the Node >= 22 requirement, and why the default
  path is unchanged.
- **[The stream-json deep runner](how-to/stream-json-runner.md)**: the alternative `run-deep-work`
  implementation that keeps one live Claude Code session per run, so a person can steer, stop or
  pause while it works. The control channel, the exactly-once rules, the Claude Code version it
  needs, and why the one-shot runner is still the default.
- **[The fast edit runner](how-to/fast-edit-runner.md)**: the opt-in `DeepRunner` that lands a bounded
  file edit in one model call instead of spawning a full agent, and with it quest-ai-runner's
  first write capability: how the opt-in works, what the write boundary guarantees (containment,
  secret refusal, backups), when it escalates to the full deep runner, and the vendored
  SEARCH/REPLACE matcher's attribution.
- **[Automated context updates](how-to/context-updates.md)**: one engine answering "what changed since an
  assistant last looked?" across notes, captures, reflections, and Drive comments: how a quest
  declares what it watches as data, why a narrowing spec never drops anything, the per-source
  watermarks, the receipt a run writes about what it actually used, and `quest-ai-runner context
  <quest_id>` (`collect_quest_context`), the read that answers "what is this quest's context right
  now" without running anything that consumes it.
- **[Deployment](how-to/deployment.md)**: run the poller under cron or systemd.
- **[Corpus playbooks](how-to/corpus-playbooks.md)**: distill a corpus's history into playbook files the
  shallow loop (context cards) and Claude Code deep runs both pick up automatically.
- **[Context card stores](how-to/context-card-stores.md)**: store identity, read-only mounting instead of
  copy-import, `context move-root` / `migrate-imports` / `merge-into` / `store-info`, read-only vectors for a mount on another Qdrant backend, the access model (what the
  process can read, plus the vector fence), the containment audit and the migration order.
- **[Guidance cards](how-to/guidance-cards.md)**: standing rules that are retrieved per message instead
  of pasted into one always-on prompt: the card format and tag vocabulary, how selection scores and
  scopes them (rep, team, org, global), serving one rules base to many machines from a host
  database, and turning human corrections into cards automatically.

## Explanation / reference
- **[A lane is a config file](how-to/a-lane-is-a-config-file.md)**: express an executor lane as a `qar.toml` (env files, aliases, folder maps, context sources) instead of a Python consumer.
- **[Multi-source retrieval](how-to/multi-source-retrieval.md)**: wire several adapters (files, databases, conversations) to be queried in parallel.
- **[Claude conversations](how-to/claude-conversations-integration.md)**: make your Claude conversations part of the corpus the brain reads.
- **[Deployment](how-to/deployment.md)**: cron, systemd, or `quest-ai-runner lane ensure` (no systemd, no root).
- **[File server](how-to/file-server.md)**: `quest-ai-runner serve-files`, a small read-only server so a brain on one machine can read files on another.
- **[Web search](how-to/web-search.md)**: `WebResearchAdapter`, fast token-efficient live web access for the orchestrator.
- **[Live channels](how-to/live-channels.md)**: an opt-in real-time, two-way chat lane over a messaging app.

## Explanation

Why it is built this way, and the design record behind each part.

- **[Live work trace ("Trail")](explanation/live-work-trace.md)**: the typed, reader-safe account of the work a turn did, its contract, privacy rule and build state
- **[Incremental replies and asks](explanation/incremental-replies-and-asks.md)**: say what is done, why more is coming and ask when blocked: the contract, the structural policy and the build state (shipped off)
- **[Hub of hubs](explanation/hub-of-hubs.md)**: why QAR builds on open-source hubs, how to add a tool server, agent,
  model vendor or search backend by configuration with no code, and the status of each area.
- **[The feedback ledger](explanation/feedback-ledger.md)**: what a person asked for and how far anybody got
  with it, across every channel, as recorded statuses rather than circumstance. Why replying is not
  doing, why a standing rule is never "done", how a run declares a disposition in its own receipt,
  and how a standing rule becomes a guidance card while the ledger keeps score of whether it is
  actually being followed.
- **[Continuing a deep run that ran out of turns](explanation/deep-run-continuation.md)**: why a worker that
  used its whole turn budget is continued in its own session with a bigger budget instead of being
  re-run from a cold start, what still stops it (the verifier, the token budget), and how an
  unfinished run reports the work it did.
- **[Streaming & modes](explanation/streaming-and-modes.md)**: LIVE vs BACKGROUND, the `ProgressSink`
  discipline, and the live↔background handoff.
- **[The anticipation engine](explanation/anticipation.md)**: learned ask patterns, the objective function and
  online EMA learning, precomputed context, and the opt-in `QAR_ANTICIPATION` flag.
- **[Link guard](explanation/link-guard.md)**: how every link in a reply is verified before it is sent
  (external URLs fetched, in-app paths matched against the host app's real route table), why
  "could not verify" is treated like "does not exist", and what the policy file configures.
- **[Answer explanation](explanation/answer-explanation.md)**: the user-facing "Explain how I got this" panel:
  why it is a second call after the answer, the model-free eligibility gate, and which half of the
  payload is a record rather than prose.
- **[The metric record of a deep goal](explanation/metric-results.md)**: how "what the relevant metric was before and
  after your change" is kept by code instead of claimed in prose: the ledger, the `record_metric_result` tool, the
  `metrics` payload every consumer reads, the one-request gate for a change that has no result, and the
  `iterate-by-spec` arms that add a slice of the specification and the runner's own readings.
- **[Personal lexicon](explanation/personal-lexicon.md)**: ranking one person's distinctive vocabulary by
  TF-DF-IDF, the two background sources and why they are combined with a minimum, and the
  `min_documents` safeguard that stops a mis-recognition feeding back into the recognizer.
- **[Terminal UX prior art](explanation/terminal-ux-prior-art.md)**: how Aider, Crush, Codex CLI, Gemini CLI,
  Claude Code, and others solve fixed-bottom input, streaming without scrollback corruption, log
  routing, and mid-turn queuing; which Textual primitives to use for each; and an evaluation of
  what's forkable/vendorable versus reference-only (incl. the ACP protocol for pluggable execution
  backends). Its `prompt_toolkit`/ANSI passages are prior art only: that renderer has since been
  removed and Textual is the one chat UI.
- **[QAR anatomy](explanation/anatomy.md)**: the parts of a mind mapped to the code that plays them, and the outside hubs QAR builds on (the parts table is generated).
- **[Context assembly](explanation/context-assembly.md)**: guaranteed, reusable context for every run, and [what a context card is](explanation/context-assembly.md#what-a-context-card-is).
- **[Vector context](explanation/vector-context.md)**: the semantic orientation layer added beside the keyword store.
- **[Selecting intent specifications](explanation/spec-selection.md)**: how a request is matched to the problems, ideals, solutions and metrics that concern it.
- **[Conversation cards](explanation/conversation-cards.md)**: turning conversations into context cards the planner can discover.
- **[Edited files extraction](explanation/edited-files-extraction.md)**: how the files a deep run changed are found, so their cards can be updated.
- **[Recipes](explanation/recipes.md)**: replaying a tool call that already succeeded for the same kind of request.
- **[The overseer](explanation/overseer.md)**: minimal-intervention watching of a run for risk.
- **[Cheap model routing](explanation/cheap-model-routing.md)**: running the planner reliably on a cheap model, with the measured history.
- **[Quest folder goals](explanation/quest-folder-goals.md)**: `GOALS.md`, a quest's plan in its folder, editable both ways.
- **[Quest folder zones](explanation/quest-folder-zones.md)**: keeping "who decided this?" answerable in a quest folder.
- **[Bootstrap token savings](explanation/BOOTSTRAP_TOKEN_SAVINGS.md)**: background analysis of the optimized bootstrap.
- **[TF-DF-IDF sampling](explanation/TF_DF_IDF_SAMPLING.md)**: background on smarter context selection.
- **[Lessons from Claude Code](explanation/lessons-from-claude-code.md)**: patterns from a review of Claude Code's source.
- **[State store verification](explanation/statestore-verification.md)**: a verification record of duplicate-event prevention in the state store.

## Reference

Facts to look up.

- **[Architecture Standards](reference/ARCHITECTURE_STANDARDS.md)**: the brain / runner / adapters split, why
  `core` is called `core`, and the standards code here must follow.
- **[Quest API contract](reference/quest-api-contract.md)**: the exact endpoints the runner speaks.
- **[Consumer registry](reference/consumer-registry.md)**: the one CSV a system built on QAR keeps to list every integration point, and the check that verifies it.
- **[Environment variables](reference/configuration.md)**: every variable the code reads (generated by `esoi check --write-docs`).
- **[Evolvable structure](reference/evolvable-structure.md)**: the standing instructions deep runs follow (generated from the skill).
- **[Structure registry](../esoi/README.md)**: the checked inventory of every component (`components.csv`), every swappable solution with its metric (`solutions.csv`), the waivers, and `esoi check` that keeps them true.

## See also

- [`examples/`](../examples/): runnable reference consumers.
- [CONTRIBUTING.md](../CONTRIBUTING.md) and [CLAUDE.md](../CLAUDE.md): how to contribute, and the
  rules that keep the library generic.
