# Components

Generated from `esoi/components.csv` by `esoi check --write-docs`.
Edit the CSV, never this file. The rules are listed by `esoi rules`.

| Component | Layer | Role | Serves |
|---|---|---|---|
| `core-ports` | core | The generic boundary: adapter interfaces, value objects, streaming sinks, event constants | show-progress |
| `core-orchestrator-loop` | core | The bounded plan, gather, re-plan, answer/deep/confirm loop, split into role mixins | route-a-message, answer-a-turn |
| `core-planner-prompts` | core | Prompt text and its rendering, kept out of the loop,  PLANNER_PROFILES holds variants | route-a-message |
| `core-routing-judges` | core | Small judges that rank, route or check a decision (reach, quest selection, overseer, sufficiency, explain) | watch-a-run, judge-a-decision |
| `core-model-selection` | core | Tier to live model id, family resolution, deep-run starting model, outage and usage-limit memory | choose-a-model-tier |
| `core-deep-run` | core | The /goal done-standard contract, deep-run budget, attachments, mid-run inbox, per-run control channel (steer, stop, pause), per-turn execution record | run-deep-work, receive-input-mid-run, detect-a-stalled-run |
| `core-context-selection` | core | Choosing and learning which context cards enter a prompt | select-context, guide-by-feedback, recall-recent-context |
| `core-tools` | core | The one catalog of operations an AI run can perform (tools, asks, progress, goals, notes, experiments), their relevance selection and per-run pins, plus operator-registered custom tools | operate-a-quest |
| `core-reply-guards` | core | Link honesty, replayable recipes and the anticipation learner | replay-known-operations, guard-a-reply, persist-predictions |
| `runner-quest-client` | runner | The Quest HTTP client in API-area mixins plus decision sink | escalate-to-a-human |
| `runner-poller` | runner | Event-driven, signature-deduped poll loop with bounded concurrency | run-queued-tasks |
| `runner-executor` | runner | Run one claimed task through the brain and report | run-queued-tasks |
| `runner-autopilot` | runner | The recurring autopilot pass: defaults, periods, personas, briefs, gates | gate-an-autopilot-pass, assemble-a-prompt, ground-in-intent |
| `runner-context-updates` | runner | Keeping a quest context up to date: bundle, receipts, state, judges, sources | assemble-a-prompt, feed-context-sources |
| `runner-personas` | runner | Which persona runs a task (data, not code), rep skill sync, workflow actions | run-queued-tasks |
| `runner-quest-folder` | runner | A quest as a working folder: index, two-way sync, zones, goals, experiments | run-queued-tasks |
| `runner-lane` | runner | The shared lane entry point and live two-way channel loop | run-queued-tasks |
| `runner-learning` | runner | What a person asked for and has said: ledger, insights, reflections | store-feedback |
| `adapters-model-providers` | adapters | Swappable ModelProvider implementations routed by model name | call-a-model |
| `adapters-retrieval` | adapters | RetrievalAdapter and writer implementations over files, DBs, MCP, Notion, Google, Quest | gather-evidence, write-files, write-through-mcp, talk-over-a-channel |
| `adapters-card-store` | adapters | Context card persistence, bootstrap, extraction, update and guidance stores (CardRepository) | persist-cards, resolve-references, cache-cards, detect-stale-cards |
| `adapters-vector-context` | adapters | Semantic and keyword context assemblers and their scoring helpers | index-by-meaning, select-context |
| `adapters-web` | adapters | Live web search and page fetch behind pluggable backends | search-the-web |
| `adapters-deep-runners` | adapters | Alternative DeepRunner implementations | run-deep-work |
| `adapters-conversations` | adapters | Claude session transcripts as context and ConversationStore | remember-conversations |
| `config` | config | RunnerConfig and build_orchestrator(): the one seam for consumer specifics | configure-a-lane |
| `cli-and-ui` | cli | Console entry point, attended chat session and Textual UI, card-store maintenance CLI | operate-from-a-terminal |
| `usage-and-resources` | core | Token usage and pricing, overload guard: leaf utilities with no package imports, so the runner may use them | track-usage-and-cost |
| `package-root` | config | Bootstrap email and the package root that wires core and runner to the config layer | expose-the-public-api |
| `tools-cli` | tools | python -m quest_ai_runner.tools: list and call standard tools, send email | operate-from-a-terminal |
| `prompts` | prompts | Standing deep-run instructions shipped as data (evolvable_structure.md and the public docs/evolvable-structure.md generated from the ESOI skill) | assemble-a-prompt, behave-by-esoi, judge-esoi-behavior, iterate-by-spec |
| `vendor` | vendor | Vendored third-party code, unmodified | expose-the-public-api |
| `eval-components` | evaluation | Per-component evaluation harness: one function per component, impls swappable, same dataset | verify-offline |
| `scripts` | scripts | Operator scripts, each the live operator of a solution: web-search bench (search-the-web), card quarantine (persist-cards), restart-if-idle (run-queued-tasks) | search-the-web, persist-cards, run-queued-tasks |
| `examples` | examples | Runnable reference consumers (env-driven, no real data) | learn-by-example |
| `tests` | tests | Offline test suite and shared fakes | verify-offline |
| `structure-registry` | docs | This registry, the solutions ontology, and the check that keeps them true | verify-offline, keep-secrets-out |
| `docs-and-meta` | docs | Documentation and repo-level project files | explain-qar |
| `core-selection` | core | Selection by name: resolve(solution_id, name) turns a name into the implementation declared beside its code, with the records as the only list and the snapshot for an installed copy, so switching an implementation is a data or config change | select-an-implementation |
| `core-consumer-helpers` | core | Plumbing every consumer needs and must not copy: one persistent background loop for sync adapters calling async I/O, and signature probes that drop keywords an older installed library lacks | expose-the-public-api |
