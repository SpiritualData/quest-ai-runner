# Intent specification selection

*ESOI records kept beside the code in `quest_ai_runner/the_intelligence/critical_thinking/esoi_application`, generated from the CSV files in this folder by `esoi readmes`. Edit the CSV files, not this page.*

Everything the AI does and says is continuously held to the metric that serves the person's goals.

These are the solutions of [Critical Thinking](../README.md).

## How the solutions fit together

Critical Thinking holds deep runs to their metric through Specification slice and write-back on the records Intent specification selection picks, settles narrow questions through Guarded model-verdict judge, stops unsafe replies through Reply safety and anticipation guards, ties work to its purpose through Intent frame for quest-scoped prompts, tells a stuck run from a slow one through Run liveness reviewer, drafts a missing metric through Metric drafting for a task, and keeps background work inside its hours and budget through Autopilot run-or-skip gate.

## Intent specification selection (`spec-selection`)

Choose, from an ESOI specification index, the few records a new request concerns and their linked records, so the work starts from the intent that applies.

- Entry call: `esoi spec index --out FILE`
- Selected implementation: Lexical vector gated (`lexical-vector-gated`)
- Metric: Specification selection pass rate
- Implemented by: [`evaluation/components/act_as_the_body/__init__.py`](../../../../evaluation/components/act_as_the_body/__init__.py), [`evaluation/components/act_as_the_body/broken/__init__.py`](../../../../evaluation/components/act_as_the_body/broken/__init__.py), [`evaluation/components/act_as_the_body/broken/blind_replay.py`](../../../../evaluation/components/act_as_the_body/broken/blind_replay.py), [`evaluation/components/act_as_the_body/cases.json`](../../../../evaluation/components/act_as_the_body/cases.json), [`evaluation/components/act_as_the_body/impls/__init__.py`](../../../../evaluation/components/act_as_the_body/impls/__init__.py), [`evaluation/components/act_as_the_body/impls/card_and_reads.py`](../../../../evaluation/components/act_as_the_body/impls/card_and_reads.py), [`evaluation/components/act_as_the_body/interface.py`](../../../../evaluation/components/act_as_the_body/interface.py), [`evaluation/components/act_as_the_body/score.py`](../../../../evaluation/components/act_as_the_body/score.py) and 948 more

## Implementations

| Implementation | Handle | Code | Selected |
|---|---|---|---|
| Lexical TF-IDF, optional vector search, parallel batched model judging | `spec-selection:lexical-vector-llm` | [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py`](spec_selection.py) |  |
| Lexical top few only | `spec-selection:lexical-only` | [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py`](spec_selection.py) |  |
| Lexical vector gated | `spec-selection:lexical-vector-gated` | [`quest_ai_runner/the_intelligence/critical_thinking/esoi_application/spec_selection.py`](spec_selection.py) | selected |

## Metrics

- Specification selection pass rate (`metric-spec-selection`): leader `lexical_vector_gated`, 70 of 76 cases

## Problems

- A new request or input arrives and the system cannot tell which of a project's intent specifications (problems, ideals, solutions, implementations, metrics) concern it, so it either loads none, loads all, or relies on whichever the person remembers to mention. (`prob-intent-specs-not-found-for-input`)

## Ideals

- Specifications that apply are in view (`ideal-specs-that-apply-are-in-view`): Every run sees the intent specifications that concern its input, and only those, at the cheapest tier that lets it decide.

## Where to go next

- Up: [quest_ai_runner/the_intelligence](../../README.md)
- Intent: [Critical Thinking](../README.md)
- Repository: [top README](../../../../README.md)
