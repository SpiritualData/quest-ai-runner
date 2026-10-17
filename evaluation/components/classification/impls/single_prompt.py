"""ALTERNATIVE solution: the live pre-route prompt used ALONE, with no planner behind it. One small direct
call to the same model; whatever it says is the answer, sure or not. Shows what the cheap call does on the
router's whole job when nothing checks it."""
import os

import common  # noqa: F401  (sets sys.path for the repo)


def build(meter):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    from quest_ai_runner.core.pre_route import PreRouter
    router = PreRouter(meter.wrap(ClaudeCliProvider()), os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def classify(message, context):
        pre = router.route(message, context["tools"])
        if pre is None:
            return {"route": None, "tool": None, "calls": []}
        first = pre.calls[0] if pre.calls else None
        return {"route": pre.route, "tool": first["name"] if first else None, "calls": pre.calls,
                "raw": f"{pre.route} sure={pre.sure}"}
    return classify
