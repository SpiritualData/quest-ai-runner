# Components

Generated from `docs/structure/components.csv` by `python scripts/check_structure.py --write-docs`.
Edit the CSV, never this file. Rules are the numbered ideals in `docs/structure/README.md`.

| Component | Layer | Role | Interface | Metric | Docs | Status |
|---|---|---|---|---|---|---|
| `core-ports` | core | The generic boundary: adapter interfaces, value objects, streaming sinks, event constants | Protocol/ABC ports (RetrievalAdapter, ModelProvider, DeepRunner, EscalationSink, ContextAssembler, ...) | tests/test_modes.py (event/sink contract) | docs/adapters.md, docs/ARCHITECTURE_STANDARDS.md | done |
| `core-orchestrator-loop` | core | The bounded plan, gather, re-plan, answer/deep/confirm loop, split into role mixins | Orchestrator.run(),  new methods go in the mixin that owns the role | evaluation/components/run.py classification --impl qar_planner | CLAUDE.md, docs/ARCHITECTURE_STANDARDS.md | done |
| `core-planner-prompts` | core | Prompt text and its rendering, kept out of the loop,  PLANNER_PROFILES holds variants | PLANNER_PROFILES entry,  PromptLayers | evaluation/components/run.py classification --impl qar_planner | CLAUDE.md, docs/cheap-model-routing.md | done |
| `core-routing-judges` | core | Small judges that rank, route or check a decision (reach, quest selection, overseer, sufficiency, explain) | one judge function per question, tier-resolved model | evaluation/overseer_signals_eval.py (overseer),  others: none yet | docs/overseer.md, docs/answer-explanation.md | partial |
| `core-model-selection` | core | Tier to live model id, family resolution, deep-run starting model, outage and usage-limit memory | ModelRegistry.resolve_tier() | tests/test_deep_difficulty_model_selection.py | docs/cheap-model-routing.md | done |
| `core-deep-run` | core | The /goal done-standard contract, deep-run budget, attachments, mid-run inbox, per-turn execution record | GoalRunner / SubprocessGoalRunner | evaluation/deep_review_eval.py | docs/acp-deep-runner.md, docs/deep-run-continuation.md | partial |
| `core-context-selection` | core | Choosing and learning which context cards enter a prompt | ContextAssembler implementations + card_filter | evaluation/components/run.py context_selection --impl hybrid_union | docs/context-assembly.md, docs/guidance-cards.md | done |
| `core-tools` | core | The one catalog of operations an AI run can perform (tools, asks, progress, goals, notes, experiments), their relevance selection and per-run pins, plus operator-registered custom tools | standard catalog + register_tool | evaluation/components/run.py quest_ops --impl qar_orchestrator | docs/tools.md | done |
| `core-reply-guards` | core | Link honesty, replayable recipes and the anticipation learner | link_guard / recipes / anticipation engine | evaluation/components/run.py recipe_fast_path --impl recipe_replay | docs/link-guard.md, docs/recipes.md, docs/anticipation.md | partial |
| `runner-quest-client` | runner | The Quest HTTP client in API-area mixins plus decision sink | QuestClient | tests/test_quest_client_*.py | CLAUDE.md, docs/quest-api-contract.md | done |
| `runner-poller` | runner | Event-driven, signature-deduped poll loop with bounded concurrency | Poller | tests/test_runner.py | CLAUDE.md, docs/deployment.md | done |
| `runner-executor` | runner | Run one claimed task through the brain and report | Executor | tests/test_runner.py | CLAUDE.md | done |
| `runner-autopilot` | runner | The recurring autopilot pass: defaults, periods, personas, briefs, gates | autopilot pass task | tests/test_autopilot*.py | CLAUDE.md, docs/a-lane-is-a-config-file.md | partial |
| `runner-context-updates` | runner | Keeping a quest context up to date: bundle, receipts, state, judges, sources | context_updates engine | tests/test_context_updates*.py | docs/context-updates.md | partial |
| `runner-personas` | runner | Which persona runs a task (data, not code), rep skill sync, workflow actions | personas resolver,  RunnerConfig field | tests/test_persona_resolution.py | docs/personas.md | done |
| `runner-quest-folder` | runner | A quest as a working folder: index, two-way sync, zones, goals, experiments | quest_folder_sync / quest_goal_sync | tests/test_quest_folder_*.py | docs/quest-folder-goals.md, docs/quest-folder-zones.md | done |
| `runner-lane` | runner | The shared lane entry point and live two-way channel loop | lane.main / ChannelRunner | tests/test_lane.py | docs/tutorial-your-first-lane.md, docs/live-channels.md | done |
| `runner-learning` | runner | What a person asked for and has said: ledger, insights, reflections | feedback ledger | tests/test_feedback_ledger*.py | docs/feedback-ledger.md | done |
| `adapters-model-providers` | adapters | Swappable ModelProvider implementations routed by model name | MultiProvider | tests/test_multi_provider_tier_fallback.py | docs/adapters.md | partial |
| `adapters-retrieval` | adapters | RetrievalAdapter and writer implementations over files, DBs, MCP, Notion, Google, Quest | RetrievalAdapter (read_section/grep/query) | tests/test_composite_retrieval.py | docs/adapters.md, docs/multi-source-retrieval.md | done |
| `adapters-card-store` | adapters | Context card persistence, bootstrap, extraction, update and guidance stores (CardRepository) | CardRepository / FileContextStore | tests/test_card_store_merge_and_remote.py | docs/context-card-stores.md, docs/card-schema.md | done |
| `adapters-vector-context` | adapters | Semantic and keyword context assemblers and their scoring helpers | VectorStore / ContextAssembler | evaluation/components/run.py context_selection --impl vector_bge_small | docs/vector-context.md, docs/TF_DF_IDF_SAMPLING.md | done |
| `adapters-web` | adapters | Live web search and page fetch behind pluggable backends | SearchBackend / WebResearchAdapter | scripts/web_search_bench.py | docs/web-search.md | partial |
| `adapters-deep-runners` | adapters | Alternative DeepRunner implementations | DeepRunner | tests/test_fast_edit_runner.py | docs/acp-deep-runner.md, docs/fast-edit-runner.md | done |
| `adapters-conversations` | adapters | Claude session transcripts as context and ConversationStore | ConversationStore | tests/test_conversation_format.py | docs/claude-conversations-integration.md, docs/conversation-cards.md | done |
| `config` | config | RunnerConfig and build_orchestrator(): the one seam for consumer specifics | RunnerConfig | tests/test_config_env_vars.py | CLAUDE.md, docs/writing-a-consumer.md | done |
| `cli-and-ui` | cli | Console entry point, attended chat session and Textual UI, card-store maintenance CLI | quest-ai-runner console script | tests/test_cli_*.py | docs/chat.md, docs/quickstart.md | done |
| `usage-and-resources` | config | Token usage and pricing, overload guard, bootstrap email, package root | usage tracker | tests/test_usage.py | docs/deployment.md | done |
| `tools-cli` | tools | python -m quest_ai_runner.tools: list and call standard tools, send email | tools CLI | tests/test_tools.py | docs/tools.md | done |
| `prompts` | prompts | Standing deep-run instructions shipped as data (adaptable_structure.md) | QAR_DEEP_INSTRUCTIONS_FILES | none yet (see gap) | docs/acp-deep-runner.md | partial |
| `vendor` | vendor | Vendored third-party code, unmodified | import path | exempt: upstream code, pinned and not edited here | CLAUDE.md | done |
| `eval-components` | evaluation | Per-component evaluation harness: one function per component, impls swappable, same dataset | evaluation/components/run.py <component> --impl <name> | self (offline test_components.py) | evaluation/components/README.md | done |
| `eval-qualitative` | evaluation | End-to-end qualitative harness against a dev Quest world with datasets and an LLM judge | evaluation/qualitative/runner.py | judge verdicts per dataset | evaluation/qualitative/README.md | done |
| `eval-legacy-scripts` | evaluation | Older standalone eval scripts that predate the components harness | one script each | each prints its own README-documented numbers | evaluation/README.md | partial |
| `scripts` | scripts | Operator scripts: card quarantine, restart-if-idle, web-search bench | CLI scripts | exempt: operator utilities,  each has --help | docs/deployment.md | done |
| `examples` | examples | Runnable reference consumers (env-driven, no real data) | examples/README.md | tests/test_examples.py | examples/README.md, docs/writing-a-consumer.md | done |
| `tests` | tests | Offline test suite and shared fakes | pytest per file | the offline suite itself (run tests per file,  see CLAUDE.md) | CLAUDE.md | done |
| `structure-registry` | docs | This registry, the solutions ontology, and the check that keeps them true | python scripts/check_structure.py | check exits 0 | docs/structure/README.md | done |
| `docs-and-meta` | docs | Documentation and repo-level project files | docs/README.md index | exempt: prose,  checked for existence by S2 | docs/README.md | done |

