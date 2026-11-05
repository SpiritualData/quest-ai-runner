# esoi_application

*ESOI records kept beside the code in `quest_ai_runner/the_intelligence/critical_thinking/esoi_application`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Lexical TF-IDF, optional vector search, parallel batched model judging | `spec-selection:lexical-vector-llm` | [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py`](spec_selection.py) |  |
| Lexical top few only | `spec-selection:lexical-only` | [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py`](spec_selection.py) |  |
| Lexical vector gated | `spec-selection:lexical-vector-gated` | [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py`](spec_selection.py) | selected |

## Metrics

- Specification selection pass rate (`metric-spec-selection`): leader `lexical_vector_gated`, 70 of 76 cases

## Where to go next

- Up: [quest_ai_runner](../../../README.md)
- Intent: [Critical Thinking](../README.md)
- Repository: [top README](../../../../README.md)
