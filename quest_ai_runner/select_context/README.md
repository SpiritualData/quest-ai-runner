# Selecting the context a planner reads

The material a planner reads before it plans, whether a card, a file, a conversation or a specification record, is the material the request concerns, however the request is worded.

*ESOI records of this intent (evolvable structure that optimizes intent), generated from the CSV files beside this page by `esoi readmes`. Edit the CSV files, not this page.*

This sub-intent arises as the implementation `do-grounded-work:library` of its parent intent [`quest-ai-runner`](../../esoi/README.md). Opportunities: Which material a planner reads is a retrieval problem with its own alternatives and its own public evidence, which the parent solution does not weigh: lexical, vector and hybrid rankers over a person's cards, files and conversations, a ranker per representative, references that must resolve to current text without failing a turn, and one ranking that holds the cards and the specification records of a repository together so a run also sees when no solution covers its request. Public benchmarks of similar rankers are shown beside the measurements made here.

## How the solutions fit together

Context card selector chooses the cards, files and conversations a planner reads before it plans, so the right material is found however a request is worded. This area arises from the library implementation that wires every area through one configuration (do-grounded-work:library, the top intent's implementation), because no built implementation of one solution of the parent composes the assemblers: the lane's configuration chooses them. The Semantic vector index finds a card by what it means, not by its words. Fail-safe card reference resolver follows the pointer of a card to the person's current notes and files. Unified card and specification search ranks the cards of a run together with the specification records of a repository in one list.

## Diagram

An arrow runs from the solution that provides to the solution that builds on it; its label is what the provider outputs.

```mermaid
flowchart LR
    s0["Context card selector<br/><i>select-context</i>"]
    s1["Semantic vector index<br/><i>index-by-meaning</i>"]
    s1 -->|"Ranked hits with id, score, text and payload for search..."| s0
```

## Solutions at a glance

| Solution | Handle | Entry call | Selected implementation | Metric |
|---|---|---|---|---|
| Context card selector | `select-context` | no entry declared | `blind_grep` | Context selection ranking accuracy and card-quality outcome |
| Fail-safe card reference resolver | `resolve-references` | no entry declared | `note` | Pointer-resolution safety over offline cases |
| Unified card and specification search | `search-cards-and-specs` | no entry declared | `solutions_and_problems_with_links` | Mixed card and specification selection, with both existing selection metrics held |
| Semantic vector index | `index-by-meaning` | no entry declared | `qdrant` | Semantic recall and query latency on the synthetic handbook |

## Problems and ideals

**Problems**

- `prob-context-rediscovered-each-run`: An agent asked to answer or work from a person's own material has to find the relevant part of it first.
- `prob-two-searches-for-one-context`: A run's context comes from two retrieval pipelines that repeat the same stages separately: context card selection (keyword IDF, vector search, weighted reciprocal-rank fusion, then a cheap-model...

**Ideals**

- Compounding Context (`ideal-compounding-context`): What one run found is kept as a card that later runs reuse, so every run starts grounded in the person's material at little cost, reads only what is current, and can name where each piece came from.
- One Ranked Context (`ideal-one-ranked-context`): A run's context comes from one search over everything it may be grounded in, with one ranking and one budget, and every result says what kind of thing it is and where it lives.

## Metrics and their leaders

- Context selection ranking accuracy and card-quality outcome (`metric-select-context`): leader `hub_feature_rerank`, 115 of 185 cases
- Pointer-resolution safety over offline cases (`metric-resolve-references`): leader `card_resolvers`, 16 of 16 cases
- Mixed card and specification selection, with both existing selection metrics held (`metric-search-cards-and-specs`): leader `solutions_and_problems_with_links`, 14 of 17 cases
- Semantic recall and query latency on the synthetic handbook (`metric-index-by-meaning`): leader `memory_cosine`, 16 of 17 cases

## Where to go next

- Parent intent: [Grounded AI that advances people's goals](../../esoi/README.md)
- Repository: [top README](../../README.md)
- Records: [intent.csv](intent.csv), [problems.csv](problems.csv), [ideals.csv](ideals.csv), [solutions.csv](solutions.csv), [implementations.csv](implementations.csv), [metrics.csv](metrics.csv)
- Diagram page: [diagram.md](diagram.md)

---

## The full record

### Problems

- `prob-context-rediscovered-each-run`: An agent asked to answer or work from a person's own material has to find the relevant part of it first. With nothing kept between runs it re-discovers the same files and facts on every run (the planner asks what to read, retrieval searches, the loop re-plans), which costs tokens and latency, can miss what an earlier run already found, and leaves no record of where an answer came from.
- `prob-two-searches-for-one-context`: A run's context comes from two retrieval pipelines that repeat the same stages separately: context card selection (keyword IDF, vector search, weighted reciprocal-rank fusion, then a cheap-model filter and consolidation) and ESOI record selection (field-weighted TF-IDF, an optional vector search, reciprocal-rank fusion, then a gated cheap-model choice). They keep separate indexes, budgets and output shapes, and only the card result is put into a run's context: ESOI records reach a run only through tools it has to think to call, or as a lexical-only slice for a deep goal, so the problem, ideal and metric a run serves are often not in view.

### Ideals

- Compounding Context (`ideal-compounding-context`): What one run found is kept as a card that later runs reuse, so every run starts grounded in the person's material at little cost, reads only what is current, and can name where each piece came from.
- One Ranked Context (`ideal-one-ranked-context`): A run's context comes from one search over everything it may be grounded in, with one ranking and one budget, and every result says what kind of thing it is and where it lives.

### Solutions

| Solution | Handle | Selected | Metric |
|---|---|---|---|
| Context card selector | `select-context` | blind_grep | Context selection ranking accuracy and card-quality outcome |
| Fail-safe card reference resolver | `resolve-references` | note | Pointer-resolution safety over offline cases |
| Unified card and specification search | `search-cards-and-specs` | solutions_and_problems_with_links | Mixed card and specification selection, with both existing selection metrics held |
| Semantic vector index | `index-by-meaning` | qdrant | Semantic recall and query latency on the synthetic handbook |

### Implementations

| Implementation | Handle | Where | Sub-intent |
|---|---|---|---|
| Field-weighted keyword arm | `select-context:idf_docstrings` | [evaluation/components/context_selection/impls/idf_docstrings.py](../../evaluation/components/context_selection/impls/idf_docstrings.py) |  |
| Docstring ablation of the keyword arm | `select-context:idf_paths_only` | [evaluation/components/context_selection/impls/idf_paths_only.py](../../evaluation/components/context_selection/impls/idf_paths_only.py) |  |
| Blind text search | `select-context:blind_grep` | [evaluation/components/context_selection/impls/blind_grep.py](../../evaluation/components/context_selection/impls/blind_grep.py) |  |
| Plain cosine semantic baseline | `select-context:vector_bge_small` | [evaluation/components/context_selection/impls/vector_bge_small.py](../../evaluation/components/context_selection/impls/vector_bge_small.py) |  |
| Keyword plus vector fusion (measured live class) | `select-context:hybrid_union` | [evaluation/components/context_selection/impls/hybrid_union.py](../../evaluation/components/context_selection/impls/hybrid_union.py) |  |
| Card-file keyword store | `select-context:file` | [quest_ai_runner/adapters/file_context_store.py](../adapters/file_context_store.py) |  |
| Semantic card retrieval | `select-context:vector` | [quest_ai_runner/select_context/vector_context_assembler.py](vector_context_assembler.py) |  |
| Parallel keyword and vector fusion | `select-context:hybrid` | [quest_ai_runner/select_context/hybrid_context_assembler.py](hybrid_context_assembler.py) |  |
| Exact-content BM25 arm | `select-context:bm25` | [quest_ai_runner/select_context/bm25_content_store.py](bm25_content_store.py) |  |
| Per-agent preference wrapper | `select-context:rep` | [quest_ai_runner/select_context/rep_context_assembler.py](rep_context_assembler.py) |  |
| Ordered assembler composition | `select-context:composite` | [quest_ai_runner/config_assembler.py](../config_assembler.py) |  |
| Seeded keyword card-quality arm | `select-context:seeded_keyword_store` | [evaluation/components/card_quality/impls/seeded_keyword_store.py](../../evaluation/components/card_quality/impls/seeded_keyword_store.py) |  |
| Open-gate ablation of the card-quality arm | `select-context:seeded_keyword_open` | [evaluation/components/card_quality/impls/seeded_keyword_open.py](../../evaluation/components/card_quality/impls/seeded_keyword_open.py) |  |
| Live learner plus hybrid store arm | `select-context:live_updater_hybrid` | [evaluation/components/card_quality/impls/live_updater_hybrid.py](../../evaluation/components/card_quality/impls/live_updater_hybrid.py) |  |
| Offline warm-hint builder | `select-context:idf_hint` | [evaluation/components/claude_code_ab/impls/idf_hint.py](../../evaluation/components/claude_code_ab/impls/idf_hint.py) |  |
| Agent without hint | `select-context:cold_claude` | [evaluation/components/claude_code_ab/impls/cold_claude.py](../../evaluation/components/claude_code_ab/impls/cold_claude.py) |  |
| Agent with hint | `select-context:warm_claude` | [evaluation/components/claude_code_ab/impls/warm_claude.py](../../evaluation/components/claude_code_ab/impls/warm_claude.py) |  |
| Registry of built-in and host resolvers | `resolve-references:note` | [quest_ai_runner/config_assembler.py](../config_assembler.py) |  |
| Production registry under scripted host resolvers | `resolve-references:card_resolvers` | [evaluation/components/resolve_references/impls/card_resolvers.py](../../evaluation/components/resolve_references/impls/card_resolvers.py) |  |
| Two pipelines side by side (today) | `search-cards-and-specs:two_pipelines` | [quest_ai_runner/core/unified_search.py](../core/unified_search.py) |  |
| Every record kind ranked beside cards (planned) | `search-cards-and-specs:every_record_a_view` |  |  |
| Cards, solutions and problems ranked; intents, ideals and metrics reached through links | `search-cards-and-specs:solutions_and_problems_with_links` | [quest_ai_runner/core/unified_search.py](../core/unified_search.py) |  |
| The hub's per-file card arm | `select-context:hub_per_file_symbols` | [evaluation/components/context_selection/impls/hub_per_file_symbols.py](../../evaluation/components/context_selection/impls/hub_per_file_symbols.py) |  |
| One card per folder, from the hub's card helpers | `select-context:hub_folder_cards` | [evaluation/components/context_selection/impls/hub_folder_cards.py](../../evaluation/components/context_selection/impls/hub_folder_cards.py) |  |
| Qdrant-backed vector store | `index-by-meaning:qdrant` | [quest_ai_runner/remember_and_recall/qdrant_vector_store.py](../remember_and_recall/qdrant_vector_store.py) |  |
| BM25 over unit bodies (hub) | `select-context:hub_bm25_body` | [evaluation/components/context_selection/impls/hub_bm25_body.py](../../evaluation/components/context_selection/impls/hub_bm25_body.py) |  |
| Fielded BM25 over title, path and body (hub) | `select-context:hub_bm25_fielded` | [evaluation/components/context_selection/impls/hub_bm25_fielded.py](../../evaluation/components/context_selection/impls/hub_bm25_fielded.py) |  |
| BM25 over whole files (hub) | `select-context:hub_bm25_whole_files` | [evaluation/components/context_selection/impls/hub_bm25_whole_files.py](../../evaluation/components/context_selection/impls/hub_bm25_whole_files.py) |  |
| Dense retrieval with the local bge-small embedder (hub) | `select-context:hub_dense_bge_small` | [evaluation/components/context_selection/impls/hub_dense_bge_small.py](../../evaluation/components/context_selection/impls/hub_dense_bge_small.py) |  |
| Latent semantic indexing (hub) | `select-context:hub_lsi` | [evaluation/components/context_selection/impls/hub_lsi.py](../../evaluation/components/context_selection/impls/hub_lsi.py) |  |
| BM25 and dense fused by reciprocal rank (hub) | `select-context:hub_hybrid_rrf` | [evaluation/components/context_selection/impls/hub_hybrid_rrf.py](../../evaluation/components/context_selection/impls/hub_hybrid_rrf.py) |  |
| Hybrid reranked by weighted features and priors (hub) | `select-context:hub_feature_rerank` | [evaluation/components/context_selection/impls/hub_feature_rerank.py](../../evaluation/components/context_selection/impls/hub_feature_rerank.py) |  |
| Feature rerank without link and co-change priors (hub) | `select-context:hub_feature_rerank_no_priors` | [evaluation/components/context_selection/impls/hub_feature_rerank_no_priors.py](../../evaluation/components/context_selection/impls/hub_feature_rerank_no_priors.py) |  |
| Hybrid that returns nothing when the corpus does not answer (hub) | `select-context:hub_abstaining_hybrid` | [evaluation/components/context_selection/impls/hub_abstaining_hybrid.py](../../evaluation/components/context_selection/impls/hub_abstaining_hybrid.py) |  |
| Hybrid reranked by a model trained on past commits (repository) or on earlier private cases (hub) | `select-context:hub_learned_rerank` | [evaluation/components/context_selection/impls/hub_learned_rerank.py](../../evaluation/components/context_selection/impls/hub_learned_rerank.py) |  |
| Learned reranker that returns nothing when the corpus does not answer (hub) | `select-context:hub_abstaining_learned` | [evaluation/components/context_selection/impls/hub_abstaining_learned.py](../../evaluation/components/context_selection/impls/hub_abstaining_learned.py) |  |

### Metrics

- Context selection ranking accuracy and card-quality outcome (`metric-select-context`): `esoi run context_selection --impl hybrid_union; esoi run card_quality --impl seeded_keyword_store; esoi run claude_code_ab --impl idf_hint`
- Pointer-resolution safety over offline cases (`metric-resolve-references`): `esoi run resolve_references --impl card_resolvers`
- Mixed card and specification selection, with both existing selection metrics held (`metric-search-cards-and-specs`): `esoi run context_and_specs --impl solutions_and_problems_with_links; esoi run spec_selection --impl solutions_and_problems_with_links; esoi run context_selection --impl solutions_and_problems_with_links`
- Semantic recall and query latency on the synthetic handbook (`metric-index-by-meaning`): `esoi run vector_recall --impl qdrant_embedded`
