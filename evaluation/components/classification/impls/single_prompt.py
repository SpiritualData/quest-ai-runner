"""ABLATION of the candidate: the live pre-route (``core/pre_route.py``) used ALONE, with no planner behind
it. One small direct call to the same model, and whatever it says is the answer, sure or not. It returns a
label and tool calls, not a PlanDecision: it cannot gather evidence, write a deep brief or ground an answer,
so it is not a candidate to replace the planner (see the route-a-message gap). It shows what the cheap call
does when nothing checks it."""
import os

import common  # noqa: F401  (sets sys.path for the repo)


def build(meter):
    from quest_ai_runner.adapters.claude_cli_provider import ClaudeCliProvider
    from quest_ai_runner.core.pre_route import PreRouter
    from quest_ai_runner.core.tools import build_tool_registry
    registry = build_tool_registry(dict(os.environ, QUEST_BASE_URL=common.OFFLINE_QUEST_URL, QUEST_API_KEY="offline-eval"))
    router = PreRouter(meter.wrap(ClaudeCliProvider()), os.environ.get("QAR_EVAL_MODEL", "haiku"))

    def classify(message, context):
        catalog = registry.render_catalog(registry.select(message, 6))
        pre = router.route(message, catalog, today="2026-10-16",
                           known=f"Quest: {context['quest']['title']}\nOutcome: {context['quest']['outcome']}")
        if pre is None:
            return {"route": None, "tool": None, "calls": []}
        first = pre.calls[0] if pre.calls else None
        return {"route": pre.route, "tool": first["name"] if first else None, "calls": pre.calls,
                "raw": f"{pre.route} sure={pre.sure}"}
    return classify
