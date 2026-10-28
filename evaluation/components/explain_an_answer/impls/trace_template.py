"""FLOOR (``explain-an-answer:trace_template``): no model call. The explanation is the trace-only payload the library already builds
when the generation call fails (``build_payload(trace, None)``): the recorded reads, actions, sources, verdict and metric facts, with
no prose. The grader reads it against the same cases as the call's output."""
import claude_judge
from explain_an_answer import arms


def build(meter):
    provider = claude_judge.cli_provider(meter, "grader-only")   # the only model calls are the grader's

    def explain(case):
        out = arms.trace_only(arms.trace_of(case))
        out["provider"] = provider
        return out
    return explain
