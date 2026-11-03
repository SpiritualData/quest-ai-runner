These are ESOI records rendered as a diagram (evolvable structure that optimizes intent). This page is generated from the solutions' outputs and the implementations' `uses_solution_ids` by `esoi diagram`; edit those records, not this page.

# Solution diagram: Grounded AI that advances people's goals

quest-ai-runner turns a person's request or queued task into grounded work in one loop, and do-grounded-work is that loop. do-grounded-work takes a request in through talk-over-a-channel, operate-from-a-terminal, receive-input-mid-run and run-queued-tasks, hands it to route-a-message, and answers chat turns through answer-a-turn. It grounds the answer in the person's own material through select-context, gather-evidence, remember-conversations, recall-recent-context, resolve-references and cache-cards, with persist-cards and index-by-meaning keeping the cards, and it keeps the prompt in order through assemble-a-prompt and the model choice through call-a-model, choose-a-model-tier and track-usage-and-cost. It does the work through operate-a-quest, write-files, write-through-mcp and run-deep-work, watches it through watch-for-risk and detect-a-stalled-run, settles narrow questions through judge-a-decision, stops unsafe replies through guard-a-reply, and brings a step only a person may take to that person through escalate-to-a-human. It shows what it is doing through show-progress, learns from corrections through guide-by-feedback and store-feedback, and replays what worked through replay-known-operations. It keeps itself measurable and open through keep-it-generic, select-an-implementation, configure-a-lane, verify-offline, keep-secrets-out, behave-by-esoi and judge-esoi-behavior, and it is offered to others through expose-the-public-api, learn-by-example and explain-qar. The specification of the loop is spread over named areas. reason-and-answer is the mind, one goal-focused stream per request, and remember-and-recall is the subconscious that admits the person's own cards into it, with card-store holding them, bound-card-growth and repair-a-card-store keeping the store from inflating, choose-what-to-index choosing what deserves a card, label-context-cards and detect-stale-cards keeping them honest, and search-cards-and-specs searching cards and specifications together. know-the-world answers questions about the world through search-the-web. think-critically holds deep runs to their metric through iterate-by-spec, spec-selection, ground-in-intent, draft-a-metric and gate-an-autopilot-pass, and core-ml-tasks, answer-end-to-end and tag-solutions measure the classifiers, the whole turn and the reach of a change. act-as-the-body does the work and claim-reach claims only the reach it has, while use-open-source-hubs prefers a standard hub to a bespoke integration and access-quest-operations reaches Quest's operations. hold-the-intents keeps the standing view of a representative across turns, select-a-rep picks the representative, hand-off-a-goal decides who owns a new goal, and guide-toward-the-long-term-goal keeps work aimed at the person's end, with persist-predictions saving what was learned. The person is kept informed through reply-in-steps, ask-on-block, narrate-a-turn, show-the-ais-work, explain-an-answer, describe-an-attachment and assist-a-terminal-session, and feed-context-sources adds what changed in a work item, while justify-model-calls accounts for every model call.

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Grounded goal agent: the engine that...<br/>do-grounded-work"]
    s1["Cost-aware message router<br/>route-a-message"]
    s2["Grounded chat turn handler<br/>answer-a-turn"]
    s3["Quest change executor<br/>operate-a-quest"]
    s4["Multi-source evidence retriever<br/>gather-evidence"]
    s5["Bounded web research<br/>search-the-web"]
    s6["Tiered model provider layer<br/>call-a-model"]
    s7["Bounded autonomous work runner<br/>run-deep-work"]
    s8["Bounded reversible file writer<br/>write-files"]
    s9["Allowlisted operation writer<br/>write-through-mcp"]
    s10["Human decision escalation channel<br/>escalate-to-a-human"]
    s11["Mode-aware progress surfacing<br/>show-progress"]
    s12["Cross-conversation recall store<br/>remember-conversations"]
    s13["Context card selector<br/>select-context"]
    s14["Context card persistence backend<br/>persist-cards"]
    s15["Semantic vector index<br/>index-by-meaning"]
    s16["Feedback-to-guidance loop<br/>guide-by-feedback"]
    s17["Live messaging channel transport<br/>talk-over-a-channel"]
    s18["Learned-operation fast path<br/>replay-known-operations"]
    s19["Overseer: risk management that raises...<br/>watch-for-risk"]
    s20["Source-change staleness detector<br/>detect-stale-cards"]
    s21["Budgeted prompt assembler<br/>assemble-a-prompt"]
    s22["Guarded model-verdict judge<br/>judge-a-decision"]
    s23["Verbatim feedback ledger<br/>store-feedback"]
    s24["Scoped recent-context warm start<br/>recall-recent-context"]
    s25["Offline card cache and write outbox<br/>cache-cards"]
    s26["Standing-instruction behaviour layer<br/>behave-by-esoi"]
    s27["Queued-task runner lane<br/>run-queued-tasks"]
    s28["Cost-ordered model tier selector with...<br/>choose-a-model-tier"]
    s29["Scoped prediction store<br/>persist-predictions"]
    s30["Intent frame for quest-scoped prompts<br/>ground-in-intent"]
    s31["Specification slice before a task and...<br/>iterate-by-spec"]
    s32["Core ML tasks: quest classification and...<br/>core-ml-tasks"]
    s33["Select the intent specifications an...<br/>spec-selection"]
    s34["Card store growth prevention<br/>bound-card-growth"]
    s35["Card store repair<br/>repair-a-card-store"]
    s36["Index scope decision: which folders and...<br/>choose-what-to-index"]
    s37["Mind: one goal-focused working stream...<br/>reason-and-answer"]
    s38["Subconscious: the person's own...<br/>remember-and-recall"]
    s39["Science: evidence-weighted knowledge...<br/>know-the-world"]
    s40["World: doing things outside the host...<br/>claim-reach"]
    s41["Body: the host's data model as the...<br/>act-as-the-body"]
    s42["Higher mind: an AI representative's...<br/>hold-the-intents"]
    s43["Critical thinking: the enforcer of ESOI...<br/>think-critically"]
    s44["Representative selection: which AI...<br/>select-a-rep"]
    s45["Card categories and metadata<br/>label-context-cards"]
    s46["Whole-path answer check<br/>answer-end-to-end"]
    s47["unconfirmed: for Payal to confirm...<br/>access-quest-operations"]
    s48["unconfirmed: for Payal to confirm. QAR...<br/>card-store"]
    s49["Show the AI's work and next steps<br/>show-the-ais-work"]
    s1 -->|"A routing decision: one of answer, tool, deep, confirm or..."| s32
    s1 -->|"A routing decision: one of answer, tool, deep, confirm or..."| s40
    s2 -->|"The reply text, the routing taken (inline or delegated)..."| s39
    s3 -->|"The resulting quest state and a reply that reports what was..."| s41
    s3 -->|"The resulting quest state and a reply that reports what was..."| s43
    s4 -->|"Observation values carrying text, a path or source label..."| s38
    s4 -->|"Observation values carrying text, a path or source label..."| s39
    s4 -->|"Observation values carrying text, a path or source label..."| s40
    s4 -->|"Observation values carrying text, a path or source label..."| s41
    s5 -->|"One compact observation of title, link and snippet per hit..."| s39
    s6 -->|"A raw decision dictionary for planning, answer text for..."| s0
    s6 -->|"A raw decision dictionary for planning, answer text for..."| s37
    s6 -->|"A raw decision dictionary for planning, answer text for..."| s40
    s6 -->|"A raw decision dictionary for planning, answer text for..."| s42
    s7 -->|"A result stating whether the goal was met, the output text..."| s40
    s8 -->|"A write result stating success, whether the file was..."| s40
    s9 -->|"A write result with success, error and an audit detail of..."| s40
    s10 -->|"A decision identifier the caller reports back"| s19
    s10 -->|"A decision identifier the caller reports back"| s40
    s10 -->|"A decision identifier the caller reports back"| s43
    s10 -->|"A decision identifier the caller reports back"| s44
    s11 -->|"None returned; surfaced events reach the consumer's stream..."| s49
    s12 -->|"A conversation context holding rendered text slices from..."| s42
    s13 -->|"An assembled context: rendered context view, card ids in..."| s38
    s13 -->|"An assembled context: rendered context view, card ids in..."| s40
    s13 -->|"An assembled context: rendered context view, card ids in..."| s42
    s14 -->|"Load-all returns a mapping of id to card, read returns a..."| s35
    s14 -->|"Load-all returns a mapping of id to card, read returns a..."| s41
    s14 -->|"Load-all returns a mapping of id to card, read returns a..."| s42
    s14 -->|"Load-all returns a mapping of id to card, read returns a..."| s48
    s15 -->|"Ranked hits with id, score, text and payload for search..."| s48
    s16 -->|"A list of guidance cards with bodies for the..."| s42
    s16 -->|"A list of guidance cards with bodies for the..."| s44
    s17 -->|"An inbound batch (messages with ids, chat references, text..."| s40
    s18 -->|"The reply text and exit reason of the run, with the tool..."| s38
    s20 -->|"Whether the file is stale, and the stale card list for the..."| s48
    s21 -->|"The assembled prompt string for each assembly point"| s37
    s22 -->|"A parsed verdict (signal, reach verdict, quest ranking or..."| s19
    s22 -->|"A parsed verdict (signal, reach verdict, quest ranking or..."| s38
    s22 -->|"A parsed verdict (signal, reach verdict, quest ranking or..."| s44
    s23 -->|"Ledger items with status lines, the open and standing sets..."| s42
    s24 -->|"Recent card records merged across scopes, narrower scope..."| s42
    s25 -->|"The stored listing with its update time, or none, and the..."| s48
    s26 -->|"A reply, then a pass or fail verdict with a one-line reason..."| s43
    s27 -->|"A reported status on each task (with result text or..."| s43
    s27 -->|"A reported status on each task (with result text or..."| s44
    s28 -->|"A starting model and ladder, a concrete model id for a tier..."| s19
    s29 -->|"The saved patterns, the unexpired predictions, a..."| s38
    s29 -->|"The saved patterns, the unexpired predictions, a..."| s42
    s30 -->|"Frame text, an anchor (intent, solution, implementation..."| s42
    s30 -->|"Frame text, an anchor (intent, solution, implementation..."| s43
    s31 -->|"A slice for the brief; per record served: the last run that..."| s43
    s33 -->|"A SpecSelection: ids best first then linked ids, the..."| s42
    s33 -->|"A SpecSelection: ids best first then linked ids, the..."| s43
    s34 -->|"Cards written only under an id made of their origin; a..."| s48
    s35 -->|"A plan (tidy-state/last-plan.json for the startup tidy), a..."| s48
    s36 -->|"A FolderGate: the set of corpus-relative folders no card..."| s35
    s37 -->|"Each step's view of the request (a carried message list or..."| s41
    s38 -->|"The admitted items with a one-line reason each, and the..."| s0
    s38 -->|"The admitted items with a one-line reason each, and the..."| s37
    s42 -->|"For a request the representative handles: its standing view..."| s38
    s42 -->|"For a request the representative handles: its standing view..."| s44
    s44 -->|"The chosen representative (or none for the plain..."| s42
    s45 -->|"The text or verdict the call returns"| s48
    s46 -->|"The reply, the writes that landed, the web queries made and..."| s40
    s47 -->|"The result of one Quest operation"| s0
    s48 -->|"unconfirmed: for Payal to confirm. Hub Card objects"| s0
```
