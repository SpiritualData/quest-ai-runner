# Arms in impls

*ESOI arms kept in `evaluation/components/context_selection/impls`, generated from the records of the intents that own them by `esoi readmes`. Edit those records, not this page.*

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Blind text search | `select-context:blind_grep` | [`evaluation/components/context_selection/impls/blind_grep.py`](blind_grep.py) | selected |
| Hybrid that returns nothing when the corpus does not answer (hub) | `select-context:hub_abstaining_hybrid` | [`evaluation/components/context_selection/impls/hub_abstaining_hybrid.py`](hub_abstaining_hybrid.py) |  |
| Learned reranker that returns nothing when the corpus does not answer (hub) | `select-context:hub_abstaining_learned` | [`evaluation/components/context_selection/impls/hub_abstaining_learned.py`](hub_abstaining_learned.py) |  |
| BM25 over unit bodies (hub) | `select-context:hub_bm25_body` | [`evaluation/components/context_selection/impls/hub_bm25_body.py`](hub_bm25_body.py) |  |
| Fielded BM25 over title, path and body (hub) | `select-context:hub_bm25_fielded` | [`evaluation/components/context_selection/impls/hub_bm25_fielded.py`](hub_bm25_fielded.py) |  |
| BM25 over whole files (hub) | `select-context:hub_bm25_whole_files` | [`evaluation/components/context_selection/impls/hub_bm25_whole_files.py`](hub_bm25_whole_files.py) |  |
| Dense retrieval with the local bge-small embedder (hub) | `select-context:hub_dense_bge_small` | [`evaluation/components/context_selection/impls/hub_dense_bge_small.py`](hub_dense_bge_small.py) |  |
| Hybrid reranked by weighted features and priors (hub) | `select-context:hub_feature_rerank` | [`evaluation/components/context_selection/impls/hub_feature_rerank.py`](hub_feature_rerank.py) |  |
| Feature rerank without link and co-change priors (hub) | `select-context:hub_feature_rerank_no_priors` | [`evaluation/components/context_selection/impls/hub_feature_rerank_no_priors.py`](hub_feature_rerank_no_priors.py) |  |
| One card per folder, from the hub's card helpers | `select-context:hub_folder_cards` | [`evaluation/components/context_selection/impls/hub_folder_cards.py`](hub_folder_cards.py) |  |
| BM25 and dense fused by reciprocal rank (hub) | `select-context:hub_hybrid_rrf` | [`evaluation/components/context_selection/impls/hub_hybrid_rrf.py`](hub_hybrid_rrf.py) |  |
| Hybrid reranked by a model trained on past commits (repository) or on earlier private cases (hub) | `select-context:hub_learned_rerank` | [`evaluation/components/context_selection/impls/hub_learned_rerank.py`](hub_learned_rerank.py) |  |
| Latent semantic indexing (hub) | `select-context:hub_lsi` | [`evaluation/components/context_selection/impls/hub_lsi.py`](hub_lsi.py) |  |
| The hub's per-file card arm | `select-context:hub_per_file_symbols` | [`evaluation/components/context_selection/impls/hub_per_file_symbols.py`](hub_per_file_symbols.py) |  |
| Keyword plus vector fusion (measured live class) | `select-context:hybrid_union` | [`evaluation/components/context_selection/impls/hybrid_union.py`](hybrid_union.py) |  |
| Field-weighted keyword arm | `select-context:idf_docstrings` | [`evaluation/components/context_selection/impls/idf_docstrings.py`](idf_docstrings.py) |  |
| Docstring ablation of the keyword arm | `select-context:idf_paths_only` | [`evaluation/components/context_selection/impls/idf_paths_only.py`](idf_paths_only.py) |  |
| Plain cosine semantic baseline | `select-context:vector_bge_small` | [`evaluation/components/context_selection/impls/vector_bge_small.py`](vector_bge_small.py) |  |

## Where to go next

- Owning intent: [Selecting the context a planner reads](../../../../quest_ai_runner/select_context/README.md)
- Repository: [top README](../../../../README.md)
