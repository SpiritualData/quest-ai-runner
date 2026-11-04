# private_context_needs

Measures context selection on REAL inputs with labelled candidate context (notes, goal updates, decisions, conversation turns,
quest state fields, context cards), not on coding tasks about one repository. The cases are private and are never in this
repository. They are read from the folder named by `QAR_PRIVATE_CONTEXT_DATASET` (a folder holding `cases.jsonl`, or that file).
With the variable absent, every run loads zero cases, prints why, and ends as an empty run; nothing fails.

```
QAR_PRIVATE_CONTEXT_DATASET=/path/to/folder esoi run private_context_needs --impl keyword_idf
esoi run private_context_needs --impl send_nothing      # no dataset: 0 cases, says why
```

Function: `select(task, candidates) -> [item_id]` (see `interface.py`). Arms, none of which calls a model:
`send_nothing` (floor), `send_everything`, `keyword_idf` (TF-IDF ranking of single items), `qar_topic_cards` (candidates grouped into
topic cards, selected by the real `FileContextStore.assemble`, members of selected cards returned).

Score: ONE balanced number per arm, the mean over cases of `0.75 * accuracy + 0.20 * size + 0.05 * time`, with the weights and
budgets fixed in `score.py` before scoring. Accuracy is the harmonic mean of needed-item recall and (1 - noise); a self-contained
case (no needed item) scores 1 when nothing is selected. Size is 1 - selected characters / 8000. Per-case recall, noise and size are
details. `ok` (recall at least 0.5 and noise at most 0.5; a self-contained case: at most one item selected) is a descriptive count
only and never ranks the arms.
