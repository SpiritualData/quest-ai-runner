"""BASELINE: the shortcut a first draft would write. Every task gets a proposal: the first question set to zero, with a generic
evidence line, whether or not the quest holds anything. The real parse accepts it (three words of evidence, a value), so only the cases
can catch it. Zero model calls."""
from draft_a_metric import arms


def build(meter):  # noqa: ARG001
    def draft(case):
        question = case["card"]["questions"][0]["name"]
        decision = {"propose": True, "values": {question: 0}, "evidence": [{"kind": "note", "summary": "Looks about right this week"}]}
        return arms.run_draft(case, arms.ScriptedProvider(decision), "scripted")
    return draft
