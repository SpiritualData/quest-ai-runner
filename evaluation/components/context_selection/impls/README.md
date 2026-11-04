# Arms in impls

*ESOI arms kept in `evaluation/components/context_selection/impls`, generated from the records of the intents that own them by `esoi readmes`. Edit those records, not this page.*

| Implementation | Name | Code | Selected |
|---|---|---|---|
| `select-context:blind_grep` | Blind text search | [`evaluation/components/context_selection/impls/blind_grep.py`](blind_grep.py) | selected |
| `select-context:hub_folder_cards` | One card per folder, from the hub's card helpers | [`evaluation/components/context_selection/impls/hub_folder_cards.py`](hub_folder_cards.py) |  |
| `select-context:hub_per_file_symbols` | The hub's per-file card arm | [`evaluation/components/context_selection/impls/hub_per_file_symbols.py`](hub_per_file_symbols.py) |  |
| `select-context:hybrid_union` | Keyword plus vector fusion (measured live class) | [`evaluation/components/context_selection/impls/hybrid_union.py`](hybrid_union.py) |  |
| `select-context:idf_docstrings` | Field-weighted keyword arm | [`evaluation/components/context_selection/impls/idf_docstrings.py`](idf_docstrings.py) |  |
| `select-context:idf_paths_only` | Docstring ablation of the keyword arm | [`evaluation/components/context_selection/impls/idf_paths_only.py`](idf_paths_only.py) |  |
| `select-context:vector_bge_small` | Plain cosine semantic baseline | [`evaluation/components/context_selection/impls/vector_bge_small.py`](vector_bge_small.py) |  |

## Where to go next

- Owning intent: [Selecting the context a planner reads](../../../../quest_ai_runner/select_context/README.md)
- Repository: [top README](../../../../README.md)
