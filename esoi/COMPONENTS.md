# Components

Generated from `esoi/components.csv` by `esoi check --write-docs`.
Edit the CSV, never this file. The rules are listed by `esoi rules`.

| Component | Handle | Layer | Role | Serves |
|---|---|---|---|---|
| the-body-configuration | `the-body-configuration` | body | QAR's code for configuration (the body) | system-integrity, cards-from-files, card-store, quest-requests, delegated-agent-sessions, quest-context, context-retriever, intake-sources, file-editing, quest-change-executor, persist-cards |
| the-body-hands-quest-operations | `the-body-hands-quest-operations` | body | QAR's code for hands / quest operations (the body) | quest-change-executor, quest-requests, quest-representatives |
| the-body-host | `the-body-host` | body | QAR's code for host (the body) | model-plumbing |
| the-body-intake-sources | `the-body-intake-sources` | body | QAR's code for intake sources (the body) | context-intake, quest-requests, delegated-agent-sessions, quest-context, intake-sources, file-editing, quest-change-executor, feed-context-sources |
| the-body-internal-context | `the-body-internal-context` | body | QAR's code for internal context (the body) | quest-work-shown, cache-cards, persist-cards, index-by-meaning |
| the-body-internal-context-card-store | `the-body-internal-context-card-store` | body | QAR's code for internal context / card store (the body) | cards-from-files, card-store, feedback-learning, context-retriever, long-term-memory, quest-context, cache-cards, persist-cards, index-by-meaning |
| the-body-internal-context-quest | `the-body-internal-context-quest` | body | QAR's code for internal context / quest (the body) | quest-body, the-tools, quest-context, quest-representatives, quest-change-executor, quest-requests, intake-sources, quest-account-truth, file-editing |
| the-body-internal-context-quest-client | `the-body-internal-context-quest-client` | body | QAR's code for internal context / quest / client (the body) | quest-decision-requests |
| the-body-internal-context-run-store | `the-body-internal-context-run-store` | body | QAR's code for internal context / run store (the body) | metric-translation, quest-requests |
| the-consciousness-feedback-learning | `the-consciousness-feedback-learning` | consciousness | QAR's code for feedback learning (the consciousness) | cards-from-files, card-store, quest-context, context-retriever, feedback-learning |
| the-consciousness-judgment-calls | `the-consciousness-judgment-calls` | consciousness | QAR's code for judgment calls (the consciousness) | metric-translation, quest-work-shown, goal-completion, nervous-system, quest-requests |
| the-consciousness-representatives | `the-consciousness-representatives` | consciousness | QAR's code for representatives (the consciousness) | quest-requests, quest-representatives |
| the-consciousness-request-intake | `the-consciousness-request-intake` | consciousness | QAR's code for request intake (the consciousness) | metric-translation, system-integrity, quest-requests, quest-work-shown |
| the-consciousness-terminal | `the-consciousness-terminal` | consciousness | QAR's code for terminal (the consciousness) | system-integrity, quest-work-shown, show-progress |
| the-intelligence-critical-thinking | `the-intelligence-critical-thinking` | intelligence | QAR's code for critical thinking (the intelligence) | critical-thinking, delegated-agent-sessions, metric-translation, quest-autopilot, guidance, response-quality, run-applies-esoi, goal-completion, spec-selection, detect-a-stalled-run |
| the-intelligence-critical-thinking-esoi-application | `the-intelligence-critical-thinking-esoi-application` | intelligence | QAR's code for critical thinking / esoi application (the intelligence) | context-intake, quest-autopilot, guidance, quest-representatives, quest-change-executor, context-retriever, spec-selection |
| the-intelligence-critical-thinking-goal-completion | `the-intelligence-critical-thinking-goal-completion` | intelligence | QAR's code for critical thinking / goal completion (the intelligence) | quest-self, quest-work-shown, planner, quest-account-truth |
| the-intelligence-critical-thinking-metric-translation | `the-intelligence-critical-thinking-metric-translation` | intelligence | QAR's code for critical thinking / metric translation (the intelligence) | metric-translation, run-applies-esoi, quest-change-executor, quest-requests |
| the-intelligence-critical-thinking-response-quality | `the-intelligence-critical-thinking-response-quality` | intelligence | QAR's code for critical thinking / response quality (the intelligence) | system-integrity, response-quality, intuition, planner |
| the-intelligence-critical-thinking-decision-check | `the-intelligence-critical-thinking-decision-check` | intelligence | QAR's code for critical thinking / decision check (the intelligence) | decision-check, response-quality |
| the-intelligence-higher-mind-guidance | `the-intelligence-higher-mind-guidance` | intelligence | QAR's code for higher mind / guidance (the intelligence) | cards-from-files, card-store, feedback-learning, long-term-memory, quest-context, context-retriever, quest-work-shown, guide-by-feedback |
| the-intelligence-higher-mind-long-term-memory | `the-intelligence-higher-mind-long-term-memory` | intelligence | QAR's code for higher mind / long term memory (the intelligence) | system-integrity, feedback-learning, long-term-memory, context-retriever, recall-recent-context |
| the-intelligence-higher-mind-objectives | `the-intelligence-higher-mind-objectives` | intelligence | QAR's code for higher mind / objectives (the intelligence) | context-intake, quest-work-shown, quest-autopilot, guidance, feedback-learning, quest-representatives, quest-intents, goal-completion, model-plumbing, nervous-system, intuition, long-term-memory, planner, quest-requests, context-retriever, the-mind, the-subconscious |
| the-intelligence-mind | `the-intelligence-mind` | intelligence | QAR's code for mind (the intelligence) | quest-self, system-integrity, quest-work-shown, planner, quest-account-truth, receive-input-mid-run, describe-an-attachment, detect-a-stalled-run, reply-in-steps, replay-known-operations |
| the-intelligence-mind-context-intake | `the-intelligence-mind-context-intake` | intelligence | QAR's code for mind / context intake (the intelligence) | quest-self, quest-requests, delegated-agent-sessions, response-quality, feedback-learning, guidance, quest-intents, model-plumbing, quest-work-shown, intuition, long-term-memory, planner, context-retriever, quest-account-truth, the-mind, the-subconscious, describe-an-attachment |
| the-intelligence-mind-delegation | `the-intelligence-mind-delegation` | intelligence | QAR's code for mind / delegation (the intelligence) | quest-self, quest-requests, delegated-agent-sessions, metric-translation, quest-work-shown, feedback-learning, goal-completion, nervous-system, long-term-memory, planner, context-retriever, quest-account-truth, receive-input-mid-run, detect-a-stalled-run |
| the-intelligence-mind-planner | `the-intelligence-mind-planner` | intelligence | QAR's code for mind / planner (the intelligence) | quest-self, quest-work-shown, system-integrity, response-quality, guidance, quest-intents, goal-completion, model-plumbing, nervous-system, intuition, planner, quest-requests, quest-account-truth, the-mind, the-subconscious |
| the-intelligence-mind-request-router | `the-intelligence-mind-request-router` | intelligence | Reads the route of a request (information, act, draft or clarify) off the planner's decision by structure: Quest's arm of the hub's Route a Request. | route-a-request |
| the-intelligence-mind-reference-resolver | `the-intelligence-mind-reference-resolver` | intelligence | Offers the quest's people and goals to the hub's Resolve a Reference and gives its answer back in the shape each call site uses, with an abstain where nothing is meant. | resolve-a-reference |
| the-intelligence-mind-response-writer | `the-intelligence-mind-response-writer` | intelligence | QAR's code for mind / response writer (the intelligence) | quest-self, quest-work-shown, response-quality, goal-completion, nervous-system, intuition, planner, quest-account-truth, reply-in-steps |
| the-intelligence-nervous-system | `the-intelligence-nervous-system` | intelligence | QAR's code for nervous system (the intelligence) | nervous-system |
| the-intelligence-proactivity | `the-intelligence-proactivity` | intelligence | QAR's code for proactivity (the intelligence) | context-intake, quest-autopilot, guidance, quest-representatives, quest-requests, proactivity, prioritization |
| the-intelligence-subconscious-cards-from-files | `the-intelligence-subconscious-cards-from-files` | intelligence | QAR's code for subconscious / cards from files (the intelligence) | system-integrity, cards-from-files, card-store, quest-context, context-retriever |
| the-intelligence-subconscious-context-retriever | `the-intelligence-subconscious-context-retriever` | intelligence | QAR's code for subconscious / context retriever (the intelligence) | quest-self, cards-from-files, card-store, context-retriever, feedback-learning, quest-work-shown, long-term-memory, quest-context, planner, quest-account-truth, search-cards-and-specs |
| the-intelligence-subconscious-context-digest | `the-intelligence-subconscious-context-digest` | intelligence | QAR's code for subconscious / digest by need (the intelligence) | digest-records-for-a-need, model-plumbing |
| the-intelligence-subconscious-intuition | `the-intelligence-subconscious-intuition` | intelligence | QAR's code for subconscious / intuition (the intelligence) | response-quality, intuition, planner |
| the-self | `the-self` | self | QAR's code for  (the self) | quest-self, system-integrity, quest-work-shown, guide-toward-the-long-term-goal |
| the-self-assembler | `the-self-assembler` | self | QAR's code for assembler (the self) | quest-self, system-integrity |
| the-self-role-understanding | `the-self-role-understanding` | self | QAR's own names (QAR) and the placing of the names a request holds (the hub does the placing) | quest-self, system-integrity |
| the-self-integrity-prompts | `the-self-integrity-prompts` | self | QAR's code for integrity / prompts (the self) | guidance, quest-intents, model-plumbing, planner, the-mind, the-subconscious, guide-toward-the-long-term-goal |
| the-world-model-plumbing-model-tier-chooser | `the-world-model-plumbing-model-tier-chooser` | world | QAR's code for model plumbing / model tier chooser (the world) | model-tier-chooser |
| the-world-model-plumbing-spend | `the-world-model-plumbing-spend` | world | QAR's code for model plumbing / spend (the world) | model-plumbing |
| the-world-model-plumbing-vendor-model-caller | `the-world-model-plumbing-vendor-model-caller` | world | QAR's code for model plumbing / vendor model caller (the world) | vendor-model-caller |
| the-world-science | `the-world-science` | world | QAR's code for science (the world) | quest-change-executor |
| the-world-tools | `the-world-tools` | world | QAR's code for tools (the world) | quest-change-executor, write-through-mcp, write-in-names |
| the-world-tools-connected-system-operations | `the-world-tools-connected-system-operations` | world | QAR's code for tools / connected system operations (the world) | quest-context, intake-sources, file-editing, quest-change-executor, write-through-mcp |
| the-world-tools-delegated-agent-sessions | `the-world-tools-delegated-agent-sessions` | world | QAR's code for tools / delegated agent sessions (the world) | quest-requests, delegated-agent-sessions, vendor-model-caller |
| the-world-tools-file-editing | `the-world-tools-file-editing` | world | QAR's code for tools / file editing (the world) | quest-context, intake-sources, file-editing, quest-change-executor |
| the-world-tools-file-reading | `the-world-tools-file-reading` | world | QAR's code for tools / file reading (the world) | system-integrity, quest-context, intake-sources, file-editing, quest-change-executor |
| the-world-tools-web-search | `the-world-tools-web-search` | world | QAR's code for tools / web search (the world) | quest-context, web-search, intake-sources, the-science, file-editing, quest-change-executor |
| package-front-door | `package-front-door` | self | The front door of the package | quest-self |
| prompts | `prompts` | prompts | Prompt text shipped as data and the scripts that keep each copy equal to its source: standing deep-run instructions (evolvable_structure.md and the public docs/reference/evolvable-structure.md generated from the ESOI skill) and the vendored quest field guidance | context-intake, run-applies-esoi, response-quality |
| eval-components | `eval-components` | evaluation | Per-component evaluation harness: one function per component, impls swappable, same dataset,  the combined QAR metric (qar_overall) reads their recorded rows,  qar_overall/render_handoff.py renders the handoff page from them | system-integrity, context-retriever, quest-self, planner, the-science, the-tools, critical-thinking |
| scripts | `scripts` | scripts | Operator scripts, each the live operator of a solution: web-search bench (search-the-web), card quarantine (repair-a-card-store), restart-if-idle (run-queued-tasks),  dataset_sheet exports labelled datasets (one or several files, with item and request metadata, need role and scope need) for human review and imports the corrections next to each source file (no solution of its own yet),  qar_datasets_sheet writes the evaluation datasets, their inputs and the leading implementation's last answers to an existing sheet | web-search, card-store, quest-requests |
| examples | `examples` | examples | Runnable reference consumers (env-driven, no real data) | system-integrity |
| tests | `tests` | tests | Offline test suite and shared fakes | system-integrity |
| structure-registry | `structure-registry` | docs | This registry, the solutions ontology, and the check that keeps them true | system-integrity |
| solution-tags | `solution-tags` | scripts | Solution tags: rule Q1 over esoi/tags.csv and the tags columns, and the lookup of the solutions that share a tag or a connected problem | system-integrity |
| docs-and-meta | `docs-and-meta` | docs | Documentation and repo-level project files | system-integrity |
| package-selection | `package-selection` | self | QAR's recorded selection by metric | system-integrity |
| package-entry-points | `package-entry-points` | self | The stable command line and the stable names a host application builds against | quest-self |
| the-body-container | `the-body-container` | body | The the body container of QAR | quest-self |
| the-intelligence-container | `the-intelligence-container` | intelligence | The the intelligence container of QAR | quest-self |
| the-world-container | `the-world-container` | world | The the world container of QAR | quest-self |
| the-consciousness-container | `the-consciousness-container` | consciousness | The the consciousness container of QAR | quest-self |
| package-folders | `package-folders` | self | Folders of QAR that hold only sub-folders | quest-self, hand-off-a-goal, choose-the-context |
| the-consciousness-intent-evolution | `the-consciousness-intent-evolution` | consciousness | QAR's code for intent evolution (the consciousness) | intent-evolution |
| the-consciousness-flow | `the-consciousness-flow` | consciousness | The composition of Quest Consciousness | quest-consciousness |
| the-intelligence-flow | `the-intelligence-flow` | intelligence | The composition of Quest Intelligence | quest-intelligence |
| the-world-flow | `the-world-flow` | world | The composition of Quest World | quest-world |
| the-body-diagram | `the-body-diagram` | body | The generated solution diagram of Quest Body | quest-body |
| the-consciousness-diagram | `the-consciousness-diagram` | consciousness | The generated solution diagram of Quest Consciousness | quest-consciousness |
| the-intelligence-diagram | `the-intelligence-diagram` | intelligence | The generated solution diagram of Quest Intelligence | quest-intelligence |
| the-consciousness-ways-in | `the-consciousness-ways-in` | consciousness | The ways a request comes in, handed to one router | quest-requests, take-in-the-world |
| the-consciousness-prioritization | `the-consciousness-prioritization` | consciousness | QAR's code for prioritization (the consciousness) | prioritization |
| the-consciousness-interface | `the-consciousness-interface` | consciousness | The interfaces of Quest Consciousness and Hand Off A Goal | quest-consciousness, hand-off-a-goal |
| the-intelligence-interface | `the-intelligence-interface` | intelligence | The interface of Quest Intelligence | quest-intelligence |
| the-world-interface | `the-world-interface` | world | The interface of Quest World | quest-world |
| the-intelligence-higher-mind-objectives-place-on-a-quest | `the-intelligence-higher-mind-objectives-place-on-a-quest` | intelligence | QAR's code for higher mind / objectives / place on a quest (the intelligence) | place-on-a-quest |