## Open gaps

* `core-routing-judges` (partial): Only the overseer has a metric script; reach/quest/sufficiency judges have none. Action: Add a component eval per judge under evaluation/components/ (see solutions.csv)
* `core-deep-run` (partial): Metric is a legacy script outside the component harness; no recorded result. Action: Port deep_review_eval.py to evaluation/components/deep_run/ and record.
* `core-reply-guards` (partial): Its eval (recipe_fast_path) is ported to the component harness but has never been run against a live model, so no result is recorded. Action: Run evaluation/components/run.py recipe_fast_path --impl recipe_replay --record on a host with the claude CLI.
* `runner-autopilot` (partial): Its prompts are assembled here with no measured size or duplication limit; the 2026-10-06 838K-character prompt (16 duplicated context-update blocks) came from this path, so it is not an invariant. Action: Measure it under the assemble-a-prompt solution (max and p95 prompt characters and duplicate blocks per assembly point), then restore a metric kind.
* `runner-context-updates` (partial): Its prompts are assembled here with no measured size or duplication limit; the 2026-10-06 838K-character prompt (16 duplicated context-update blocks) came from this path, so it is not an invariant. Action: Measure it under the assemble-a-prompt solution (max and p95 prompt characters and duplicate blocks per assembly point), then restore a metric kind.
* `adapters-model-providers` (partial): No metric compares providers or tiers on the same cases (cost, latency, pass rate). Action: Add a call-a-model component eval (see solutions.csv call-a-model).
* `adapters-web` (partial): scripts/web_search_bench.py is a script, not a recorded component result. Action: Port web_search_bench.py to evaluation/components/web_search/ and record.
* `prompts` (partial): No metric on prompt size or duplication, the failure behind the 2026-10-06 838K-character prompt. Action: Add an assemble-a-prompt component eval: max and p95 prompt characters per assembly point on a fixed fixture, using core/prompt_budget.py.
* `eval-legacy-scripts` (partial): Five scripts (card_quality, chat_quest_ops_routing, deep_review, eval_vs_claude_code, overseer_signals) are not behind the component interface, so their implementations cannot be swapped and results are not comparable. eval_deterministic and recipe_fast_path are now thin wrappers over evaluation/components/. Action: Port each to evaluation/components/<component>/ when next touched (targets are named in solutions.csv).
