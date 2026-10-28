"""CURRENT, live (``explain-an-answer:model_explanation``): the library's own ``write_answer_explanation`` writes the prose half with its
one cheap call (the fast tier, haiku) on the Claude Code subscription, and the recorded half comes from the trace. Model calls, not
API dollars: the dollar figure a run reports is a list-price equivalent. Stay on the ladder of esoi/metric-run-policy.md and always
pass ``--max-calls``."""
PAID = True
import claude_judge
from explain_an_answer import arms
from quest_ai_runner.core.orchestrator_judges import JudgesMixin


def build(meter):
    provider = claude_judge.cli_provider(meter, arms.MODEL)
    write = arms.writer_for(provider, JudgesMixin)

    def explain(case):
        trace = arms.trace_of(case)
        out = arms.result_of(trace, write(trace) or {})
        out["provider"] = provider
        return out
    return explain
