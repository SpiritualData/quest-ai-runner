"""BASELINE: zero-model-call keyword rules. The floor any model-based solution must beat to justify its cost."""
import re

DEEP = re.compile(r"\b(refactor|repo|script|python|deploy|server|logs|readme|test suite|literature review|"
                  r"report file|my machine|export|csv|~/)", re.I)
TOOL = [("create_quest_experiment", r"start an experiment"),
        ("update_quest_experiment", r"(conclude|rename).*experiment"),
        ("find_quest_experiments", r"(which|find).*experiment"),
        ("create_quest_goal", r"(add|create) (a )?(\w+ )?goal"),
        ("update_quest_current_state", r"update my current state"),
        ("add_quest_measurable_outcome", r"measurable outcome:"),
        ("create_quest_habit", r"(add|create) a (daily )?habit")]


def build(meter):  # noqa: ARG001
    def classify(message, context):  # noqa: ARG001
        if re.search(r"don't (create|save)|no need to save", message, re.I):
            return {"route": "answer", "tool": None}
        if DEEP.search(message):
            return {"route": "deep", "tool": None}
        for name, pat in TOOL:
            if re.search(pat, message, re.I):
                return {"route": "tool", "tool": name}
        return {"route": "answer", "tool": None}
    return classify
