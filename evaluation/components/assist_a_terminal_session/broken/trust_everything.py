"""BASELINE: the shortcut a first draft would write. Every ask is a request, every goal is AI-doable, and the sentence
printed is whatever came to mind, queued or not, with no check on the task id. Zero model calls."""
from assist_a_terminal_session import arms


def build(meter):  # noqa: ARG001
    def assist(case):
        kind = case["kind"]
        if kind == "ack":
            return {"stdout": "I am looking into this \u2014 right away.", "exit": 0}
        if kind == "classify":
            return {"raw": {"kind": "request", "assignee_id": None}, "decision": None}
        if kind == "judge":
            return {"raw": {"ai_doable": True}}
        return {"sent": [a["key"] for a in case["asks"]]}
    return assist
