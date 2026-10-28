"""FLOOR: no model call. The acknowledgement is the fixed Queued line (the library's own fallback, reached by having no
provider), an ask is classified by its shape (a standing rule opens with always or never, a question ends in a question
mark, a request opens with an action verb), and a goal is judged only when its name opens with a drafting verb. Anything
else is left unclassified or unjudged, which is what the terminal does today when no model is configured."""
import re

from assist_a_terminal_session import arms

STANDING = ("always", "never", "from now on")
ACTION_VERBS = {"send", "draft", "fix", "write", "add", "update", "confirm", "share", "review", "check", "make", "create",
                "prepare", "schedule", "ask", "tell", "set", "remove", "publish", "post", "book", "call", "email"}
AI_VERBS = {"draft", "write", "summarise", "summarize", "outline", "compile", "list"}


def classify(ask, roster):
    text = str(ask.get("text") or "").strip()
    low = text.lower()
    first = re.split(r"\W+", low, maxsplit=1)[0] if low else ""
    if low.startswith(STANDING):
        kind = "standing"
    elif low.endswith("?"):
        kind = "question"
    elif first in ACTION_VERBS:
        kind = "request"
    else:
        return None
    named = [m["user_id"] for m in roster if re.search(rf"\b{re.escape(str(m.get('name') or ''))}\b", text, re.I)]
    return {"kind": kind, "assignee_id": named[0] if kind == "request" and len(named) == 1 else None,
            "confidence": 0.9, "reason": "shape of the text", "title": text[:80]}


def judge(goal):
    first = re.split(r"\W+", str(goal.get("name") or "").lower().strip(), maxsplit=1)[0]
    return {"ai_doable": True, "confidence": 0.9, "reason": "opens with a drafting verb"} if first in AI_VERBS else None


def build(meter):  # noqa: ARG001
    def assist(case):
        kind = case["kind"]
        if kind == "ack":
            return arms.run_ack(case, None)
        if kind == "classify":
            return arms.run_classify(case, classify)
        if kind == "judge":
            return arms.run_judge(case, judge)
        return arms.run_gate(case, classify)
    return assist
